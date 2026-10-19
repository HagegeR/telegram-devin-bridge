"""Process-local counters rendered by /metrics. Reset on restart, never
persisted. Module state keeps the call sites one line each; there is no
per-chat attribution and nothing here is a billing source."""

from __future__ import annotations

import time
from collections import Counter

_started = time.monotonic()
_counters: Counter[str] = Counter()
# the documented counters, rendered even while still zero — an absent row
# reads as missing instrumentation, not as "nothing happened"
_NAMES = (
    "updates",
    "commands",
    "turns",
    "sessions_cloud",
    "sessions_local",
    "events",
    "errors",
    "watcher_failures",
)


def incr(name: str) -> None:
    _counters[name] += 1


def render() -> str:
    up = int(time.monotonic() - _started)
    hours, rem = divmod(up, 3600)
    minutes, seconds = divmod(rem, 60)
    age = f"{hours}h{minutes}m{seconds}s" if hours else f"{minutes}m{seconds}s"
    lines = [f"◆ Metrics · uptime {age} (counters reset on restart)"]
    extras = sorted(name for name in _counters if name not in _NAMES)
    width = max((len(name) for name in (*_NAMES, *extras)), default=0)
    for name in (*_NAMES, *extras):
        lines.append(f"{name.ljust(width)}  {_counters[name]}")
    return "\n".join(lines)
