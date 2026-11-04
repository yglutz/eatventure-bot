import requests
import logging
import queue
import threading
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token, chat_id, enabled=True, max_queue_size=16):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = enabled and bool(bot_token and chat_id)
        self._queue = queue.Queue(maxsize=max_queue_size)
        self._stop_event = threading.Event()
        self._thread = None
        self._closed = False

        if self.enabled:
            logger.info("Telegram notifier enabled")
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
        else:
            logger.warning("Telegram notifier disabled")

    def _worker(self):
        while True:
            try:
                message = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._stop_event.is_set():
                    break
                continue

            self._send(message)

            # Drain any remaining items after stop is signaled
            if self._stop_event.is_set():
                while not self._queue.empty():
                    try:
                        msg = self._queue.get_nowait()
                        self._send(msg)
                    except queue.Empty:
                        break
                break

    def _send(self, message):
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
            }

            response = requests.post(url, json=data, timeout=5)

            if response.status_code == 200:
                logger.debug(f"Telegram message sent: {message}")
            else:
                logger.error(f"Failed to send Telegram message: {response.status_code} - {response.text}")

        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")

    def send_message(self, message):
        if not self.enabled or self._closed:
            return False

        try:
            self._queue.put_nowait(message)
            return True
        except queue.Full:
            logger.warning("Telegram notifier queue full; dropping message")
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True

        if self._thread is not None:
            self._stop_event.set()
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("Telegram notifier thread did not stop within timeout")

    def notify_bot_started(self):
        message = "🤖 <b>Bot Started</b>"
        self.send_message(message)

    def notify_bot_stopped(self):
        message = "⏹️ <b>Bot Stopped</b>"
        self.send_message(message)

    def notify_new_level(self, level_number, time_spent):
        minutes = int(time_spent // 60)
        seconds = int(time_spent % 60)
        time_str = f"{minutes:02d}:{seconds:02d}"

        message = f"{level_number}. restaurant completed! Time spent: {time_str}"
        self.send_message(message)

    def notify_level_milestone(self, total_levels):
        message = f"📊 <b>Milestone Reached!</b>\nTotal cities completed: {total_levels}"
        self.send_message(message)
