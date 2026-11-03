"""Bounded reader/exclusive-writer gate for one research capability."""
from contextlib import contextmanager
import threading


class ToolGate:
    def __init__(self, parallelism=4):
        self.parallelism = parallelism
        self.condition = threading.Condition()
        self.readers = self.waiting_writers = 0
        self.writer = False

    @contextmanager
    def enter(self, *, read_only):
        with self.condition:
            if read_only:
                self.condition.wait_for(lambda: not self.writer and not self.waiting_writers and self.readers < self.parallelism)
                self.readers += 1
            else:
                self.waiting_writers += 1
                try:
                    self.condition.wait_for(lambda: not self.writer and not self.readers)
                    self.writer = True
                finally:
                    self.waiting_writers -= 1
        try:
            yield
        finally:
            with self.condition:
                if read_only:
                    self.readers -= 1
                else:
                    self.writer = False
                self.condition.notify_all()
