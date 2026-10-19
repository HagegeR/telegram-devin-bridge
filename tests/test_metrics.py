"""Coverage for app/metrics.py and the /metrics command."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import metrics
from app.commands import handle_command
from tests.test_v2 import _FakeDevin, _FakeTelegram, message, settings


def test_render_lists_counters_and_uptime() -> None:
    metrics.incr("pytest_probe")
    out = metrics.render()
    assert out.startswith("◆ Metrics · uptime ")
    assert re.search(r"^pytest_probe +[1-9]\d*$", out, re.MULTILINE)


@pytest.mark.asyncio
async def test_metrics_command_requires_admin(tmp_path: Path) -> None:
    from app.main import Bridge
    from app.store import Store

    config = settings(
        tmp_path, telegram_admin_user_ids="900", telegram_allowed_users="900,222"
    )
    telegram = _FakeTelegram()
    runtime = Bridge(config, Store(":memory:"), _FakeDevin(), telegram)  # type: ignore[arg-type]
    await handle_command(
        runtime, message("/metrics", user_id=222), "/metrics"
    )
    assert "admin-only" in telegram.sent[-1]["text"]

    await handle_command(
        runtime, message("/metrics", user_id=900), "/metrics"
    )
    body = telegram.sent[-1]["text"]
    assert body.startswith("◆ Metrics")
    # the counter is process-global — other tests may have run commands
    # first, so only the floor is ours (both /metrics calls landed)
    match = re.search(r"^commands +(\d+)$", body, re.MULTILINE)
    assert match and int(match.group(1)) >= 2
    await runtime.shutdown()
