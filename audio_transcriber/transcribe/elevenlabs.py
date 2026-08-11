"""ElevenLabs Scribe backend (cloud).

Fixes M8, H8 and the core of K1:
  * The previous version read the entire WAV into RAM and built a bytearray
    from it - roughly 700 MB peak for an hour of audio. The body is streamed
    now.
  * No timeout: a stalled connection blocked the thread indefinitely.
  * The multipart boundary was hard-coded; it is now generated randomly.
  * Words were joined with ' '.join() although the API returns its own
    'spacing' tokens, which produced double spaces and detached punctuation.
    The text is now assembled from the tokens.
  * model_id 'scribe_v2' is attempted first; if the API rejects the model the
    backend falls back to 'scribe_v1' instead of failing without explanation.
"""

import json
import os
import secrets
import urllib.error
import urllib.request

from .base import Backend, Segment, TranscriptionError

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"
REQUEST_TIMEOUT = 900          # 15 min - large files take a while
FALLBACK_MODEL = "scribe_v1"
MAX_WORDS_PER_SEGMENT = 18
SENTENCE_ENDINGS = (".", "?", "!", "…")


class _ChainedBody:
    """File-like object chaining byte blocks and an open file.

    http.client reads from it block by block, so the audio file never lands in
    memory as a whole."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._index = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = 1 << 20
        while self._index < len(self._chunks):
            chunk = self._chunks[self._index]
            if isinstance(chunk, (bytes, bytearray)):
                if chunk:
                    data = bytes(chunk[:size])
                    rest = chunk[size:]
                    self._chunks[self._index] = rest
                    if not rest:
                        self._index += 1
                    return data
                self._index += 1
                continue
            data = chunk.read(size)
            if data:
                return data
            try:
                chunk.close()
            except Exception:
                pass
            self._index += 1
        return b""

    def close(self):
        for chunk in self._chunks:
            if hasattr(chunk, "close"):
                try:
                    chunk.close()
                except Exception:
                    pass


class ElevenLabsBackend(Backend):
    name = "ElevenLabs Scribe"

    def __init__(self, api_key, model_id="scribe_v2", diarize=True,
                 tag_audio_events=True):
        self.api_key = (api_key or "").strip()
        self.model_id = model_id
        self.diarize = diarize
        self.tag_audio_events = tag_audio_events
        self._cancelled = False
        self._response = None       # live response, so cancel() can close it

    # ------------------------------------------------------------------
    def transcribe(self, wav_path, language="de", log=None, track="",
                   progress=None):
        if not self.api_key:
            raise TranscriptionError(
                "No ElevenLabs API key is configured. Enter it in the main "
                "window or set the ELEVENLABS_API_KEY environment variable.")
        if not os.path.exists(wav_path):
            raise TranscriptionError(f"Audio file not found: {wav_path}")

        size_mb = os.path.getsize(wav_path) / (1 << 20)
        self._log(log, f"Uploading {os.path.basename(wav_path)} "
                       f"({size_mb:.1f} MB) to ElevenLabs…\n")

        try:
            payload = self._post(wav_path, language, self.model_id)
        except TranscriptionError as exc:
            if self._looks_like_model_error(str(exc)) and self.model_id != FALLBACK_MODEL:
                self._log(log, f"Model '{self.model_id}' was rejected; "
                               f"trying '{FALLBACK_MODEL}'…\n")
                payload = self._post(wav_path, language, FALLBACK_MODEL)
                self.model_id = FALLBACK_MODEL
            else:
                raise

        return self._to_segments(payload, track)

    # ------------------------------------------------------------------
    def _post(self, wav_path, language, model_id):
        boundary = "----AudioTranscriber" + secrets.token_hex(16)
        fields = {
            "model_id": model_id,
            "tag_audio_events": "true" if self.tag_audio_events else "false",
            "diarize": "true" if self.diarize else "false",
        }
        if language and language != "auto":
            fields["language_code"] = language

        prefix = bytearray()
        for name, value in fields.items():
            prefix += f"--{boundary}\r\n".encode()
            prefix += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            prefix += f"{value}\r\n".encode()

        filename = os.path.basename(wav_path)
        prefix += f"--{boundary}\r\n".encode()
        prefix += (f'Content-Disposition: form-data; name="file"; '
                   f'filename="{filename}"\r\n').encode()
        prefix += b"Content-Type: audio/wav\r\n\r\n"
        suffix = f"\r\n--{boundary}--\r\n".encode()

        file_size = os.path.getsize(wav_path)
        content_length = len(prefix) + file_size + len(suffix)

        handle = open(wav_path, "rb")
        body = _ChainedBody([bytes(prefix), handle, suffix])

        request = urllib.request.Request(
            API_URL, data=body, method="POST",
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(content_length),
                "Accept": "application/json",
                "User-Agent": "AudioTranscriber/2.0",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                self._response = response
                if self._cancelled:
                    raise TranscriptionError("Transcription was cancelled.")
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1500]
            hint = ""
            if exc.code in (401, 403):
                hint = ("\nHint: the API key was rejected. Is it still valid "
                        "and enabled for speech to text?")
            elif exc.code == 429:
                hint = "\nHint: rate limit reached - try again later."
            raise TranscriptionError(
                f"ElevenLabs responded with HTTP {exc.code}:\n{detail}{hint}") from exc
        except urllib.error.URLError as exc:
            raise TranscriptionError(
                f"Could not reach ElevenLabs: {exc.reason}") from exc
        except OSError as exc:
            if self._cancelled:
                raise TranscriptionError("Transcription was cancelled.") from exc
            raise TranscriptionError(f"Upload failed: {exc}") from exc
        finally:
            self._response = None
            body.close()

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranscriptionError(
                f"ElevenLabs response was not valid JSON: {raw[:300]}") from exc

    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_model_error(message):
        lowered = message.lower()
        return ("model" in lowered
                and any(code in lowered for code in ("422", "400", "404"))) \
            or "model_id" in lowered

    # ------------------------------------------------------------------
    def _to_segments(self, payload, track):
        """Build sentences with timestamps from the word tokens.

        The API returns tokens of type 'word', 'spacing' and 'audio_event'.
        The text is assembled from the raw tokens so punctuation and spacing
        stay correct; segment boundaries appear at sentence punctuation or
        after MAX_WORDS_PER_SEGMENT words.
        """
        words = payload.get("words") or []
        if not words:
            text = (payload.get("text") or "").strip()
            if not text:
                return []
            return [Segment(start=0.0, end=0.0, text=text, track=track)]

        segments = []
        buffer = []
        word_count = 0
        seg_start = None
        seg_end = 0.0
        speaker = ""

        def flush():
            nonlocal buffer, word_count, seg_start, seg_end, speaker
            text = "".join(buffer).strip()
            if text and seg_start is not None:
                segments.append(Segment(start=seg_start,
                                        end=max(seg_end, seg_start),
                                        text=text, track=track,
                                        speaker_hint=speaker))
            buffer = []
            word_count = 0
            seg_start = None
            seg_end = 0.0        # must reset too, else the next segment
            speaker = ""         # inherits this one's end time

        for token in words:
            kind = token.get("type", "word")
            text = token.get("text", "")
            start = token.get("start")
            end = token.get("end")

            if kind == "spacing":
                if buffer:
                    buffer.append(text or " ")
                continue

            if start is not None and seg_start is None:
                seg_start = float(start)
            if end is not None:
                seg_end = float(end)
            if not speaker:
                speaker = str(token.get("speaker_id") or "")

            buffer.append(text)
            if kind == "word":
                word_count += 1

            stripped = text.rstrip()
            if stripped.endswith(SENTENCE_ENDINGS) or word_count >= MAX_WORDS_PER_SEGMENT:
                flush()

        flush()
        return segments

    def cancel(self):
        """Abort a running request.

        There is no clean interrupt for urlopen, so the socket is closed from
        underneath the reader: the blocked read raises OSError, which _post
        turns back into a cancellation. Without this the worker sat on the
        900 s timeout after the window had already closed.
        """
        self._cancelled = True
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
