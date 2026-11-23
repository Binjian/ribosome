"""Coordinate KRLU project starts and service cooldowns across one CLI run."""

import logging
import math
import re
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from threading import Condition


def retry_after_seconds(value):
    if not isinstance(value, str):
        return 0
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            seconds = float(value)
            return seconds if math.isfinite(seconds) else 0
        return max(0, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


class ProjectPacing:
    def __init__(self, concurrency, delay):
        self.limit = concurrency
        self.delay = delay
        self._active = 0
        self._condition = Condition()
        self._next_start = 0
        self._cooldown = 0

    def slow_down(self, reason, retry_after=None):
        with self._condition:
            self.limit = 1
            self.delay = max(self.delay, min(120, self.delay * 2))
            now = time.monotonic()
            self._next_start = max(self._next_start, now + self.delay)
            if retry_after is not None:
                self._cooldown = max(self._cooldown, now + max(self.delay, retry_after))
            logging.getLogger("collect_krl_qa").warning(
                "%s. Limiting KRLU generation to one active project, spacing starts by %gs "
                "(shared request cooldown %gs)", reason, self.delay, max(0, self._cooldown - now),
            )
            self._condition.notify_all()

    def wait_for_service(self, deadline=None):
        with self._condition:
            while (remaining := self._cooldown - time.monotonic()) > 0:
                if deadline is not None and self._cooldown >= deadline:
                    return False
                self._condition.wait(min(60, remaining))
            return True

    @contextmanager
    def generation(self):
        with self._condition:
            while True:
                now = time.monotonic()
                remaining = max(self._cooldown, self._next_start) - now
                if self._active < self.limit and remaining <= 0:
                    break
                self._condition.wait(min(remaining, 60) if remaining > 0 else None)
            self._active += 1
            self._next_start = now + self.delay
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()
