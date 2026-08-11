"""Tests for the recording timeline (audit finding H3)."""

import os
import shutil
import tempfile
import unittest

import numpy as np
import soundfile as sf

from audio_transcriber.audio import capture, dsp

RATE = 48000
BLOCK = capture.BLOCK_FRAMES


class TestDriftCorrection(unittest.TestCase):
    """Both track timelines must stay tied to the system clock.

    In the previous version lost blocks (exception_on_overflow=False) were
    swallowed silently. Since both tracks were read independently they drifted
    apart - and the whole speaker attribution relied on the assumption that
    sample index equals time.
    """

    def test_no_correction_when_in_sync(self):
        elapsed = 100 * BLOCK / RATE
        written = 99 * BLOCK
        self.assertEqual(capture.drift_deficit(elapsed, RATE, written, BLOCK), 0)

    def test_dropout_produces_positive_deficit(self):
        # 500 ms have passed but only 100 ms were written
        deficit = capture.drift_deficit(0.5, RATE, int(0.1 * RATE), 0)
        self.assertGreater(deficit, 0)
        self.assertAlmostEqual(deficit / RATE, 0.4, places=2)

    def test_small_jitter_is_ignored(self):
        # 20 ms of deviation is below the 50 ms threshold
        deficit = capture.drift_deficit(0.52, RATE, int(0.5 * RATE), 0)
        self.assertEqual(deficit, 0)

    def test_device_clock_ahead_is_reported_negative(self):
        deficit = capture.drift_deficit(0.5, RATE, int(0.7 * RATE), 0)
        self.assertLess(deficit, 0)

    def test_correction_keeps_tracks_aligned(self):
        """Simulation: track A loses 300 ms halfway through, track B does not.
        Without correction A ends 300 ms early - with correction they match."""
        total_blocks = 200
        block_s = BLOCK / RATE
        dropout_s = 0.3

        for corrected in (False, True):
            frames = 0
            elapsed = 0.0
            for index in range(total_blocks):
                # The wall clock keeps running; during a dropout time passes
                # without samples arriving.
                elapsed += block_s
                if index == 100:
                    elapsed += dropout_s
                if corrected:
                    deficit = capture.drift_deficit(elapsed, RATE, frames, BLOCK)
                    if deficit > 0:
                        frames += deficit
                frames += BLOCK

            # On the common timeline the track should be as long as the wall
            # clock says.
            drift_s = abs(frames / RATE - elapsed)
            if corrected:
                self.assertLess(drift_s, 0.02,
                                "the correction does not compensate the dropout")
            else:
                self.assertAlmostEqual(drift_s, dropout_s, places=2)


class TestTrackLoading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name, seconds, rate=RATE):
        path = os.path.join(self.dir, name)
        data = np.sin(2 * np.pi * 440 * np.arange(int(seconds * rate)) / rate)
        sf.write(path, data.astype(np.float32), rate, subtype="PCM_16")
        return path

    def test_resamples_to_16k(self):
        path = self._write("track.wav", 2.0)
        result = capture.TrackResult(path=path, rate=RATE, frames=2 * RATE)
        audio = capture.load_track(result)
        self.assertAlmostEqual(len(audio) / dsp.TARGET_RATE, 2.0, places=2)

    def test_start_offset_is_padded_at_the_front(self):
        """The track whose stream started later missed the beginning and has
        to move back on the common timeline."""
        path = self._write("track.wav", 1.0)
        result = capture.TrackResult(path=path, rate=RATE, frames=RATE,
                                     start_offset_s=0.25)
        audio = capture.load_track(result)
        pad = int(0.25 * dsp.TARGET_RATE)
        np.testing.assert_allclose(audio[:pad], 0.0, atol=1e-7)
        self.assertGreater(float(np.max(np.abs(audio[pad:]))), 0.5)
        self.assertAlmostEqual(len(audio) / dsp.TARGET_RATE, 1.25, places=2)

    def test_missing_file_returns_empty(self):
        result = capture.TrackResult(path=os.path.join(self.dir, "gone.wav"),
                                     rate=RATE, frames=0)
        self.assertEqual(len(capture.load_track(result)), 0)
        self.assertEqual(len(capture.load_track(None)), 0)

    def test_duration_property(self):
        result = capture.TrackResult(path="x", rate=16000, frames=32000)
        self.assertAlmostEqual(result.duration_s, 2.0)


if __name__ == "__main__":
    unittest.main()
