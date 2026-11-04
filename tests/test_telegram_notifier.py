"""Offline tests for TelegramNotifier — no real network calls."""

import queue
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch, MagicMock

# Inject a stub 'requests' module so telegram_notifier can be imported
# without the real package installed in this environment.
if "requests" not in sys.modules:
    _requests_stub = types.ModuleType("requests")
    _requests_stub.post = MagicMock()
    sys.modules["requests"] = _requests_stub

from telegram_notifier import TelegramNotifier  # noqa: E402


class TestTelegramNotifier(unittest.TestCase):
    """Tests for the non-blocking TelegramNotifier."""

    def _make_notifier(self, enabled=True, max_queue_size=16, **kwargs):
        return TelegramNotifier(
            "test_token", "12345", enabled=enabled, max_queue_size=max_queue_size, **kwargs
        )

    def test_send_message_returns_immediately_when_enabled(self):
        """send_message enqueues and returns True without blocking on network."""
        with patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier = self._make_notifier()
            try:
                start = time.monotonic()
                result = notifier.send_message("hello")
                elapsed = time.monotonic() - start
                self.assertTrue(result)
                # Must return in well under the 5s network timeout
                self.assertLess(elapsed, 0.1)
            finally:
                notifier.close()

    def test_send_message_returns_false_when_disabled(self):
        """When disabled, send_message returns False and nothing is enqueued."""
        notifier = self._make_notifier(enabled=False)
        try:
            result = notifier.send_message("hello")
            self.assertFalse(result)
            self.assertTrue(notifier._queue.empty())
        finally:
            notifier.close()

    def test_queue_full_drops_message_with_warning(self):
        """When the queue is full, the message is dropped and a warning is logged."""
        with patch("telegram_notifier.requests.post") as mock_post:
            # Block the worker so it doesn't drain the queue
            block = threading.Event()
            entered = threading.Event()

            def blocked_post(*args, **kwargs):
                entered.set()
                block.wait(5)
                return MagicMock(status_code=200)

            mock_post.side_effect = blocked_post

            notifier = self._make_notifier(max_queue_size=1)
            try:
                # The worker will pick up the first message and block on requests.post
                notifier.send_message("msg1")
                # Give the worker time to pull msg1 from the queue (it's now blocked)
                self.assertTrue(entered.wait(2))

                # Queue is empty again (worker pulled msg1) — msg2 fills it
                result2 = notifier.send_message("msg2")
                self.assertTrue(result2)

                # Queue is now full (1/1) — this should be dropped
                result3 = notifier.send_message("msg3")
                self.assertFalse(result3)

                # Verify a warning was logged
                with self.assertLogs("telegram_notifier", level="WARNING") as log:
                    notifier.send_message("msg4")
                self.assertTrue(any("queue full" in m.lower() for m in log.output))

            finally:
                block.set()
                notifier.close()

    def test_close_flushes_pending_messages(self):
        """Pending messages in the queue are sent before shutdown."""
        with patch("telegram_notifier.requests.post") as mock_post:
            # Make the worker process messages slowly so they accumulate
            call_count = {"n": 0}

            def slow_post(*args, **kwargs):
                call_count["n"] += 1
                time.sleep(0.05)
                return MagicMock(status_code=200)

            mock_post.side_effect = slow_post

            notifier = self._make_notifier()
            try:
                # Enqueue several messages rapidly while worker is slow
                for i in range(5):
                    notifier.send_message(f"msg{i}")

                # Close should flush remaining messages
                notifier.close()
            finally:
                pass

            # All 5 messages should have been sent
            self.assertEqual(mock_post.call_count, 5)

    def test_close_stops_thread(self):
        """After close(), the background thread is no longer alive."""
        with patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier = self._make_notifier()
            try:
                self.assertTrue(notifier._thread.is_alive())
                notifier.close()
            finally:
                pass

            self.assertFalse(notifier._thread.is_alive())

    def test_messages_sent_in_order(self):
        """Messages are sent in FIFO order."""
        with patch("telegram_notifier.requests.post") as mock_post:
            # Synchronize: block the first call until all 3 are enqueued,
            # then release them all at once
            ready = threading.Event()
            release = threading.Event()
            sent_texts = []

            def controlled_post(url, json=None, **kwargs):
                if sent_texts:
                    # Subsequent calls proceed immediately
                    pass
                else:
                    # First call: wait until we signal all are enqueued
                    ready.wait(5)
                sent_texts.append(json["text"])
                return MagicMock(status_code=200)

            mock_post.side_effect = controlled_post

            notifier = self._make_notifier()
            try:
                notifier.send_message("first")
                notifier.send_message("second")
                notifier.send_message("third")

                # Let the worker pick up the first message (it will block on ready)
                time.sleep(0.3)
                ready.set()

                # Give time for all three to be processed
                time.sleep(0.5)
            finally:
                notifier.close()

            self.assertEqual(sent_texts, ["first", "second", "third"])

    def test_send_message_after_close_returns_false(self):
        """After close(), send_message returns False without enqueuing."""
        with patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier = self._make_notifier()
            notifier.close()

            result = notifier.send_message("after close")
            self.assertFalse(result)

    def test_close_is_idempotent(self):
        """Calling close() twice is safe and does not raise."""
        with patch("telegram_notifier.requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200)
            notifier = self._make_notifier()
            notifier.close()
            notifier.close()  # Second call should be a no-op
            self.assertFalse(notifier._thread.is_alive())

    def test_queue_requires_positive_bound(self):
        for bound in (0, -1, 1.5, None):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                self._make_notifier(enabled=False, max_queue_size=bound)

    def test_close_timeout_then_repeat_close_joins_worker(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_post(*args, **kwargs):
            entered.set()
            release.wait(5)
            return MagicMock(status_code=200)

        with patch("telegram_notifier.requests.post", side_effect=blocked_post) as post:
            notifier = self._make_notifier(max_queue_size=1)
            try:
                self.assertTrue(notifier.send_message("in flight"))
                self.assertTrue(entered.wait(2))
                self.assertTrue(notifier.send_message("pending"))
                self.assertFalse(notifier.close(timeout=0))
                self.assertTrue(notifier._thread.is_alive())
                self.assertFalse(notifier.send_message("too late"))
                release.set()
                self.assertTrue(notifier.close())
                self.assertEqual(post.call_count, 2)
            finally:
                release.set()
                notifier.close()

    def test_enqueue_is_serialized_with_close(self):
        enqueue_entered = threading.Event()
        release_enqueue = threading.Event()
        close_attempted = threading.Event()
        results = []
        with patch("telegram_notifier.requests.post", return_value=MagicMock(status_code=200)) as post:
            notifier = self._make_notifier()
            original_lock = notifier._lifecycle_lock
            original_put = notifier._queue.put_nowait

            class ObservedLock:
                def __enter__(self):
                    if threading.current_thread().name == "test-closer":
                        close_attempted.set()
                    original_lock.acquire()

                def __exit__(self, *args):
                    original_lock.release()

            def paused_put(message):
                enqueue_entered.set()
                if not release_enqueue.wait(2):
                    raise AssertionError("enqueue was not released")
                original_put(message)

            notifier._lifecycle_lock = ObservedLock()
            sender = threading.Thread(target=lambda: results.append(notifier.send_message("accepted")))
            closer = threading.Thread(target=notifier.close, name="test-closer")
            try:
                with patch.object(notifier._queue, "put_nowait", side_effect=paused_put):
                    sender.start()
                    self.assertTrue(enqueue_entered.wait(2))
                    closer.start()
                    self.assertTrue(close_attempted.wait(2))
                    self.assertFalse(notifier._stop_event.is_set())
                    release_enqueue.set()
                    sender.join(2)
                    closer.join(2)
                self.assertFalse(sender.is_alive())
                self.assertFalse(closer.is_alive())
                self.assertEqual(results, [True])
                self.assertEqual(post.call_count, 1)
                self.assertTrue(notifier._queue.empty())
            finally:
                release_enqueue.set()
                sender.join(2)
                if closer.ident is not None:
                    closer.join(2)
                notifier.close()


if __name__ == "__main__":
    unittest.main()
