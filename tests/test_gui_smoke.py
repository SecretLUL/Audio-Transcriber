"""Smoke test for the user interface.

Builds the complete window invisibly, checks that no exception is raised and
shuts it down cleanly. Covers audit finding M7 (the previous version had no
WM_DELETE_WINDOW handler and never terminated PyAudio).
"""

import os
import tempfile
import unittest

try:
    import tkinter as tk
    _HAS_TK = True
except Exception:                                   # pragma: no cover
    _HAS_TK = False


def _can_open_window():
    if not _HAS_TK:
        return False
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(_can_open_window(), "no graphical display available")
class TestAppLifecycle(unittest.TestCase):
    def setUp(self):
        from audio_transcriber import config, paths
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cfg = config.CFG_PATH
        # Never touch the user's real settings
        config.CFG_PATH = os.path.join(self.tmpdir, "settings.json")
        self.paths = paths

    def tearDown(self):
        import shutil
        from audio_transcriber import config
        config.CFG_PATH = self._orig_cfg
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_and_close(self):
        from audio_transcriber import config
        from audio_transcriber.ui.app import RecorderApp

        root = tk.Tk()
        root.withdraw()                     # build it invisibly
        app = None
        try:
            app = RecorderApp(root)
            root.update()                   # run one event cycle

            self.assertTrue(app.model_combo["values"])
            self.assertTrue(app.lang_combo["values"])
            self.assertEqual(str(app.start_btn["state"]), "normal")
            self.assertEqual(str(app.stop_btn["state"]), "disabled")

            # Collect the settings from the interface
            app._sync_settings_from_ui()
            self.assertIsInstance(app.settings.mic_gain_db, float)
            self.assertIn(app.settings.language,
                          [code for _label, code in config.LANGUAGE_CHOICES])

            # Tick the level meters once
            app._tick()
            root.update()
        finally:
            if app is not None:
                app.on_close()
            else:                                        # pragma: no cover
                root.destroy()

    def test_output_name_sanitising(self):
        """Path traversal through the file name field must be impossible."""
        cases = {
            "..\\..\\windows\\system32\\evil": "evil",
            "my_meeting.wav": "my_meeting",
            "  ": "my_meeting",
            "": "my_meeting",
            "C:/temp/report.wav": "report",
            'in<va>lid:"|?*': "in_va_lid_____",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(self.paths.safe_output_name(raw), expected)


if __name__ == "__main__":
    unittest.main()
