"""Tests for the worker-to-GUI bridge - a direct regression for K2."""

import queue
import threading
import unittest

from audio_transcriber.events import Failed, Log, Status, UiBridge


class FakeRoot:
    """Minimal Tk stand-in: after() only remembers the callback."""

    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def after(self, _ms, func=None):
        self.scheduled.append(func)
        return len(self.scheduled)

    def after_cancel(self, ident):
        self.cancelled.append(ident)


class TestLateBoundExceptionBug(unittest.TestCase):
    """The actual core of K2.

    The previous version wrote this inside worker threads:
        except Exception as e:
            self.root.after(0, lambda: self.transcription_failed(str(e)))

    Python deletes 'e' at the end of the except block. The lambda only ran
    later on the Tk event loop - by then the closure cell was empty and it
    raised NameError instead of the error message. Result: no dialog, no
    reset, a locked user interface.
    """

    def test_old_pattern_raises_nameerror(self):
        deferred = queue.Queue()

        def worker():
            try:
                raise ValueError("model missing")
            except Exception as e:                        # noqa: F841
                deferred.put(lambda: f"Error: {e}")       # the old spelling

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        with self.assertRaises(NameError):
            deferred.get()()

    def test_new_pattern_transports_the_message(self):
        bridge = UiBridge(FakeRoot())
        received = []
        bridge.on(Failed, lambda event: received.append(event.message))

        def worker():
            try:
                raise ValueError("model missing")
            except Exception as exc:
                bridge.post_exception("Transcription failed", exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        bridge.drain_now()
        self.assertEqual(len(received), 1)
        self.assertIn("model missing", received[0])
        self.assertIn("Transcription failed", received[0])

    def test_failed_event_is_a_plain_string(self):
        """Events carry data only - no callable with closure state crosses the
        thread boundary."""
        event = Failed(message="something went wrong")
        self.assertIsInstance(event.message, str)
        with self.assertRaises(Exception):
            event.message = "immutable"      # frozen dataclass


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.root = FakeRoot()
        self.bridge = UiBridge(self.root)

    def test_events_are_dispatched_in_order(self):
        seen = []
        self.bridge.on(Log, lambda event: seen.append(event.text))
        for index in range(5):
            self.bridge.post(Log(text=str(index)))
        self.bridge.drain_now()
        self.assertEqual(seen, ["0", "1", "2", "3", "4"])

    def test_events_from_many_threads_arrive(self):
        seen = []
        self.bridge.on(Log, lambda event: seen.append(event.text))
        threads = [threading.Thread(target=lambda i=i: self.bridge.post(Log(str(i))))
                   for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.bridge.drain_now()
        self.assertEqual(sorted(seen, key=int), [str(i) for i in range(20)])

    def test_broken_handler_does_not_stop_the_pump(self):
        import contextlib
        import io

        def explode(_event):
            raise RuntimeError("broken")

        seen = []
        self.bridge.on(Log, explode)
        self.bridge.on(Status, lambda event: seen.append(event.text))
        self.bridge.post(Log("whatever"))
        self.bridge.post(Status("carry on"))

        # The handler deliberately prints a traceback - keep it quiet here.
        with contextlib.redirect_stderr(io.StringIO()):
            self.bridge.drain_now()
        self.assertEqual(seen, ["carry on"])

    def test_unknown_event_types_are_ignored(self):
        self.bridge.post(Status("no handler"))
        self.bridge.drain_now()          # must not raise

    def test_pump_reschedules_itself(self):
        self.bridge.start()
        self.assertTrue(self.root.scheduled)
        self.bridge.stop()
        self.assertTrue(self.root.cancelled)

    def test_batch_limit_protects_the_gui(self):
        """A download must not starve the interface with events."""
        seen = []
        self.bridge.on(Log, lambda event: seen.append(event.text))
        for index in range(500):
            self.bridge.post(Log(str(index)))
        self.bridge.drain_now()
        self.assertEqual(len(seen), 200)
        self.bridge.drain_now()
        self.assertEqual(len(seen), 400)


if __name__ == "__main__":
    unittest.main()
