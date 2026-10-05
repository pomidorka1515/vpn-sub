"""One locked generation cache. Inbounds and clients each hold their own.

A failed stamp bump leaves the local value in place, so this worker cannot
hide a generation other workers still trust. A fill that observed an older
stamp does not revive a value a clear already dropped.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from .stamp import bump_stamp, read_stamp

__all__ = ["GenerationCache"]



class GenerationCache[T]:
    """In-memory value whose generation is a stamp file's mtime."""

    def __init__(self, path: str, clock: Callable[[], float]) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._value: T | None = None
        self.time: float = 0
        self.stamp: int | None = None

    def get(self) -> T | None:
        with self._lock:
            return self._value

    def set(self, value: T) -> None:
        # read before the lock so a clear that bumps during the panel query
        # is visible here instead of being overwritten by this fill
        stamp = read_stamp(self.path)
        with self._lock:
            if self.stamp is not None and stamp != self.stamp:
                return
            self._value = value
            self.time = self._clock()
            self.stamp = stamp

    def age(self) -> float:
        """Seconds since the value was last populated (own clock domain)."""
        with self._lock:
            return self._clock() - self.time

    def current(self) -> bool:
        """True when this process's value still matches the shared stamp."""
        with self._lock:
            if self._value is None or self.stamp is None:
                return False
            try:
                return read_stamp(self.path) == self.stamp
            except OSError:
                return False

    def fresh(self, ttl: float) -> T | None:
        """Return the value only when age and stamp still agree.

        The three checks share one lock so a clear cannot land between them
        and hand back a value that was just dropped.
        """
        with self._lock:
            cached = self._value
            seen = self.stamp
            if cached is None or seen is None or self._clock() - self.time >= ttl:
                return None
            try:
                current = read_stamp(self.path) == seen
            except OSError:
                return None
            return cached if current else None

    def clear(self) -> None:
        """Bump the shared stamp first, then drop the local value.

        If the bump fails the local value stays. Dropping it first would hide
        the failure from this worker while every other worker kept serving it.
        """
        bump_stamp(self.path)
        with self._lock:
            self._value = None
            self.time = 0
            self.stamp = None
