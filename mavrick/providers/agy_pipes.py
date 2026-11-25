"""Relay Windows subprocess pipes through selectable local sockets."""

import socket
import threading


class WorkerPipeRelay:
    """Expose the existing NDJSON reader/writer to Windows anonymous pipes."""

    def __init__(self, stdin, stdout):
        self.input, writer = socket.socketpair()
        self.output, reader = socket.socketpair()
        self._sockets = (self.input, self.output, writer, reader)
        self.input.setblocking(False)
        self.output.setblocking(False)

        def to_worker():
            with writer:
                try:
                    while data := writer.recv(4096):
                        pending = memoryview(data)
                        while pending:
                            written = stdin.write(pending)
                            if not written:
                                return
                            pending = pending[written:]
                except (OSError, ValueError):
                    pass

        def from_worker():
            with reader:
                try:
                    while data := stdout.read(4096):
                        reader.sendall(data)
                except (OSError, ValueError):
                    pass

        self._threads = [threading.Thread(target=relay, daemon=True) for relay in (to_worker, from_worker)]
        for thread in self._threads:
            thread.start()

    def close(self):
        for wire in self._sockets:
            try:
                wire.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            wire.close()
        for thread in self._threads:
            thread.join(timeout=0.2)
