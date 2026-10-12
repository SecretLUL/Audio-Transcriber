"""StepFun StepAudio 3 ASR backend (cloud).

The service takes the audio base64-encoded in a JSON body and streams the
text back as server-sent events (POST /v1/audio/asr/sse). Two things about it
shape this module:

  * Its English API reference documents no timestamps, and speaker
    attribution (diarize.py) has to know when every line was said. So the
    track is cut into passages of speech at its pauses, and every passage is
    a request of its own: each line knows where its passage lies in the
    track, whatever the service sends back. The Chinese reference of the same
    endpoint documents `enable_timestamp`, which adds start_time/end_time to
    every delta. It is asked for, and where the times come back a passage is
    split further into sentences.
  * One request per passage keeps every request small - an hour of audio is
    150 MB of base64, and the service documents no upper limit - and makes
    live transcription possible: a chunk of the running recording is only a
    few more passages.

Robustness:
  * Up to PARALLEL_REQUESTS passages are on the way at a time.
  * HTTP 429, 5xx, connection errors and an answer that breaks off are retried
    after growing pauses. A passage is a few seconds of audio, so sending one
    again costs next to nothing - unlike ElevenLabs, where it is the whole
    file.
  * A rejected key or an empty balance ends the run at once. A passage the
    service refuses (HTTP 400, a failed content review, an error event) is
    marked in the transcript and the rest goes on - unless nothing has worked
    at all, which points at the request rather than at the audio.
  * cancel() ends the wait at once. Requests already under way finish in their
    daemon threads, and their answers are dropped.
"""

import base64
import http.client
import json
import os
import threading
import urllib.error
import urllib.request
from collections import namedtuple
from dataclasses import dataclass

import numpy as np
import soundfile as sf

from .. import version
from ..audio import dsp
from .base import Backend, Segment, TranscriptionError, format_clock

API_URL = "https://api.stepfun.ai/v1/audio/asr/sse"
DEFAULT_MODEL = "stepaudio-3-asr-max"

# The languages the service documents (all but Chinese and English in
# preview). Without a language it assumes English - it detects none itself.
SUPPORTED_LANGUAGES = ("zh", "en", "ja", "ko", "fr", "es")

REQUEST_TIMEOUT = 60           # connecting, and every single read of the stream
RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0, 30.0)    # pauses before attempts 2 to 6
MAX_RETRY_AFTER_S = 60.0       # the most a Retry-After header may ask for
PARALLEL_REQUESTS = 3          # the smallest account tier allows five
CANCEL_POLL_S = 0.2            # how often the waiting caller looks for cancel()

# --- Cutting a track into passages -------------------------------------
FRAME_S = 0.1                  # the level is measured per 100 ms
VOICE_RATIO = 0.1              # speech: 10 % (-20 dB) of the track's speech level
JOIN_S = 1.0                   # shorter pauses stay inside a passage
MAX_PASSAGE_S = 30.0           # longer stretches of speech are cut ...
SPLIT_SEARCH_S = 5.0           # ... in the quietest frame of their last seconds
PAD_S = 0.2                    # sent before and after the speech, for the onsets
MIN_SPEECH_S = 0.2             # less speech than this is a click or a cough

# A refused passage is marked and skipped - but when this many are refused
# before a single one works, the problem is the request, not the audio.
FAILURES_BEFORE_GIVING_UP = 3

MAX_WORDS_PER_SEGMENT = 18
SENTENCE_ENDINGS = (".", "?", "!", "…", "。", "？", "！")


class _Retryable(TranscriptionError):
    """A failure that says nothing about the passage: send it again."""

    def __init__(self, message, delay=None):
        super().__init__(message)
        self.delay = delay      # seconds to wait first; None: the next RETRY_DELAYS


class _PassageError(TranscriptionError):
    """The service refused this passage; the others may still go through."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status    # the HTTP status, None for a refusal in the stream


@dataclass(frozen=True)
class Passage:
    """A stretch of speech that is sent as one request, in samples.

    start/end is what is sent - the speech with PAD_S around it - and
    speech_start/speech_end where the speech itself lies.
    """
    start: int
    end: int
    speech_start: int
    speech_end: int


# A delta of the answer. start/end are seconds into the passage, None when
# the service sent no times.
_Piece = namedtuple("_Piece", "text start end item")


class StepFunBackend(Backend):
    name = "StepFun StepAudio 3 ASR"

    def __init__(self, api_key, model_id=DEFAULT_MODEL):
        self.api_key = (api_key or "").strip()
        self.model_id = model_id or DEFAULT_MODEL
        self._cancel_event = threading.Event()
        # Dropped for good once the service turns the parameter down.
        self._ask_timestamps = True
        self._user_agent = f"Sotto/{version.current() or 'dev'}"

    @property
    def _cancelled(self):
        return self._cancel_event.is_set()

    def _check_cancelled(self):
        if self._cancelled:
            raise TranscriptionError("Transcription was cancelled.")

    # ------------------------------------------------------------------
    def transcribe(self, wav_path, language="de", log=None, track="",
                   progress=None):
        if not self.api_key:
            raise TranscriptionError(
                "No StepFun API key is configured. Enter it on the Settings "
                "tab or set the STEPFUN_API_KEY environment variable.")
        if not os.path.exists(wav_path):
            raise TranscriptionError(f"Audio file not found: {wav_path}")
        self._check_cancelled()

        rate, frame_len, total, levels = _measure(wav_path)
        passages = [
            Passage(*(min(total, frame * frame_len) for frame in frames))
            for frames in find_passages(levels)]
        if not passages:
            self._log(log, "No speech found for StepFun to transcribe.\n")
            return []

        speech_s = sum(p.speech_end - p.speech_start for p in passages) / rate
        self._log(log, f"Sending {len(passages)} passage(s) of speech "
                       f"({format_clock(speech_s)}) to StepFun…\n")
        outcomes = self._run_all(wav_path, passages, rate, language, log,
                                 progress)

        segments = []
        for passage, outcome in zip(passages, outcomes):
            if isinstance(outcome, _PassageError):
                # Marked where it was said: a gap that nobody sees is worse.
                reason = str(outcome).splitlines()[0][:160]
                segments.append(Segment(start=passage.speech_start / rate,
                                        end=passage.speech_end / rate,
                                        text=f"[not transcribed: {reason}]",
                                        track=track))
            else:
                segments.extend(_passage_segments(passage, rate, *outcome,
                                                  track=track))
        return segments

    # ------------------------------------------------------------------
    def _run_all(self, path, passages, rate, language, log, progress):
        """One outcome per passage, in order: (text, pieces) or _PassageError.

        PARALLEL_REQUESTS daemon threads work through the passages; this
        thread only waits, so cancel() and a fatal error end the wait at once.
        """
        outcomes = [None] * len(passages)
        pending = list(range(len(passages) - 1, -1, -1))     # pop() goes in order
        lock = threading.Lock()
        wake = threading.Event()
        state = {"done": 0, "worked": 0, "refused": 0, "first_refusal": None,
                 "fatal": None}

        def work():
            try:
                while True:
                    with lock:
                        if (not pending or state["fatal"] is not None
                                or self._cancelled):
                            return
                        index = pending.pop()
                    passage = passages[index]
                    outcome, fatal = self._outcome(path, passage, rate, language)
                    with lock:
                        outcomes[index] = outcome
                        if fatal is not None:
                            state["fatal"] = state["fatal"] or fatal
                        elif isinstance(outcome, _PassageError):
                            state["refused"] += 1
                            state["first_refusal"] = state["first_refusal"] or outcome
                            if (not state["worked"]
                                    and state["refused"] >= FAILURES_BEFORE_GIVING_UP):
                                state["fatal"] = state["fatal"] or state["first_refusal"]
                        else:
                            state["worked"] += 1
                        # Reported under the lock, before the passage counts
                        # as done: the caller may return the moment it does,
                        # and the lines come in order. Both only post events.
                        done = state["done"] + 1
                        if isinstance(outcome, _PassageError):
                            self._log(log, f"⚠ StepFun could not transcribe the "
                                           f"passage at "
                                           f"{format_clock(passage.speech_start / rate)}: "
                                           f"{outcome}\n")
                        if progress is not None and fatal is None:
                            progress(f"Transcribing with StepFun: {done} of "
                                     f"{len(passages)} passages "
                                     f"({done / len(passages) * 100:.0f} %)")
                        state["done"] = done
                    wake.set()
            except Exception as exc:
                # A callback that fails must not leave the caller waiting for
                # passages nobody works on any more.
                with lock:
                    state["fatal"] = state["fatal"] or TranscriptionError(
                        f"StepFun request failed: {exc}")
                wake.set()

        for _ in range(min(PARALLEL_REQUESTS, len(passages))):
            threading.Thread(target=work, name="stepfun-request",
                             daemon=True).start()

        while True:
            self._check_cancelled()
            with lock:
                fatal, done = state["fatal"], state["done"]
            if fatal is not None:
                raise TranscriptionError(str(fatal)) from fatal
            if done == len(passages):
                break
            wake.wait(CANCEL_POLL_S)
            wake.clear()

        if not state["worked"] and state["first_refusal"] is not None:
            refusal = state["first_refusal"]
            raise TranscriptionError(str(refusal)) from refusal
        return outcomes

    def _outcome(self, path, passage, rate, language):
        """(outcome, fatal) of one passage: its (text, pieces) or the
        _PassageError it was refused with - or the error that ends the run."""
        try:
            return self._transcribe_passage(path, passage, rate, language), None
        except _PassageError as exc:
            return exc, None
        except TranscriptionError as exc:
            return None, exc
        except Exception as exc:                    # handed over, never lost
            return None, TranscriptionError(f"StepFun request failed: {exc}")

    def _transcribe_passage(self, path, passage, rate, language):
        """(text, pieces) of one passage, retried where retrying is safe."""
        pcm = _read_pcm(path, passage.start, passage.end)
        timestamps = self._ask_timestamps
        attempts = len(RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            self._check_cancelled()
            try:
                answer = self._attempt(pcm, rate, language, timestamps)
            except _PassageError as exc:
                if exc.status != 400 or not timestamps or attempt == attempts:
                    raise
                # Only the Chinese reference documents enable_timestamp, and
                # a service that does not know it may refuse the request over
                # it - whatever its message says. Once more without it.
                timestamps = False
                continue
            except _Retryable as exc:
                if attempt == attempts:
                    raise TranscriptionError(
                        f"{exc}\nStepFun did not answer properly in {attempts} "
                        f"attempts.") from exc
                delay = RETRY_DELAYS[attempt - 1] if exc.delay is None else exc.delay
                if self._cancel_event.wait(delay):
                    raise TranscriptionError("Transcription was cancelled.") from exc
                continue
            if not timestamps:
                self._ask_timestamps = False        # it works without: so be it
            return answer
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _attempt(self, pcm, rate, language, timestamps=True):
        request = urllib.request.Request(
            API_URL, data=self._payload(pcm, rate, language, timestamps),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": self._user_agent,
            })
        try:
            response = urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:
            self._raise_for_status(exc, language)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise _Retryable(f"Could not reach StepFun: {reason}") from exc
        with response:
            try:
                return _read_answer(response)
            except (OSError, http.client.HTTPException) as exc:   # timeouts too
                raise _Retryable(f"StepFun's answer broke off ({exc}).") from exc

    def _payload(self, pcm, rate, language, timestamps=True):
        transcription = {"model": self.model_id, "enable_itn": True}
        if timestamps:
            transcription["enable_timestamp"] = True
        if language and language != "auto":
            transcription["language"] = language
        return json.dumps({
            "audio": {
                "data": base64.b64encode(pcm).decode("ascii"),
                "input": {
                    "transcription": transcription,
                    "format": {"type": "pcm", "codec": "pcm_s16le",
                               "rate": int(rate), "bits": 16, "channel": 1},
                },
            },
        }).encode("utf-8")

    def _raise_for_status(self, exc, language):
        detail = _error_detail(exc.read().decode("utf-8", errors="replace"))
        message = f"StepFun responded with HTTP {exc.code}: {detail}"
        if exc.code in (401, 403):
            raise TranscriptionError(
                f"{message}\nHint: the API key was rejected. Is it a key from "
                f"platform.stepfun.ai, and still valid?") from exc
        if exc.code == 402:
            raise TranscriptionError(
                f"{message}\nHint: the StepFun account has no balance left - "
                f"top it up in the StepFun console.") from exc
        if exc.code == 404:
            raise TranscriptionError(message) from exc
        if exc.code == 429 or exc.code >= 500:
            raise _Retryable(message, delay=_retry_after(exc)) from exc
        if exc.code == 400 and language not in SUPPORTED_LANGUAGES + ("auto",):
            message += ("\nHint: StepFun documents only Chinese, English, "
                        "Japanese, Korean, French and Spanish.")
        elif exc.code == 451:
            message += "\nHint: the passage did not pass StepFun's content review."
        raise _PassageError(message, status=exc.code) from exc

    # ------------------------------------------------------------------
    def cancel(self):
        """Abort at once; the waiting caller notices within CANCEL_POLL_S."""
        self._cancel_event.set()


# ----------------------------------------------------------------------
# Passages
# ----------------------------------------------------------------------
def find_passages(levels, frame_s=FRAME_S):
    """Where the speech of a track is, cut into passages.

    `levels` is the RMS of every frame of `frame_s` seconds. Returns
    (start, end, speech_start, speech_end) per passage, in frames and in
    order: what to send, and where the speech in it lies.

    A frame is speech from VOICE_RATIO of the track's speech level on. Pauses
    shorter than JOIN_S stay inside a passage, as long as it does not grow
    past MAX_PASSAGE_S; one stretch of speech that is longer anyway is cut in
    its quietest frame. Passages hold whole sentences that way, and a speaker
    change - a pause on this track while the other one talks - starts a new
    one.
    """
    levels = np.asarray(levels, dtype=np.float32)
    reference = dsp.reference_from_levels(levels)
    if reference <= dsp.SILENCE_FLOOR:
        return []
    voiced = levels >= reference * VOICE_RATIO

    def frames(seconds):
        return max(1, int(round(seconds / frame_s)))

    longest, search, join = frames(MAX_PASSAGE_S), frames(SPLIT_SEARCH_S), frames(JOIN_S)

    pieces = []
    for first, end in _runs(voiced):
        while end - first > longest:
            window = first + max(1, longest - search)
            cut = window + int(np.argmin(levels[window:first + longest])) + 1
            pieces.append([first, cut])
            first = cut
        pieces.append([first, end])

    joined = []
    for first, end in pieces:
        if joined and first - joined[-1][1] < join and end - joined[-1][0] <= longest:
            joined[-1][1] = end
        else:
            joined.append([first, end])
    joined = [(first, end) for first, end in joined
              if voiced[first:end].sum() * frame_s >= MIN_SPEECH_S]

    # Padding, but never into the neighbour: the same audio sent twice would
    # come back as the same words in two lines.
    pad = frames(PAD_S)
    passages = []
    for index, (first, end) in enumerate(joined):
        before = first if index == 0 else (first - joined[index - 1][1]) // 2
        after = (len(levels) - end if index == len(joined) - 1
                 else (joined[index + 1][0] - end) // 2)
        passages.append((first - min(pad, before), end + min(pad, after),
                         first, end))
    return passages


def _runs(mask):
    """(first, end) of every stretch of True in `mask`, end exclusive."""
    edges = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist()))


def _measure(path):
    """(rate, frame length, samples, level per frame) of a track.

    Read block by block: an hour of audio is never in memory at once. The
    backend needs nothing but these levels - and later each passage on its own.
    """
    rate = sf.info(path).samplerate
    frame_len = max(1, int(round(rate * FRAME_S)))
    levels = []
    total = 0
    for block in sf.blocks(path, blocksize=frame_len * 600, dtype="float32",
                           always_2d=True):
        mono = block.mean(axis=1)
        total += len(mono)
        whole = len(mono) // frame_len * frame_len
        levels.append(dsp.frame_rms(mono[:whole], frame_len))
        if whole < len(mono):                 # a short last frame counts too
            levels.append(np.array([dsp.rms(mono[whole:])], dtype=np.float32))
    if not levels:
        return rate, frame_len, 0, np.zeros(0, dtype=np.float32)
    return rate, frame_len, total, np.concatenate(levels)


def _read_pcm(path, start, end):
    """Samples [start, end) of a track as 16-bit little-endian mono PCM."""
    data, _rate = sf.read(path, start=start, stop=end, dtype="int16",
                          always_2d=True)
    mono = (data[:, 0] if data.shape[1] == 1
            else np.round(data.mean(axis=1)).astype(np.int16))
    return mono.astype("<i2").tobytes()


# ----------------------------------------------------------------------
# The answer
# ----------------------------------------------------------------------
def _read_answer(response):
    """(final text, pieces) from the event stream of one request."""
    pieces = []
    stray = []
    for data in _events(response, stray):
        if data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError as exc:
            raise _PassageError(f"StepFun sent an event that is not JSON: "
                                f"{data[:200]}") from exc
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "transcript.text.delta":
            pieces.append(_Piece(str(event.get("delta") or ""),
                                 _seconds(event.get("start_time")),
                                 _seconds(event.get("end_time")),
                                 event.get("item_id")))
        elif kind == "transcript.text.done":
            return str(event.get("text") or ""), pieces
        elif kind == "error":
            raise _PassageError(f"StepFun reported an error: {_event_error(event)}")
    if stray:
        raise _PassageError(f"StepFun sent an unexpected answer: "
                            f"{_error_detail(chr(10).join(stray))}")
    raise _Retryable("StepFun's answer ended before the transcript was complete.")


def _events(lines, stray):
    """The data of every server-sent event; lines that are no SSE go to `stray`."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
            continue
        field, colon, value = line.partition(":")
        if not colon or field not in ("", "data", "event", "id", "retry"):
            stray.append(line)
        elif field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


def _passage_segments(passage, rate, text, pieces, track=""):
    """The segments of one answered passage, on the track's timeline.

    Sentences at the service's times where it sent them for every word;
    otherwise the whole passage in one segment, timed by where its speech is.
    """
    offset = passage.start / rate
    sentences = _sentences(pieces, (passage.end - passage.start) / rate)
    if sentences:
        return [Segment(start=offset + start, end=offset + end, text=sentence,
                        track=track) for sentence, start, end in sentences]
    text = (text or "".join(piece.text for piece in pieces)).strip()
    if not text:
        return []
    return [Segment(start=passage.speech_start / rate,
                    end=passage.speech_end / rate, text=text, track=track)]


def _sentences(pieces, duration):
    """(text, start, end) per sentence, from deltas that carry times.

    Empty unless every delta with text has its times. A segment ends at
    sentence punctuation, after MAX_WORDS_PER_SEGMENT words, or where the
    service starts a new item.
    """
    spoken = [piece for piece in pieces if piece.text.strip()]
    if (not spoken or any(piece.start is None or piece.end is None
                          for piece in spoken)
            or max(piece.end for piece in spoken) <= 0):
        return []

    def clamp(seconds):
        return min(max(seconds, 0.0), duration)

    sentences = []
    buffer, words, start, end, item = [], 0, None, 0.0, None

    def flush():
        nonlocal buffer, words, start, end
        text = "".join(buffer).strip()
        if text:
            sentences.append((text, start, max(end, start)))
        buffer, words, start, end = [], 0, None, 0.0

    for piece in pieces:
        if piece.item is not None and piece.item != item:
            if item is not None:
                flush()
            item = piece.item
        buffer.append(piece.text)
        if piece.text.strip():
            if start is None:
                start = clamp(piece.start)
            end = clamp(piece.end)
            words += len(piece.text.split())
        if piece.text.rstrip().endswith(SENTENCE_ENDINGS) or words >= MAX_WORDS_PER_SEGMENT:
            flush()
    flush()
    return sentences


def _seconds(milliseconds):
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)):
        return None
    return milliseconds / 1000.0


def _event_error(event):
    error = event.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    return str(event.get("message") or error or "no details")


def _error_detail(text):
    """The message of an error body - {"error": {"message": ...}} and the
    like - or its first characters."""
    text = text.strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500] or "(no details)"
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        for key in ("message", "msg", "detail"):
            if data.get(key):
                return str(data[key])
        if isinstance(error, str) and error:
            return error
    return text[:500]


def _retry_after(exc):
    """The pause a Retry-After header asks for, in seconds; None without one."""
    try:
        seconds = float(exc.headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return None
    return min(max(0.0, seconds), MAX_RETRY_AFTER_S)
