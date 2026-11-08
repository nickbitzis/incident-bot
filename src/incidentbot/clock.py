"""Clocks. Everything reads time through one of these so tests and the simulation control it."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    """Manual clock for tests and the scripted simulation. Seconds since the epoch."""

    def __init__(self, start: float = 1_783_000_000.0) -> None:
        self.t = float(start)

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float = 0, minutes: float = 0) -> float:
        self.t += seconds + minutes * 60
        return self.t

    def set(self, t: float) -> None:
        self.t = float(t)


class DemoClock(FakeClock):
    """Fixed while the demo data is being scripted, then follows real time after release()."""

    def __init__(self, start: float) -> None:
        super().__init__(start)
        self.live = False

    def release(self) -> None:
        self.live = True

    def now(self) -> float:
        return time.time() if self.live else self.t
