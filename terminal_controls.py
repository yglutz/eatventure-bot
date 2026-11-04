"""Terminal-local commands. Reader never starts the bot or performs input."""
import os
import queue
import select
import threading


class CommandQueue:
    """Serialize start/stop intent without executing actions on callback threads."""
    def __init__(self, pause):
        self.pause = pause
        self.commands = queue.SimpleQueue()
        self.stopped = threading.Event()
        self._intent_lock = threading.Lock()
        self._generation = 0
        self._quit_requested = False
        self._start_requested = False

    def submit(self, line):
        command = line.strip().lower()
        command = {'z': 'start', 'p': 'quit'}.get(command, command)
        with self._intent_lock:
            if self._quit_requested or self.stopped.is_set():
                return
            if command == 'toggle':
                command = 'pause' if self._start_requested else 'start'
            if command == 'start':
                self._start_requested = True
            if command in ('pause', 'quit'):
                self._start_requested = False
                self._generation += 1
                self._quit_requested = command == 'quit'
                self.pause()
            if command:
                self.commands.put((command, self._generation))

    def try_start(self, command_token, resume):
        """Atomically reject starts superseded even after main dequeued them."""
        with self._intent_lock:
            if (self._quit_requested or self.stopped.is_set()
                    or command_token != self._generation):
                return False
            resume()
            return True

    def close(self):
        with self._intent_lock:
            self.stopped.set()


class TerminalControls(CommandQueue):
    def __init__(self, stream, pause):
        super().__init__(pause)
        self.stream = stream
        self._wake_read, self._wake_write = os.pipe()
        self._thread = threading.Thread(target=self._read, name='terminal-controls')

    def _read(self):
        try:
            self._read_lines()
        except (OSError, ValueError):
            # Losing the controlling terminal is a stop request, never permission
            # to continue unattended. No backend calls occur on this thread.
            self.submit('quit')

    def _read_lines(self):
        if os.name == 'nt':
            self._read_windows_console()
            return
        # Read bytes rather than buffered readline: a partial line must not
        # prevent orderly shutdown. The wake pipe makes close immediate.
        pending = b''
        fd = self.stream.fileno()
        while not self.stopped.is_set():
            ready, _, _ = select.select([fd, self._wake_read], [], [])
            if self._wake_read in ready:
                return
            data = os.read(fd, 4096)
            if not data:
                self.submit('quit')
                return
            pending += data
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                self.submit(line.decode('utf-8', errors='replace'))

    def _read_windows_console(self):
        import msvcrt
        if not self.stream.isatty():
            # Windows select cannot wait on redirected console handles.
            self.submit('quit')
            return
        pending = ''
        while not self.stopped.wait(0.05):
            while msvcrt.kbhit():
                char = msvcrt.getwche()
                if char in ('\x03', '\x1a'):
                    self.submit('quit')
                    return
                if char in ('\r', '\n'):
                    self.submit(pending)
                    pending = ''
                elif char == '\b':
                    pending = pending[:-1]
                else:
                    pending += char

    def start(self):
        self._thread.start()

    def close(self):
        super().close()
        os.write(self._wake_write, b'x')
        if self._thread.ident is not None:
            self._thread.join()
        os.close(self._wake_read)
        os.close(self._wake_write)
