"""Recording test against real audio hardware.

Skipped by default because it opens the microphone and the loopback. Enable
with:

    set AUDIO_TRANSCRIBER_HW_TEST=1
    python run_tests.py capture_hardware

Checks the parts of audit finding H3 that cannot show up without hardware:
opening the streams, the timeline against the system clock, alignment of both
tracks, constant memory use and clean thread shutdown.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest

import soundfile as sf

ENABLED = os.environ.get("AUDIO_TRANSCRIBER_HW_TEST") == "1"
DURATION_S = 6.0


@unittest.skipUnless(ENABLED, "set AUDIO_TRANSCRIBER_HW_TEST=1 to enable")
class TestRealCapture(unittest.TestCase):
    def setUp(self):
        import pyaudiowpatch as pyaudio
        from audio_transcriber.audio import devices as devmod
        from audio_transcriber.audio.capture import AudioEngine

        self.dir = tempfile.mkdtemp()
        self.pa = pyaudio.PyAudio()
        self.devices = devmod.enumerate_devices(self.pa)

        mics = devmod.microphone_candidates(self.devices)
        outputs = devmod.playback_candidates(self.devices)
        if not mics or not outputs:
            self.skipTest("no suitable devices found")

        self.mic = mics[0]
        self.loopback, _reason = devmod.find_loopback_for(self.devices, outputs[0])
        self.engine = AudioEngine(self.pa, self.dir)

    def tearDown(self):
        try:
            self.engine.stop_streams()
        finally:
            self.pa.terminate()
            shutil.rmtree(self.dir, ignore_errors=True)

    def test_records_both_tracks_with_correct_timeline(self):
        for warning in self.engine.configure(self.mic, self.loopback):
            print("  note:", warning)

        threads_before = threading.active_count()
        self.engine.start_recording("hwtest")
        started = time.perf_counter()
        time.sleep(DURATION_S)
        result = self.engine.stop_recording()
        wall_clock = time.perf_counter() - started

        self.assertTrue(result.has_audio, "neither track delivered data")

        for name, track in (("mic", result.mic), ("sys", result.sys)):
            with self.subTest(track=name):
                if track is None:
                    continue
                self.assertTrue(os.path.exists(track.path))
                info = sf.info(track.path)
                self.assertEqual(info.channels, 1, "the track must be mono")

                print(f"  {name}: {track.device_name[:34]:36s} "
                      f"{info.frames / info.samplerate:6.2f} s @ {info.samplerate} Hz, "
                      f"dropouts {track.inserted_silence_s * 1000:5.1f} ms, "
                      f"start offset {track.start_offset_s * 1000:5.1f} ms")

                # What matters is not the raw length but the length on the
                # COMMON timeline: raw length plus start offset. A loopback
                # device occasionally needs a few hundred milliseconds before
                # the first buffer arrives - that is what start_offset_s is for.
                on_timeline = info.frames / info.samplerate + track.start_offset_s
                self.assertAlmostEqual(on_timeline, wall_clock, delta=0.6)
                # No dropout correction during a quiet six second recording
                self.assertLess(track.inserted_silence_s, 0.5)

        if result.mic and result.sys:
            offset_ms = abs(result.mic.start_offset_s - result.sys.start_offset_s) * 1000
            print(f"  offset between the tracks: {offset_ms:.1f} ms")
            self.assertLess(offset_ms, 250.0,
                            "the two streams started too far apart")

            # Both tracks must be equally long on the common timeline
            from audio_transcriber.audio import capture
            mic_audio = capture.load_track(result.mic)
            sys_audio = capture.load_track(result.sys)
            drift_ms = abs(len(mic_audio) - len(sys_audio)) / 16000 * 1000
            print(f"  length difference after alignment: {drift_ms:.1f} ms")
            self.assertLess(drift_ms, 300.0)

        # The capture threads of this generation must terminate
        self.engine.stop_streams()
        time.sleep(0.3)
        self.assertLessEqual(threading.active_count(), threads_before,
                             "capture threads did not terminate")

    def test_memory_stays_flat(self):
        """Regression K3: memory use must not grow with recording length. What
        is measured is the size of the live window, the only thing that stays
        in RAM."""
        self.engine.configure(self.mic, self.loopback)
        self.engine.start_recording("hwmem")
        try:
            sizes = []
            for _ in range(3):
                time.sleep(2.0)
                mic, system = self.engine.live_window()
                sizes.append(sum(len(x) for x in (mic, system) if x is not None))
                print(f"  live window: {sizes[-1]} samples")
            # The window is capped (30 s at 16 kHz per track)
            self.assertLessEqual(max(sizes), 2 * 30 * 16000 * 1.1)
        finally:
            self.engine.stop_recording()

    def test_restarting_monitoring_does_not_leak_threads(self):
        """Regression H1: in the previous version every device change could
        leave another generation of capture threads behind."""
        baseline = threading.active_count()
        for _ in range(4):
            self.engine.configure(self.mic, self.loopback)
            time.sleep(0.3)
        self.engine.stop_streams()
        time.sleep(0.5)
        self.assertLessEqual(threading.active_count(), baseline + 1,
                             "threads of an old generation are still alive")


if __name__ == "__main__":
    unittest.main()
