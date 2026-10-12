"""Tests for the StepFun backend: how a track is cut into passages, what is
sent for each, how the event stream is read, and how the requests behave -
retrying, refusals, cancelling and working in parallel.

A local server stands in for the endpoint; nothing goes on the network.
"""

import base64
import http.server
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from unittest.mock import patch

import numpy as np
import soundfile as sf

from audio_transcriber import config, pipeline
from audio_transcriber.audio import capture
from audio_transcriber.events import Failed, Finished
from audio_transcriber.transcribe import stepfun
from audio_transcriber.transcribe.base import TranscriptionError
from tests.test_live import FakeBridge, FakeEngine

RATE = 16000

Request = namedtuple("Request", "path authorization accept content_type body pcm")


def sse(*events):
    """A server-sent event stream of these JSON events."""
    return b"".join(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
                    for event in events)


def delta(text, start_ms=None, end_ms=None, item=None):
    event = {"type": "transcript.text.delta", "delta": text,
             "meta": {"session_id": "sse_1", "timestamp": 1}}
    if start_ms is not None:
        event.update(start_time=start_ms, end_time=end_ms)
    if item is not None:
        event["item_id"] = item
    return event


def done(text):
    return {"type": "transcript.text.done", "text": text,
            "meta": {"session_id": "sse_1", "timestamp": 2}}


def answer(text="Hello there."):
    """The plain answer: one delta, then the whole text."""
    return 200, {}, sse(delta(text), done(text))


def write_track(path, seconds, bursts, level=0.1, seed=3):
    """A track of silence with noise where someone speaks.

    `bursts` are (start, end) in seconds; on whole tenths they coincide with
    the frames the backend measures, so the times can be checked exactly.
    """
    rng = np.random.default_rng(seed)
    audio = np.zeros(int(round(seconds * RATE)), dtype=np.float32)
    for start, end in bursts:
        first, last = int(round(start * RATE)), int(round(end * RATE))
        audio[first:last] = np.clip(rng.normal(0, level, last - first), -0.9, 0.9)
    sf.write(path, audio, RATE, subtype="PCM_16")
    return sf.read(path, dtype="int16")[0]


class _Server:
    """The endpoint, answering every request through `respond(request, number)`."""

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self.lock = threading.Lock()
        self.active = 0
        self.most_active = 0
        self.release = threading.Event()     # ends a request that stalls
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                request = Request(self.path, self.headers.get("Authorization"),
                                  self.headers.get("Accept"),
                                  self.headers.get("Content-Type"), body,
                                  base64.b64decode(body["audio"]["data"]))
                with server.lock:
                    server.requests.append(request)
                    number = len(server.requests)
                    server.active += 1
                    server.most_active = max(server.most_active, server.active)
                try:
                    reply = server.respond(request, number)
                    if reply == "stall":
                        server.release.wait(30)
                        return
                    status, headers, payload = reply
                    self.send_response(status)
                    self.send_header("Content-Type", headers.pop(
                        "Content-Type", "text/event-stream" if status == 200
                        else "application/json"))
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                finally:
                    with server.lock:
                        server.active -= 1

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, args=(0.01,),
                         daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/audio/asr/sse"

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class StepFunTestCase(unittest.TestCase):
    respond = staticmethod(lambda request, number: answer())

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.server = _Server(lambda request, number: self.respond(request, number))
        self.addCleanup(self.server.close)
        for name, value in (("API_URL", self.server.url),
                            ("RETRY_DELAYS", (0.0,) * 5)):
            patcher = patch.object(stepfun, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.wav = os.path.join(self.dir, "track.wav")
        self.samples = write_track(self.wav, 10.0, [(1.0, 3.0), (6.0, 8.0)])

    def backend(self, **options):
        return stepfun.StepFunBackend(api_key=options.pop("api_key", "sk_test"),
                                      **options)

    def transcribe(self, backend=None, **options):
        options.setdefault("language", "en")
        return (backend or self.backend()).transcribe(self.wav, **options)

    def requests(self):
        with self.server.lock:
            return list(self.server.requests)


# ----------------------------------------------------------------------
class TestRequest(StepFunTestCase):
    def test_it_is_what_the_reference_documents(self):
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        self.transcribe(language="en")

        (request,) = self.requests()
        self.assertEqual(request.path, "/v1/audio/asr/sse")
        self.assertEqual(request.authorization, "Bearer sk_test")
        self.assertEqual(request.accept, "text/event-stream")
        self.assertEqual(request.content_type, "application/json")
        sent = request.body["audio"]["input"]
        self.assertEqual(sent["transcription"],
                         {"model": "stepaudio-3-asr-max", "language": "en",
                          "enable_itn": True, "enable_timestamp": True})
        self.assertEqual(sent["format"], {"type": "pcm", "codec": "pcm_s16le",
                                          "rate": 16000, "bits": 16, "channel": 1})

    def test_the_audio_is_the_passage_exactly_with_some_air_around_it(self):
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        self.transcribe()
        (request,) = self.requests()
        first, last = int(0.8 * RATE), int(3.2 * RATE)         # PAD_S each side
        self.assertEqual(request.pcm, self.samples[first:last].astype("<i2").tobytes())

    def test_automatic_detection_sends_no_language(self):
        self.transcribe(language="auto")
        for request in self.requests():
            self.assertNotIn("language", request.body["audio"]["input"]["transcription"])

    def test_the_model_can_be_chosen(self):
        self.transcribe(backend=self.backend(model_id="stepaudio-2.5-asr"))
        models = {request.body["audio"]["input"]["transcription"]["model"]
                  for request in self.requests()}
        self.assertEqual(models, {"stepaudio-2.5-asr"})

    def test_a_stereo_file_is_sent_as_one_channel(self):
        stereo = np.zeros((4 * RATE, 2), dtype=np.float32)
        stereo[RATE:3 * RATE, 0] = 0.2
        stereo[RATE:3 * RATE, 1] = 0.4
        sf.write(self.wav, stereo, RATE, subtype="PCM_16")
        self.transcribe()
        (request,) = self.requests()
        pcm = np.frombuffer(request.pcm, dtype="<i2")
        self.assertAlmostEqual(pcm.max() / 32768.0, 0.3, places=2)
        self.assertEqual(request.body["audio"]["input"]["format"]["channel"], 1)


class TestPassages(StepFunTestCase):
    def test_every_stretch_of_speech_is_a_request_and_a_line(self):
        segments = self.transcribe()
        self.assertEqual(len(self.requests()), 2)
        self.assertEqual([(s.start, s.end) for s in segments],
                         [(1.0, 3.0), (6.0, 8.0)])
        self.assertEqual([s.text for s in segments], ["Hello there."] * 2)

    def test_the_track_is_passed_on(self):
        segments = self.transcribe(track="sys")
        self.assertEqual({segment.track for segment in segments}, {"sys"})

    def test_silence_sends_nothing(self):
        write_track(self.wav, 5.0, [])
        logs = []
        self.assertEqual(self.transcribe(log=logs.append), [])
        self.assertEqual(self.requests(), [])
        self.assertIn("No speech", "".join(logs))

    def test_the_log_says_what_is_sent_and_progress_counts_the_passages(self):
        logs, progress = [], []
        self.transcribe(log=logs.append, progress=progress.append)
        self.assertIn("2 passage(s)", "".join(logs))
        self.assertEqual(len(progress), 2)
        self.assertIn("2 of 2", progress[-1])
        self.assertIn("100 %", progress[-1])

    def test_a_long_track_is_measured_block_by_block(self):
        """An hour of audio is never read whole; the blocks have to add up."""
        write_track(self.wav, 61.05, [(10.0, 20.0)])
        rate, frame_len, total, levels = stepfun._measure(self.wav)
        self.assertEqual((rate, frame_len, total), (RATE, 1600, round(61.05 * RATE)))
        self.assertEqual(len(levels), 611)           # the short last frame counts
        self.assertGreater(levels[150], 0.05)
        self.assertEqual(levels[300], 0.0)


class TestFindPassages(unittest.TestCase):
    """find_passages works on levels per 100 ms frame."""

    QUIET, LOUD = 1e-4, 0.1

    def levels(self, *pattern):
        """('L', 2.0), ('Q', 0.5), ... -> a level per frame."""
        parts = [np.full(int(round(seconds * 10)), self.LOUD if kind == "L" else self.QUIET)
                 for kind, seconds in pattern]
        return np.concatenate(parts)

    @staticmethod
    def seconds(passages):
        return [tuple(round(frame / 10.0, 1) for frame in passage)
                for passage in passages]

    def test_a_long_pause_separates_two_passages(self):
        passages = stepfun.find_passages(self.levels(
            ("Q", 1.0), ("L", 2.0), ("Q", 3.0), ("L", 1.0), ("Q", 1.0)))
        self.assertEqual(self.seconds(passages),
                         [(0.8, 3.2, 1.0, 3.0), (5.8, 7.2, 6.0, 7.0)])

    def test_a_short_pause_stays_inside_the_passage(self):
        passages = stepfun.find_passages(self.levels(
            ("Q", 1.0), ("L", 2.0), ("Q", 0.5), ("L", 1.0), ("Q", 1.0)))
        self.assertEqual(self.seconds(passages), [(0.8, 4.7, 1.0, 4.5)])

    def test_a_click_is_no_speech(self):
        passages = stepfun.find_passages(self.levels(
            ("Q", 1.0), ("L", 2.0), ("Q", 3.0), ("L", 0.1), ("Q", 3.0)))
        self.assertEqual(len(passages), 1)

    def test_silence_has_no_passages(self):
        self.assertEqual(stepfun.find_passages(np.zeros(100)), [])
        self.assertEqual(stepfun.find_passages(np.zeros(0)), [])

    def test_long_speech_is_cut_in_its_quietest_frame(self):
        levels = self.levels(("Q", 1.0), ("L", 70.0), ("Q", 1.0))
        dip = 10 + 270                                # 27 s into the speech
        levels[dip] = self.LOUD * 0.3
        passages = stepfun.find_passages(levels)
        self.assertEqual(passages[0][3], dip + 1, "cut right after the dip")
        for start, end, speech_start, speech_end in passages:
            self.assertLessEqual(speech_end - speech_start, 300)
        self.assertEqual(passages[0][2], 10)
        self.assertEqual(passages[-1][3], 10 + 700)
        # No air at a cut: the next passage starts where this one ends.
        self.assertEqual(passages[0][1], dip + 1)
        self.assertEqual(passages[1][0], dip + 1)

    def test_the_air_around_a_passage_stops_half_way_to_its_neighbour(self):
        """Joining these two would make a passage longer than allowed."""
        passages = stepfun.find_passages(self.levels(
            ("Q", 1.0), ("L", 20.0), ("Q", 0.3), ("L", 20.0), ("Q", 1.0)))
        self.assertEqual(len(passages), 2)
        (_s1, end, _a, first_speech_end), (start, _e, second_speech, _b) = passages
        self.assertEqual((first_speech_end, second_speech), (210, 213))
        self.assertEqual((end, start), (211, 212))

    def test_nothing_is_sent_twice_and_no_speech_is_left_out(self):
        rng = np.random.default_rng(11)
        pattern = []
        for _ in range(60):
            # Up to 45 s of speech, so that some are cut and some cannot be
            # joined across a short pause.
            pattern += [("L", float(rng.integers(1, 450)) / 10.0),
                        ("Q", float(rng.integers(1, 40)) / 10.0)]
        levels = self.levels(*pattern)
        passages = stepfun.find_passages(levels)

        for (_s, end, _a, _b), (start, _e, _c, _d) in zip(passages, passages[1:]):
            self.assertLessEqual(end, start, "two passages overlap")
        covered = np.zeros(len(levels), dtype=bool)
        for start, end, speech_start, speech_end in passages:
            self.assertLessEqual(start, speech_start)
            self.assertLessEqual(speech_end, end)
            covered[speech_start:speech_end] = True
        voiced = levels > self.QUIET
        # Only stretches too short to be speech may be missing.
        missing = voiced & ~covered
        self.assertLessEqual(missing.sum(), 1 * sum(1 for kind, s in pattern
                                                    if kind == "L" and s < 0.2))


class TestAnswer(StepFunTestCase):
    def setUp(self):
        super().setUp()
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])

    def test_times_from_the_service_split_the_passage_into_sentences(self):
        self.respond = lambda request, number: (200, {}, sse(
            delta("Hello", 200, 600, "item_1"), delta(" world.", 600, 1000, "item_1"),
            delta(" How are", 1100, 1600, "item_1"), delta(" you?", 1600, 2000, "item_1"),
            done("Hello world. How are you?")))
        segments = self.transcribe()
        # The passage starts at 0.8 s; the times are seconds into it.
        self.assertEqual([(s.text, round(s.start, 2), round(s.end, 2)) for s in segments],
                         [("Hello world.", 1.0, 1.8), ("How are you?", 1.9, 2.8)])

    def test_a_new_item_starts_a_new_line(self):
        self.respond = lambda request, number: (200, {}, sse(
            delta("first part", 0, 800, "a"), delta(" second part", 900, 1800, "b"),
            done("first part second part")))
        segments = self.transcribe()
        self.assertEqual([s.text for s in segments], ["first part", "second part"])

    def test_without_times_the_passage_is_one_line_at_its_speech(self):
        self.respond = lambda request, number: (200, {}, sse(
            delta("Hello "), delta("world."), done("Hello world.")))
        (segment,) = self.transcribe()
        self.assertEqual((segment.start, segment.end, segment.text),
                         (1.0, 3.0, "Hello world."))

    def test_the_final_text_is_preferred_to_the_deltas(self):
        """With inverse text normalisation it may differ from them."""
        self.respond = lambda request, number: (200, {}, sse(
            delta("twenty five euros"), done("25 €")))
        self.assertEqual(self.transcribe()[0].text, "25 €")

    def test_without_a_final_text_the_deltas_are_joined(self):
        self.respond = lambda request, number: (200, {}, sse(
            delta("Hello "), delta("again"), {"type": "transcript.text.done"}))
        self.assertEqual(self.transcribe()[0].text, "Hello again")

    def test_nothing_recognised_is_no_line(self):
        self.respond = lambda request, number: (200, {}, sse(done("")))
        self.assertEqual(self.transcribe(), [])

    def test_comments_event_names_and_a_closing_marker_are_fine(self):
        payload = (b": keep-alive\n\nevent: message\n"
                   + sse(delta("Hi.")) + b"data: [DONE]\n\n" + sse(done("Hi.")))
        self.respond = lambda request, number: (200, {}, payload)
        self.assertEqual(self.transcribe()[0].text, "Hi.")

    def test_times_beyond_the_passage_are_held_inside_it(self):
        self.respond = lambda request, number: (200, {}, sse(
            delta("Late.", 1000, 99000), done("Late.")))
        (segment,) = self.transcribe()
        self.assertAlmostEqual(segment.end, 3.2)               # end of the passage


class TestErrors(StepFunTestCase):
    def test_a_missing_key_is_reported_clearly(self):
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe(backend=self.backend(api_key="  "))
        self.assertIn("API key", str(ctx.exception))
        self.assertIn("STEPFUN_API_KEY", str(ctx.exception))
        self.assertEqual(self.requests(), [])

    def test_a_missing_file(self):
        with self.assertRaises(TranscriptionError):
            self.backend().transcribe(os.path.join(self.dir, "missing.wav"))

    def test_a_rejected_key_ends_the_run_at_once(self):
        write_track(self.wav, 60.0, [(t, t + 1.0) for t in range(1, 60, 3)])
        self.respond = lambda request, number: (
            401, {}, b'{"error": {"message": "Incorrect API key provided"}}')
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe()
        message = str(ctx.exception)
        self.assertIn("401", message)
        self.assertIn("Incorrect API key provided", message)
        self.assertIn("rejected", message)
        self.assertLessEqual(len(self.requests()), stepfun.PARALLEL_REQUESTS,
                             "no new passage after the first refusal")

    def test_an_empty_balance_says_so(self):
        self.respond = lambda request, number: (402, {}, b'{"error": {"message": "balance"}}')
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe()
        self.assertIn("no balance left", str(ctx.exception))

    def test_rate_limits_and_server_errors_are_retried(self):
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        replies = iter([(429, {"Retry-After": "0"}, b"{}"), (503, {}, b"{}"),
                        answer("Finally.")])
        self.respond = lambda request, number: next(replies)
        self.assertEqual(self.transcribe()[0].text, "Finally.")
        self.assertEqual(len(self.requests()), 3)

    def test_retry_after_is_honoured_but_capped(self):
        class Refusal:
            headers = {"Retry-After": "3600"}
        self.assertEqual(stepfun._retry_after(Refusal()), stepfun.MAX_RETRY_AFTER_S)
        Refusal.headers = {"Retry-After": "soon"}
        self.assertIsNone(stepfun._retry_after(Refusal()))

    def test_a_server_that_keeps_failing_is_reported_in_the_end(self):
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        self.respond = lambda request, number: (503, {}, b'{"message": "overloaded"}')
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe()
        self.assertIn("overloaded", str(ctx.exception))
        self.assertEqual(len(self.requests()), len(stepfun.RETRY_DELAYS) + 1)

    def test_an_unreachable_service_is_retried_then_reported(self):
        with patch("http.client.HTTPConnection.connect",
                   side_effect=ConnectionRefusedError(10061, "refused")):
            with self.assertRaises(TranscriptionError) as ctx:
                self.transcribe()
        self.assertIn("Could not reach StepFun", str(ctx.exception))

    def test_an_answer_that_breaks_off_is_asked_for_again(self):
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        replies = iter([(200, {}, sse(delta("Half"))), answer("Whole.")])
        self.respond = lambda request, number: next(replies)
        self.assertEqual(self.transcribe()[0].text, "Whole.")
        self.assertEqual(len(self.requests()), 2)

    def test_a_refusal_is_tried_once_more_without_timestamps(self):
        """Only the Chinese reference documents enable_timestamp."""
        def respond(request, number):
            if "enable_timestamp" in request.body["audio"]["input"]["transcription"]:
                return 400, {}, b'{"error": {"message": "unknown field"}}'
            return answer()
        self.respond = respond
        self.transcribe(language="en")

        asked = ["enable_timestamp" in r.body["audio"]["input"]["transcription"]
                 for r in self.requests()]
        self.assertIn(True, asked)
        self.assertFalse(asked[-1])
        self.assertLessEqual(asked.count(True), stepfun.PARALLEL_REQUESTS,
                             "once it works without, it is left out")

    def test_a_language_the_service_does_not_know_gets_a_hint(self):
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        self.respond = lambda request, number: (
            400, {}, b'{"error": {"message": "invalid language"}}')
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe(language="de")
        message = str(ctx.exception)
        self.assertIn("invalid language", message)
        self.assertIn("Chinese, English, Japanese, Korean, French and Spanish", message)

    def test_a_refused_passage_is_marked_and_the_rest_goes_on(self):
        self.samples = write_track(self.wav, 14.0, [(1.0, 3.0), (5.0, 8.0), (11.0, 12.0)])
        refused_len = int(3.4 * RATE) * 2                     # the 3 s passage

        def respond(request, number):
            if len(request.pcm) == refused_len:
                return 451, {}, b'{"error": {"message": "content review"}}'
            return answer("Fine.")
        self.respond = respond
        logs = []
        segments = self.transcribe(log=logs.append)

        self.assertEqual([s.text for s in segments][0::2], ["Fine.", "Fine."])
        marked = segments[1]
        self.assertEqual((marked.start, marked.end), (5.0, 8.0))
        self.assertTrue(marked.text.startswith("[not transcribed:"))
        self.assertIn("451", marked.text)
        self.assertIn("could not transcribe the passage at 00:05", "".join(logs))

    def test_when_every_passage_is_refused_the_run_fails(self):
        write_track(self.wav, 60.0, [(t, t + 1.0) for t in range(1, 60, 3)])
        self.respond = lambda request, number: (200, {}, sse(
            {"type": "error", "message": "audio could not be decoded"}))
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe()
        self.assertIn("audio could not be decoded", str(ctx.exception))
        self.assertLess(len(self.requests()),
                        stepfun.FAILURES_BEFORE_GIVING_UP + stepfun.PARALLEL_REQUESTS)

    def test_a_single_refused_passage_fails_the_run(self):
        """A transcript that only says 'not transcribed' helps nobody."""
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        self.respond = lambda request, number: (200, {}, sse(
            {"type": "error", "error": {"message": "risk_blocked"}}))
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe()
        self.assertIn("risk_blocked", str(ctx.exception))

    def test_a_failing_callback_ends_the_run_instead_of_hanging_it(self):
        def broken(message):
            raise RuntimeError("window gone")
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe(progress=broken)
        self.assertIn("window gone", str(ctx.exception))

    def test_an_answer_that_is_no_event_stream(self):
        self.samples = write_track(self.wav, 4.0, [(1.0, 3.0)])
        self.respond = lambda request, number: (
            200, {"Content-Type": "application/json"},
            b'{"error": {"message": "gateway says no"}}')
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe()
        self.assertIn("gateway says no", str(ctx.exception))
        self.assertEqual(len(self.requests()), 1, "not worth asking again")


class TestParallel(StepFunTestCase):
    def test_several_passages_are_on_the_way_at_once_but_not_too_many(self):
        write_track(self.wav, 40.0, [(t, t + 1.0) for t in range(1, 40, 3)])

        def respond(request, number):
            time.sleep(0.15)
            return answer(f"{len(request.pcm)}")
        self.respond = respond
        segments = self.transcribe()

        self.assertEqual(len(segments), 13)
        self.assertGreater(self.server.most_active, 1)
        self.assertLessEqual(self.server.most_active, stepfun.PARALLEL_REQUESTS)
        self.assertEqual([s.start for s in segments], sorted(s.start for s in segments))


class TestCancel(StepFunTestCase):
    def test_cancel_before_the_request_sends_nothing(self):
        backend = self.backend()
        backend.cancel()
        with self.assertRaises(TranscriptionError) as ctx:
            self.transcribe(backend=backend)
        self.assertIn("cancelled", str(ctx.exception))
        self.assertEqual(self.requests(), [])

    def test_cancel_ends_the_wait_for_the_service_at_once(self):
        self.respond = lambda request, number: "stall"
        backend = self.backend()
        outcome = {}

        def work():
            try:
                self.transcribe(backend=backend)
            except TranscriptionError as exc:
                outcome["error"] = str(exc)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while not self.requests() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.requests(), "no request arrived")

        started = time.monotonic()
        backend.cancel()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "cancel() did not end the wait")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn("cancelled", outcome["error"])

    def test_a_cancel_during_the_pause_between_attempts(self):
        self.respond = lambda request, number: (503, {}, b"{}")
        backend = self.backend()
        with patch.object(stepfun, "RETRY_DELAYS", (30.0,) * 5):
            thread = threading.Thread(
                target=lambda: self.assertRaises(TranscriptionError,
                                                 self.transcribe, backend),
                daemon=True)
            thread.start()
            deadline = time.monotonic() + 5.0
            while not self.requests() and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)                           # now it is waiting to retry
            backend.cancel()
            thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "the pause was not interrupted")


class TestInThePipeline(StepFunTestCase):
    """The real backend behind the closing pass and live transcription."""

    CAPTURE_RATE = 48000

    def setUp(self):
        super().setUp()
        self.tmp = os.path.join(self.dir, "tmp")
        os.makedirs(self.tmp)
        patcher = patch.object(pipeline, "TMP_DIR", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = config.Settings(model=config.STEPFUN_MODEL, language="en",
                                        output_dir=os.path.join(self.dir, "out"))
        self.settings.stepfun_api_key = "sk_test"
        # Says how long the passage was, so each line can be told apart.
        self.respond = lambda request, number: answer(
            f"{len(request.pcm) / 2 / RATE:.1f} seconds")

    def _speech(self, seconds, bursts, seed=5):
        rng = np.random.default_rng(seed)
        audio = np.zeros(int(seconds * self.CAPTURE_RATE), dtype=np.float32)
        for start, end in bursts:
            first, last = int(start * self.CAPTURE_RATE), int(end * self.CAPTURE_RATE)
            audio[first:last] = rng.normal(0, 0.2, last - first)
        return audio

    def test_the_closing_pass_makes_a_transcript_with_both_speakers(self):
        recording = capture.RecordingResult()
        for kind, bursts in (("mic", [(1.0, 3.0)]), ("sys", [(5.0, 8.0)])):
            path = os.path.join(self.tmp, f"meeting.{kind}.raw.wav")
            audio = self._speech(10.0, bursts)
            sf.write(path, audio, self.CAPTURE_RATE, subtype="PCM_16")
            setattr(recording, kind, capture.TrackResult(
                path=path, rate=self.CAPTURE_RATE, frames=len(audio),
                device_name=kind))

        bridge = FakeBridge()
        pipeline.Finalizer(bridge, self.settings)._process(recording, "meeting")

        self.assertEqual(bridge.of_type(Failed), [])
        text = bridge.of_type(Finished)[0].text
        self.assertIn("[00:01] [You]: 2.4 seconds", text)
        self.assertIn("[00:05] [Participant]: 3.4 seconds", text)
        self.assertEqual(len(self.requests()), 2)

    def test_live_chunks_go_to_stepfun_while_recording(self):
        engine = FakeEngine()
        live = pipeline.LiveTranscriber(FakeBridge(), self.settings, engine,
                                        chunk_s=4.0)
        live.start("meeting")
        engine.feed("mic", self._speech(9.0, [(1.0, 2.0), (5.0, 6.0)]),
                    rate=self.CAPTURE_RATE)

        deadline = time.monotonic() + 20.0
        while live.covered_s["mic"] < 6.0 and time.monotonic() < deadline:
            time.sleep(0.02)
        covered, segments = live.finish(timeout=20.0)

        self.assertGreaterEqual(covered["mic"], 6.0)
        starts = [segment.start for segment in segments["mic"]]
        self.assertEqual(len(starts), 2, segments)
        self.assertAlmostEqual(starts[0], 1.0, delta=0.15)
        self.assertAlmostEqual(starts[1], 5.0, delta=0.15)
        self.assertTrue(all(s.text.endswith("seconds") for s in segments["mic"]))
        self.assertEqual(len(self.requests()), 2, "silent chunks send nothing")


if __name__ == "__main__":
    unittest.main()
