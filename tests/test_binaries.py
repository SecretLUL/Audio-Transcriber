"""Tests for the download and extraction layer (audit findings H8 and M2)."""

import http.server
import os
import shutil
import tempfile
import threading
import unittest
import zipfile

from audio_transcriber.transcribe import binaries

PAYLOAD = b"x" * (256 * 1024)


class _Handler(http.server.BaseHTTPRequestHandler):
    """A server with deliberately broken responses."""

    def log_message(self, *args):
        pass

    def _send(self, body, length=None, status=200):
        self.send_response(status)
        self.send_header("Content-Length",
                         str(length if length is not None else len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ok":
            self._send(PAYLOAD)
        elif self.path == "/truncated":
            # Announces the full length but delivers only half of it
            self._send(PAYLOAD[:len(PAYLOAD) // 2], length=len(PAYLOAD))
        elif self.path == "/missing":
            self._send(b"not found", status=404)
        else:
            self._send(b"", status=400)


class _Server:
    def __enter__(self):
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_successful_download(self):
        dest = os.path.join(self.dir, "model.bin")
        seen = []
        with _Server() as base:
            binaries.download(f"{base}/ok", dest, "Test file", progress=seen.append)
        self.assertEqual(os.path.getsize(dest), len(PAYLOAD))
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_truncated_download_leaves_no_file(self):
        """Regression H8: the previous version wrote straight to the target
        file. An abort left a partial file behind that passed as a valid model
        on the next start."""
        dest = os.path.join(self.dir, "model.bin")
        with _Server() as base:
            with self.assertRaises(binaries.DownloadError) as ctx:
                binaries.download(f"{base}/truncated", dest, "Test file")
        self.assertIn("incompletely", str(ctx.exception))
        self.assertFalse(os.path.exists(dest))
        self.assertFalse(os.path.exists(dest + ".part"))

    def test_http_error_is_reported(self):
        dest = os.path.join(self.dir, "model.bin")
        with _Server() as base:
            with self.assertRaises(binaries.DownloadError) as ctx:
                binaries.download(f"{base}/missing", dest, "Test file")
        self.assertIn("404", str(ctx.exception))
        self.assertFalse(os.path.exists(dest))

    def test_unreachable_host_is_reported(self):
        dest = os.path.join(self.dir, "model.bin")
        with self.assertRaises(binaries.DownloadError):
            binaries.download("http://127.0.0.1:1/nothing", dest, "Test file",
                              timeout=2)

    def test_stale_part_file_is_replaced(self):
        dest = os.path.join(self.dir, "model.bin")
        with open(dest + ".part", "wb") as handle:
            handle.write(b"garbage")
        with _Server() as base:
            binaries.download(f"{base}/ok", dest, "Test file")
        self.assertEqual(os.path.getsize(dest), len(PAYLOAD))


class TestSafeExtract(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_normal_archive(self):
        zip_path = os.path.join(self.dir, "good.zip")
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("whisper-cli.exe", b"MZ")
            archive.writestr("subfolder/whisper.dll", b"MZ")
        target = os.path.join(self.dir, "bin")
        os.makedirs(target)
        binaries.safe_extract(zip_path, target)
        self.assertTrue(os.path.exists(os.path.join(target, "whisper-cli.exe")))

    def test_path_traversal_is_rejected(self):
        """Regression M2 (zip slip): extractall() of the previous version
        would have placed this file outside the target directory."""
        zip_path = os.path.join(self.dir, "evil.zip")
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("harmless.txt", b"ok")
            archive.writestr("../../escaped.txt", b"pwned")
        target = os.path.join(self.dir, "bin")
        os.makedirs(target)

        with self.assertRaises(binaries.DownloadError) as ctx:
            binaries.safe_extract(zip_path, target)
        self.assertIn("outside", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "..", "escaped.txt")))

    def test_absolute_path_is_rejected(self):
        zip_path = os.path.join(self.dir, "absolute.zip")
        with zipfile.ZipFile(zip_path, "w") as archive:
            info = zipfile.ZipInfo("C:/Windows/Temp/evil.txt")
            archive.writestr(info, b"pwned")
        target = os.path.join(self.dir, "bin")
        os.makedirs(target)
        # Depending on normalisation either rejected or kept inside the target
        try:
            binaries.safe_extract(zip_path, target)
        except binaries.DownloadError:
            return
        for root, _dirs, files in os.walk(target):
            for name in files:
                self.assertTrue(os.path.realpath(os.path.join(root, name))
                                .startswith(os.path.realpath(target)))


if __name__ == "__main__":
    unittest.main()
