"""Offline terminal reader lifecycle and command checks."""
import os
import unittest
from unittest.mock import Mock

from terminal_controls import CommandQueue, TerminalControls


class TerminalControlsTests(unittest.TestCase):
    def test_toggle_stop_supersedes_dequeued_start(self):
        controls = CommandQueue(Mock())
        resume = Mock()
        controls.submit('toggle')
        _, token = controls.commands.get_nowait()
        controls.submit('toggle')
        self.assertFalse(controls.try_start(token, resume))
        controls.pause.assert_called_once_with()
        resume.assert_not_called()
        controls.close()

    def test_start_is_only_queued_pause_is_immediate(self):
        pause = Mock()
        controls = TerminalControls(None, pause)
        try:
            controls.submit('start')
            pause.assert_not_called()
            self.assertEqual(controls.commands.get_nowait(), ('start', 0))
            controls.submit('pause')
            pause.assert_called_once_with()
        finally:
            controls.close()

    def test_eof_pauses_and_requests_exit(self):
        read, write = os.pipe()
        pause = Mock()
        with os.fdopen(read) as stream:
            controls = TerminalControls(stream, pause)
            controls.start()
            os.close(write)
            self.assertEqual(controls.commands.get(timeout=1), ('quit', 1))
            controls.close()
        pause.assert_called_once_with()

    def test_close_wakes_partial_line_reader(self):
        read, write = os.pipe()
        with os.fdopen(read) as stream:
            controls = TerminalControls(stream, Mock())
            controls.start()
            os.write(write, b'star')
            controls.close()
            self.assertFalse(controls._thread.is_alive())
        os.close(write)

    def test_dequeued_start_cannot_override_later_pause(self):
        controls = TerminalControls(None, Mock())
        resume = Mock()
        try:
            controls.submit('start')
            _, token = controls.commands.get_nowait()
            # Deterministically model reader pause between dequeue and resume.
            controls.submit('pause')
            self.assertFalse(controls.try_start(token, resume))
            resume.assert_not_called()
            controls.submit('start')
            controls.commands.get_nowait()  # pause
            _, fresh = controls.commands.get_nowait()
            self.assertTrue(controls.try_start(fresh, resume))
            resume.assert_called_once_with()
        finally:
            controls.close()

    def test_quit_latches_and_discards_future_commands(self):
        controls = TerminalControls(None, Mock())
        resume = Mock()
        try:
            controls.submit('start')
            _, token = controls.commands.get_nowait()
            controls.submit('quit')
            for _ in range(100):
                controls.submit('start')
            self.assertFalse(controls.try_start(token, resume))
            self.assertEqual(controls.commands.get_nowait(), ('quit', 1))
            self.assertTrue(controls.commands.empty())
            resume.assert_not_called()
        finally:
            controls.close()
