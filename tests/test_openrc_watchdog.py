"""Shell-level coverage for deploy/openrc/telegram-devin-bridge-watchdog.

Runs the real script with stub flock/rc-service on a stub PATH. Guards the
BusyBox regression: busybox flock has no -w, so the old `flock -w ... ||
exit 0` line exited before ever checking the service — a dead bridge was
never restarted on Alpine hosts.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="deploy/openrc watchdog is POSIX-only"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "openrc" / "telegram-devin-bridge-watchdog"

RC_SERVICE = """#!/bin/sh
echo "$*" >> "$WATCHDOG_CALLS"
if [ "$2" = "status" ]; then
    echo " * status: ${WATCHDOG_STATUS:-stopped}"
fi
"""

# busybox flock: -s -x -u -n only — errors on -w like the real binary
FLOCK_BUSYBOX = """#!/bin/sh
case "$1" in
    -w*) echo "flock: unrecognized option: w" >&2; exit 1 ;;
esac
exit 0
"""

FLOCK_CONTENDED = "#!/bin/sh\nexit 1\n"


def run_watchdog(tmp_path: Path, flock_body: str, status: str = "stopped"):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (bin_dir / "rc-service").write_text(RC_SERVICE)
    (bin_dir / "rc-service").chmod(0o755)
    (bin_dir / "flock").write_text(flock_body)
    (bin_dir / "flock").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "BRIDGE_HOME": str(home),
        "WATCHDOG_CALLS": str(calls),
        "WATCHDOG_STATUS": status,
    }
    proc = subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    invoked = calls.read_text() if calls.exists() else ""
    return proc, invoked


def test_free_lock_starts_a_stopped_service(tmp_path: Path):
    proc, invoked = run_watchdog(tmp_path, FLOCK_BUSYBOX)
    assert proc.returncode == 0, proc.stderr
    assert "telegram-devin-bridge status" in invoked
    assert "telegram-devin-bridge start" in invoked


def test_busybox_flock_without_w_flag_still_checks_service(tmp_path: Path):
    # the regression this file guards: flock errors on -w must not skip the
    # service check — on the old `flock -w` line this test gets no calls
    proc, invoked = run_watchdog(tmp_path, FLOCK_BUSYBOX)
    assert proc.returncode == 0, proc.stderr
    assert "telegram-devin-bridge status" in invoked


def test_contended_lock_skips_the_run(tmp_path: Path):
    proc, invoked = run_watchdog(tmp_path, FLOCK_CONTENDED)
    assert proc.returncode == 0, proc.stderr
    assert "telegram-devin-bridge" not in invoked


def test_started_service_is_not_restarted(tmp_path: Path):
    proc, invoked = run_watchdog(tmp_path, FLOCK_BUSYBOX, status="started")
    assert proc.returncode == 0, proc.stderr
    assert "status" in invoked
    assert "restart" not in invoked
    assert " start" not in invoked
