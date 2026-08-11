"""Tests for the pure UI helper functions (no window required)."""

import unittest

from audio_transcriber.ui import theme, widgets


class TestColourMix(unittest.TestCase):
    def test_endpoints(self):
        self.assertEqual(theme.mix("#000000", "#ffffff", 0.0), "#000000")
        self.assertEqual(theme.mix("#000000", "#ffffff", 1.0), "#ffffff")

    def test_midpoint(self):
        self.assertEqual(theme.mix("#000000", "#ffffff", 0.5), "#808080")

    def test_channelwise(self):
        self.assertEqual(theme.mix("#ff0000", "#0000ff", 0.5), "#800080")


class TestTimestampDetection(unittest.TestCase):
    def test_accepts_timestamps(self):
        for token in ("[00:00]", "[01:15]", "[01:30:12]", "[99:59]"):
            self.assertTrue(widgets._looks_like_timestamp(token), token)

    def test_rejects_speakers_and_noise(self):
        for token in ("[You]", "[Participant]", "[Participant A]",
                      "[ERROR]", "[]", "[a:b]"):
            self.assertFalse(widgets._looks_like_timestamp(token), token)


class TestSpeakerPattern(unittest.TestCase):
    def test_matches_speaker_prefix(self):
        match = widgets._SPEAKER_RE.match(" [You]: Hello everyone.")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1).strip(), "[You]:")
        self.assertEqual(match.group(2).strip(), "Hello everyone.")

    def test_matches_other_speaker(self):
        match = widgets._SPEAKER_RE.match("[Participant]: Sure, happy to.")
        self.assertIsNotNone(match)
        self.assertIn("Participant", match.group(1))

    def test_ignores_plain_text(self):
        for line in ("Recording started.", "whisper_model_load: loading",
                     "Quality filter: 2 crosstalk segment(s) dropped."):
            self.assertIsNone(widgets._SPEAKER_RE.match(line), line)

    def test_keeps_text_with_colons(self):
        match = widgets._SPEAKER_RE.match("[You]: It is 12:30 now.")
        self.assertEqual(match.group(2).strip(), "It is 12:30 now.")


class TestRoundRectGeometry(unittest.TestCase):
    def test_radius_is_clamped_to_the_shape(self):
        """An oversized radius must not distort the polygon."""
        captured = {}

        class FakeCanvas:
            def create_polygon(self, points, **kwargs):
                captured["points"] = points
                return 1

        theme.round_rect(FakeCanvas(), 0, 0, 10, 4, 50)
        xs = captured["points"][0::2]
        ys = captured["points"][1::2]
        self.assertGreaterEqual(min(xs), 0)
        self.assertLessEqual(max(xs), 10)
        self.assertGreaterEqual(min(ys), 0)
        self.assertLessEqual(max(ys), 4)


if __name__ == "__main__":
    unittest.main()
