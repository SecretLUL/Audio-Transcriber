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
        from audio_transcriber.ui import icons
        icons._ICON_CACHE.clear()
        self.tmpdir = tempfile.mkdtemp()
        self._orig_cfg = config.CFG_PATH
        # Never touch the user's real settings
        config.CFG_PATH = os.path.join(self.tmpdir, "settings.json")
        self.paths = paths

    def tearDown(self):
        import shutil
        from audio_transcriber import config
        from audio_transcriber.ui import icons
        icons._ICON_CACHE.clear()
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

    def test_gain_slider_updates_meters(self):
        """Regression test: gain sliders must dynamically adjust the VU meter levels."""
        from audio_transcriber.ui.app import RecorderApp
        from unittest.mock import PropertyMock, patch

        root = tk.Tk()
        root.withdraw()
        app = None
        try:
            app = RecorderApp(root)
            with patch.object(type(app.engine), 'mic_level', new_callable=PropertyMock) as mock_mic:
                mock_mic.return_value = 0.1  # ~ -20 dB

                # 0 dB Gain -> ~ -20 dB
                app._on_mic_gain(0.0)
                app._tick()
                db_0 = app.mic_meter.db
                self.assertAlmostEqual(db_0, -20.0, delta=1.0)

                # +10 dB Gain -> ~ -10 dB (+10 dB shift)
                app._on_mic_gain(10.0)
                app._tick()
                db_plus_10 = app.mic_meter.db
                self.assertAlmostEqual(db_plus_10, -10.0, delta=1.0)

                # -10 dB Gain -> ~ -30 dB (-10 dB shift)
                app._on_mic_gain(-10.0)
                app._tick()
                db_minus_10 = app.mic_meter.db
                self.assertAlmostEqual(db_minus_10, -30.0, delta=1.0)

                self.assertGreater(db_plus_10, db_0)
                self.assertLess(db_minus_10, db_0)
        finally:
            if app is not None:
                app.on_close()
            else:
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
