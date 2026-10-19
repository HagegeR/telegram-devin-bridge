"""Process-local counters rendered by /metrics. Reset on restart, never
persisted. Module state keeps the call sites one line each; there is no
per-chat attribution and nothing here is a billing source."""

from __future__ import annotations

import time
from collections import Counter

_started = time.monotonic()
_counters: Counter[str] = Counter()


def incr(name: str) -> None:
    _counters[name] += 1


def render() -> str:
    up = int(time.monotonic() - _started)
    hours, rem = divmod(up, 3600)
    minutes, seconds = divmod(rem, 60)
    age = f"{hours}h{minutes}m{seconds}s" if hours else f"{minutes}m{seconds}s"
    lines = [f"◆ Metrics · uptime {age} (counters reset on restart)"]
    width = max((len(name) for name in _counters), default=0)
    for name, count in sorted(_counters.items()):
        lines.append(f"{name.ljust(width)}  {count}")
    return "\n".join(lines)
