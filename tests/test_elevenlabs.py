"""Tests for the ElevenLabs backend (audit findings M8, H8, K2)."""

import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest

import numpy as np
import soundfile as sf

from audio_transcriber.transcribe import elevenlabs
from audio_transcriber.transcribe.base import TranscriptionError

CAPTURED = {}
BEHAVIOUR = {"mode": "ok"}

WORDS_RESPONSE = {
    "text": "Hello world. How are you?",
    "words": [
        {"text": "Hello", "start": 0.0, "end": 0.4, "type": "word", "speaker_id": "speaker_0"},
        {"text": " ", "start": 0.4, "end": 0.45, "type": "spacing"},
        {"text": "world.", "start": 0.45, "end": 0.9, "type": "word", "speaker_id": "speaker_0"},
        {"text": " ", "start": 0.9, "end": 1.0, "type": "spacing"},
        {"text": "How", "start": 1.0, "end": 1.2, "type": "word", "speaker_id": "speaker_1"},
        {"text": " ", "start": 1.2, "end": 1.25, "type": "spacing"},
        {"text": "are", "start": 1.25, "end": 1.5, "type": "word", "speaker_id": "speaker_1"},
        {"text": " ", "start": 1.5, "end": 1.55, "type": "spacing"},
        {"text": "you?", "start": 1.55, "end": 2.1, "type": "word", "speaker_id": "speaker_1"},
    ],
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        CAPTURED["content_length"] = length
        CAPTURED["actual_length"] = len(body)
        CAPTURED["content_type"] = self.headers.get("Content-Type", "")
        CAPTURED["api_key"] = self.headers.get("xi-api-key", "")
        CAPTURED["body"] = body

        if BEHAVIOUR["mode"] == "model_error" and b"scribe_v2" in body:
            payload = json.dumps({"detail": {"message": "model_id not found"}}).encode()
            self.send_response(422)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if BEHAVIOUR["mode"] == "unauthorized":
            payload = b'{"detail":"invalid api key"}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        payload = json.dumps(WORDS_RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class ElevenLabsTestCase(unittest.TestCase):
    def setUp(self):
        CAPTURED.clear()
        BEHAVIOUR["mode"] = "ok"
        self.dir = tempfile.mkdtemp()
        self.wav = os.path.join(self.dir, "recording.wav")
        sf.write(self.wav, np.zeros(16000 * 3, dtype=np.float32), 16000,
                 subtype="PCM_16")

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self._original_url = elevenlabs.API_URL
        elevenlabs.API_URL = f"http://127.0.0.1:{self.httpd.server_port}/v1/speech-to-text"

    def tearDown(self):
        elevenlabs.API_URL = self._original_url
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestChainedBody(unittest.TestCase):
    def test_streams_prefix_file_suffix_exactly(self):
        """Regression M8: the previous version assembled the whole body as a
        bytearray in RAM (roughly 700 MB peak for an hour of audio)."""
        directory = tempfile.mkdtemp()
        try:
            path = os.path.join(directory, "data.bin")
            payload = bytes(range(256)) * 400
            with open(path, "wb") as handle:
                handle.write(payload)

            handle = open(path, "rb")
            body = elevenlabs._ChainedBody([b"PREFIX", handle, b"SUFFIX"])
            chunks = []
            while True:
                chunk = body.read(8192)
                if not chunk:
                    break
                chunks.append(chunk)
            self.assertEqual(b"".join(chunks), b"PREFIX" + payload + b"SUFFIX")
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_small_reads(self):
        body = elevenlabs._ChainedBody([b"abc", b"de"])
        self.assertEqual(body.read(2), b"ab")
        self.assertEqual(body.read(2), b"c")
        self.assertEqual(body.read(10), b"de")
        self.assertEqual(body.read(10), b"")


class TestRequest(ElevenLabsTestCase):
    def test_multipart_is_well_formed(self):
        backend = elevenlabs.ElevenLabsBackend(api_key="sk_test", model_id="scribe_v2")
        backend.transcribe(self.wav, language="de")

        self.assertEqual(CAPTURED["api_key"], "sk_test")
        self.assertEqual(CAPTURED["content_length"], CAPTURED["actual_length"])
        self.assertIn("multipart/form-data; boundary=", CAPTURED["content_type"])

        body = CAPTURED["body"]
        self.assertIn(b'name="model_id"', body)
        self.assertIn(b"scribe_v2", body)
        self.assertIn(b'name="diarize"', body)
        self.assertIn(b'name="language_code"', body)
        self.assertIn(b"\r\nde\r\n", body)
        self.assertIn(b'filename="recording.wav"', body)
        self.assertTrue(body.rstrip().endswith(b"--"))

    def test_boundary_is_random(self):
        backend = elevenlabs.ElevenLabsBackend(api_key="sk_test")
        backend.transcribe(self.wav)
        first = CAPTURED["content_type"]
        backend.transcribe(self.wav)
        self.assertNotEqual(first, CAPTURED["content_type"])

    def test_language_auto_is_omitted(self):
        elevenlabs.ElevenLabsBackend(api_key="sk_test").transcribe(self.wav,
                                                                   language="auto")
        self.assertNotIn(b'name="language_code"', CAPTURED["body"])


class TestResponseParsing(ElevenLabsTestCase):
    def test_words_are_joined_without_double_spaces(self):
        """Regression M8: ' '.join() across all tokens produced double spaces
        and detached punctuation."""
        segments = elevenlabs.ElevenLabsBackend(api_key="sk_test").transcribe(self.wav)
        texts = [segment.text for segment in segments]
        self.assertEqual(texts, ["Hello world.", "How are you?"])
        for text in texts:
            self.assertNotIn("  ", text)
            self.assertNotIn(" .", text)
            self.assertNotIn(" ?", text)

    def test_timestamps_and_speaker_hints(self):
        segments = elevenlabs.ElevenLabsBackend(api_key="sk_test").transcribe(self.wav)
        self.assertAlmostEqual(segments[0].start, 0.0)
        self.assertAlmostEqual(segments[0].end, 0.9)
        self.assertAlmostEqual(segments[1].start, 1.0)
        self.assertEqual(segments[0].speaker_hint, "speaker_0")
        self.assertEqual(segments[1].speaker_hint, "speaker_1")


class TestErrorHandling(ElevenLabsTestCase):
    def test_missing_key_is_reported_clearly(self):
        with self.assertRaises(TranscriptionError) as ctx:
            elevenlabs.ElevenLabsBackend(api_key="").transcribe(self.wav)
        self.assertIn("API key", str(ctx.exception))

    def test_unauthorized_gives_actionable_message(self):
        """Regression K2: in the previous version every error message vanished
        into a NameError and the user saw nothing at all."""
        BEHAVIOUR["mode"] = "unauthorized"
        with self.assertRaises(TranscriptionError) as ctx:
            elevenlabs.ElevenLabsBackend(api_key="sk_wrong").transcribe(self.wav)
        message = str(ctx.exception)
        self.assertIn("401", message)
        self.assertIn("rejected", message)

    def test_model_fallback_to_scribe_v1(self):
        """If the API rejects scribe_v2, scribe_v1 is tried automatically."""
        BEHAVIOUR["mode"] = "model_error"
        backend = elevenlabs.ElevenLabsBackend(api_key="sk_test", model_id="scribe_v2")
        segments = backend.transcribe(self.wav)
        self.assertTrue(segments)
        self.assertEqual(backend.model_id, "scribe_v1")
        self.assertIn(b"scribe_v1", CAPTURED["body"])

    def test_missing_file(self):
        with self.assertRaises(TranscriptionError):
            elevenlabs.ElevenLabsBackend(api_key="sk_test").transcribe(
                os.path.join(self.dir, "does-not-exist.wav"))


if __name__ == "__main__":
    unittest.main()
