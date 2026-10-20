"""Shell-level coverage for deploy/self-update.sh channel resolution.

Builds a bare 'origin' plus a deploy clone in a tmp dir and runs the real
script with --check, asserting which revision each channel resolves to.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# the artifact under test is a POSIX shell script — nothing to port
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="deploy/self-update.sh is POSIX-only"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "self-update.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
    )


def commit(work: Path, name: str) -> None:
    (work / name).write_text(name)
    git(work, "add", name)
    git(work, "commit", "-qm", name)


@pytest.fixture()
def deploy_clone(tmp_path: Path):
    """Bare origin on main + a clone kept one release behind, like the host."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    clone = tmp_path / "clone"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-b", "main", str(work))
    git(work, "remote", "add", "origin", str(origin))

    commit(work, "a")  # clone's HEAD stays here
    git(work, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(clone))

    commit(work, "b")
    git(work, "tag", "-a", "v1.2.0", "-m", "v1.2.0")
    commit(work, "c")
    git(work, "tag", "-a", "v1.2.1", "-m", "v1.2.1")
    commit(work, "d")
    git(work, "tag", "-a", "v2.0.0", "-m", "v2.0.0")
    git(work, "push", "-q", "origin", "main", "v1.2.0", "v1.2.1", "v2.0.0")

    # a branch whose name looks version-ish must stay a branch channel
    git(work, "push", "-q", "origin", "main:refs/heads/v2-hotfix")
    # ref-valid but refspec-looking names must still fetch by name
    git(work, "push", "-q", "origin", "main:refs/heads/+canary")
    # a plain branch later replaced by a nested name must not block the switch
    git(work, "push", "-q", "origin", "main:refs/heads/release")
    # a tag on a commit not merged into main must never be selected
    git(work, "checkout", "-qb", "side", "HEAD~1")
    commit(work, "e")
    git(work, "tag", "-a", "v9.9.9", "-m", "v9.9.9")
    git(work, "push", "-q", "origin", "v9.9.9")
    git(work, "checkout", "-q", "main")
    # malformed tags (SemVer forbids leading zeroes) must never be selected
    git(work, "tag", "-a", "v02.0.0", "-m", "v02.0.0")
    git(work, "tag", "-a", "v1.02.0", "-m", "v1.02.0", "v1.2.0^{commit}")
    git(work, "push", "-q", "origin", "v02.0.0", "v1.02.0")

    (clone / "deploy").mkdir()
    shutil.copy(SCRIPT, clone / "deploy" / "self-update.sh")
    return clone, work


def check(clone: Path, channel: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "deploy/self-update.sh", "--check", channel],
        cwd=clone,
        capture_output=True,
        text=True,
        check=False,
    )


def track_of(result: subprocess.CompletedProcess) -> str:
    assert result.returncode == 0, result.stderr + result.stdout
    line = next(l for l in result.stdout.splitlines() if l.startswith("update available:"))
    return line.rsplit("(", 1)[1].rstrip(")")


def test_channel_resolution(deploy_clone):
    clone, _ = deploy_clone
    assert track_of(check(clone, "main")) == "main"
    assert track_of(check(clone, "stable")) == "v2.0.0"
    assert track_of(check(clone, "v1")) == "v1.2.1"
    assert track_of(check(clone, "v1.2")) == "v1.2.1"
    assert track_of(check(clone, "v1.2.0")) == "v1.2.0"
    assert track_of(check(clone, "v2")) == "v2.0.0"


def test_busy_marker_defers_automatic_update_but_not_check(deploy_clone):
    clone, _ = deploy_clone
    (clone / ".bridge-busy").touch()
    checked = check(clone, "main")
    assert checked.returncode == 0, checked.stderr + checked.stdout
    assert "deferring update" not in checked.stdout

    env = {**os.environ, **GIT_ENV}
    env.pop("SELF_UPDATE_FORCE", None)
    result = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == "deferring update: a local turn is in flight"


def test_late_busy_marker_defers_restart_and_retries_after_idle(
    deploy_clone, tmp_path: Path
):
    import signal

    clone, _ = deploy_clone
    previous = git_out(clone, "rev-parse", "HEAD")
    (clone / ".self-update-rev").write_text(f"{previous}\n")
    (clone / ".make-busy-once").touch()
    devin = tmp_path / "devin"
    devin.write_text(
        "#!/bin/sh\n"
        "if [ -f .make-busy-once ]; then\n"
        "  touch .bridge-busy\n"
        "  rm .make-busy-once\n"
        "fi\n"
    )
    devin.chmod(0o755)
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{_nonroot_id_stub(tmp_path)}:{os.environ['PATH']}",
        "DEVIN_LOCAL_CLI": str(devin),
        "BRIDGE_SUPERVISED": "1",
    }
    for var in ("INVOCATION_ID", "RC_SVCNAME", "BRIDGE_PID"):
        env.pop(var, None)

    deferred = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert deferred.returncode == 0, deferred.stdout + deferred.stderr
    assert (
        "update installed; restart deferred: a local turn is in flight"
        in deferred.stdout
    )
    assert "restarting telegram-devin-bridge" not in deferred.stdout
    assert (clone / ".self-update-rev").read_text().strip() == previous
    assert not (clone / ".self-update-pending").exists()
    assert (clone / ".bridge-busy").is_file()

    (clone / ".bridge-busy").unlink()
    retried = subprocess.run(
        [
            "sh",
            "-c",
            "sh deploy/self-update.sh main; exec sleep 30",
        ],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert retried.returncode == -signal.SIGTERM
    assert "incomplete; retrying install" in retried.stdout
    assert "restarting telegram-devin-bridge (supervisor respawn)" in retried.stdout
    assert (
        (clone / ".self-update-rev").read_text().strip()
        == git_out(clone, "rev-parse", "origin/main")
    )
    assert (clone / ".self-update-pending").exists()


def test_versionish_branch_name_stays_a_branch(deploy_clone):
    clone, _ = deploy_clone
    assert track_of(check(clone, "v2-hotfix")) == "v2-hotfix"


def test_refspec_looking_branch_names(deploy_clone):
    clone, _ = deploy_clone
    assert track_of(check(clone, "+canary")) == "+canary"


def test_deleted_parent_branch_does_not_block_nested(deploy_clone):
    clone, work = deploy_clone
    assert track_of(check(clone, "release")) == "release"
    git(work, "push", "-q", "origin", ":refs/heads/release")
    git(work, "push", "-q", "origin", "main:refs/heads/release/v2")
    assert track_of(check(clone, "release/v2")) == "release/v2"


def _nonroot_id_stub(tmp_path: Path, name: str = "nonroot-bin") -> Path:
    """A bin dir whose `id` reports a non-root uid.

    On a root host with rc-service or systemctl present the script takes an
    init branch instead of the supervisor-respawn/manual paths under test —
    and would drive the host's real service manager. The stub keeps the
    branch choice host-independent.
    """
    bin_dir = tmp_path / name
    bin_dir.mkdir(exist_ok=True)
    real_id = shutil.which("id") or "/usr/bin/id"
    stub = bin_dir / "id"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-u" ]; then echo 1000; else exec ' + real_id + ' "$@"; fi\n'
    )
    stub.chmod(0o755)
    return bin_dir


def test_real_update_survives_branch_nesting_switch(deploy_clone, tmp_path: Path):
    # full update path incl. the checkout: a stale local refs/heads/release
    # (left by pre-channel checkouts) must not block release/v2
    clone, work = deploy_clone
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{_nonroot_id_stub(tmp_path)}:{os.environ['PATH']}",
    }
    for var in ("INVOCATION_ID", "RC_SVCNAME", "BRIDGE_SUPERVISED", "BRIDGE_PID"):
        env.pop(var, None)

    def run(channel: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", "deploy/self-update.sh", channel],
            cwd=clone,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert run("release").returncode == 0
    git(clone, "branch", "-f", "release", "HEAD")  # stale ref, like old -B left
    git(work, "push", "-q", "origin", ":refs/heads/release")
    commit(work, "f")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/release/v2")
    result = run("release/v2")
    assert result.returncode == 0, result.stdout + result.stderr
    # the caller is not the service's MainPID here: no detached TERM is armed
    assert "restart telegram-devin-bridge manually" in result.stdout
    head = git_out(clone, "rev-parse", "HEAD")
    assert head == git_out(clone, "rev-parse", "origin/release/v2")


def test_supervised_caller_gets_termed(deploy_clone, tmp_path: Path):
    # when the caller IS the service's MainPID the script arms a detached TERM
    # for supervisor respawn — here a sacrificial wrapper shell plays the
    # service so pytest stays untouched
    import signal

    clone, _ = deploy_clone
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    bin_dir = tmp_path / "sbin"
    bin_dir.mkdir()
    stub = bin_dir / "systemctl"
    # report the script's parent (the wrapper) as the service MainPID:
    # stub -> command-substitution shell -> script -> wrapper, so walk up two
    stub.write_text(
        "#!/bin/sh\n"
        "ps -o ppid= -p $(ps -o ppid= -p $PPID | tr -d ' ') | tr -d ' '\n"
    )
    stub.chmod(0o755)
    nonroot = _nonroot_id_stub(tmp_path)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{nonroot}:{bin_dir}:{os.environ['PATH']}",
        "INVOCATION_ID": "test",
    }
    for var in ("RC_SVCNAME", "BRIDGE_SUPERVISED", "BRIDGE_PID"):
        env.pop(var, None)
    result = subprocess.run(
        # `exec` makes the sleep itself the TERM target's child that gets
        # killed; a plain `; sleep 30` would leave an orphaned sleep
        # reparented to init and visible to other tests' `ps` listings
        ["sh", "-c", "sh deploy/self-update.sh release; exec sleep 30"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == -signal.SIGTERM
    assert (clone / ".self-update-pending").exists()


def git_out(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_channel_with_no_matching_tag_fails(deploy_clone):
    clone, _ = deploy_clone
    result = check(clone, "v3")
    assert result.returncode == 1
    assert "no release tag matches channel 'v3'" in result.stdout


def test_withdrawn_tag_is_pruned(deploy_clone):
    clone, work = deploy_clone
    git(work, "push", "-q", "origin", ":refs/tags/v2.0.0")
    assert track_of(check(clone, "stable")) == "v1.2.1"
    result = check(clone, "v2.0.0")
    assert result.returncode == 1
    assert "no release tag matches channel 'v2.0.0'" in result.stdout


def test_malformed_version_pins_stay_branch_mode(deploy_clone):
    clone, _ = deploy_clone
    for channel in ("v01.2.3", "v1.02.3", "v1.2.03", "v02.0.0"):
        assert check(clone, channel).returncode != 0, channel


def test_tag_mode_requires_origin_main(deploy_clone, tmp_path: Path):
    clone, work = deploy_clone
    # delete main on the remote: bare HEAD must move first
    git(tmp_path / "origin.git", "symbolic-ref", "HEAD", "refs/heads/v2-hotfix")
    git(work, "push", "-q", "origin", ":refs/heads/main")
    result = check(clone, "stable")
    assert result.returncode == 1
    assert "release channels require fetching origin/main" in result.stdout


def test_force_pushed_main_still_resolves(deploy_clone):
    # a force-push rewinding main must update origin/main, not wedge it
    clone, work = deploy_clone
    git(work, "push", "-qf", "origin", "HEAD~1:main")
    assert track_of(check(clone, "stable")) == "v1.2.1"


def test_nonchannel_names_stay_branch_mode(deploy_clone):
    # 'latest'/'tags'-style aliases are not channels: they fall through to
    # branch mode and fail the branch fetch instead of silently tracking tags.
    clone, _ = deploy_clone
    assert check(clone, "latest").returncode != 0


def _path_without(tmp_path: Path, *names: str) -> str:
    """A PATH mirroring the current one minus the given executables."""
    bin_dir = tmp_path / "path-bin"
    bin_dir.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for entry in os.listdir(directory):
            target = bin_dir / entry
            if entry in names or target.exists():
                continue
            target.symlink_to(os.path.join(directory, entry))
    return str(bin_dir)


def test_update_without_flock_uses_windows_venv(deploy_clone, tmp_path: Path):
    # macOS and Git Bash have no flock, and Windows venvs use Scripts/
    clone, _ = deploy_clone
    pip = clone / ".venv" / "Scripts" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\ntouch pip-ran\n")
    pip.chmod(0o755)
    env = {**os.environ, **GIT_ENV, "PATH": _path_without(tmp_path, "flock")}
    env.pop("INVOCATION_ID", None)

    def run() -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", "deploy/self-update.sh", "main"],
            cwd=clone,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    lock = clone / ".self-update.lock.d"
    lock.mkdir()
    # a fresh lock without an owner pid is still being taken: keep off
    assert "another update is running" in run().stdout
    live = subprocess.Popen(["sleep", "30"])
    try:
        (lock / "pid").write_text(f"{live.pid}\n")
        assert "another update is running" in run().stdout
    finally:
        live.kill()
        live.wait()
    # the owner is gone (a killed update): the lock is reclaimed

    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / "pip-ran").exists()
    assert not (clone / ".self-update.lock.d").exists()


def test_bridge_supervised_caller_gets_termed(deploy_clone, tmp_path: Path):
    # launchd and deploy/windows/run.ps1 set BRIDGE_SUPERVISED: exiting is the
    # restart, whatever init system the host has
    import signal

    clone, _ = deploy_clone
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{_nonroot_id_stub(tmp_path)}:{os.environ['PATH']}",
        "BRIDGE_SUPERVISED": "1",
    }
    env.pop("INVOCATION_ID", None)
    env.pop("BRIDGE_PID", None)
    env.pop("RC_SVCNAME", None)
    result = subprocess.run(
        ["sh", "-c", "sh deploy/self-update.sh release; exec sleep 30"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == -signal.SIGTERM
    assert (clone / ".self-update-pending").exists()


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() != 0,
    reason="the OpenRC branch is root-only",
)
def test_openrc_restart_sweeps_orphan_supervisors(deploy_clone, tmp_path: Path):
    # rc-service only stops the pidfile-tracked supervise-daemon — orphans
    # keep their child holding the port and the new code dies on
    # EADDRINUSE, so the detached restart must sweep every supervise-daemon
    # for the service between stop and start
    import time

    clone, _ = deploy_clone
    calls = tmp_path / "calls"
    stubbin = tmp_path / "bin"
    stubbin.mkdir()
    for name in ("rc-service", "pkill"):
        stub = stubbin / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n')
        stub.chmod(0o755)
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{stubbin}:{os.environ['PATH']}",
    }
    env.pop("INVOCATION_ID", None)
    result = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # the restart is detached (sleep 2 + lock): wait for it to log calls
    for _ in range(80):
        if calls.exists() and calls.read_text().count("rc-service") >= 2:
            break
        time.sleep(0.1)
    lines = calls.read_text().splitlines() if calls.exists() else []
    rc_lines = [line for line in lines if line.startswith("rc-service")]
    pkill_lines = [line for line in lines if line.startswith("pkill")]
    assert [line.split()[-1] for line in rc_lines] == ["stop", "start"]
    # d[a]emon bracket: the pattern must not match the restart shell's own
    # cmdline (which carries the pattern text), or pkill kills the restart
    assert "supervise-d[a]emon" in (pkill_lines[0] if pkill_lines else "")


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() != 0,
    reason="the OpenRC branch is root-only",
)
def test_restart_command_runs_without_lock_fd(deploy_clone, tmp_path: Path):
    # the restart shell holds fd 9 on the update lock for serialization;
    # if the restart command inherits it, the new supervise-daemon holds
    # the flock forever and every later update reports "another update
    # is running"
    import time

    clone, _ = deploy_clone
    calls = tmp_path / "calls"
    stubbin = tmp_path / "bin"
    stubbin.mkdir()
    rcsvc = stubbin / "rc-service"
    rcsvc.write_text(
        '#!/bin/sh\n'
        'if [ -e /proc/self/fd/9 ] || [ -e /dev/fd/9 ]; then open=yes; else open=no; fi\n'
        f'echo "rc-service $* fd9=$open" >> "{calls}"\n'
    )
    rcsvc.chmod(0o755)
    pkill = stubbin / "pkill"
    pkill.write_text("#!/bin/sh\nexit 0\n")
    pkill.chmod(0o755)
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{stubbin}:{os.environ['PATH']}",
    }
    env.pop("INVOCATION_ID", None)
    result = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for _ in range(80):
        if calls.exists() and calls.read_text().count("rc-service") >= 2:
            break
        time.sleep(0.1)
    rc_lines = calls.read_text().splitlines() if calls.exists() else []
    assert [line.split()[-1] for line in rc_lines] == ["fd9=no", "fd9=no"]


def test_deploy_kicks_off_devin_update_without_lock_fd(
    deploy_clone, tmp_path: Path
):
    # the local backend's `devin acp` binary is refreshed by the same
    # deploy: a detached best-effort `devin update` that must not inherit
    # the update lock fd (a held fd would block every later update)
    import time

    clone, _ = deploy_clone
    calls = tmp_path / "calls"
    stubbin = tmp_path / "bin"
    stubbin.mkdir()
    devin = stubbin / "devin"
    devin.write_text(
        "#!/bin/sh\n"
        "if [ -e /proc/self/fd/9 ] || [ -e /dev/fd/9 ]; "
        "then open=yes; else open=no; fi\n"
        f'echo "$1 fd9=$open" >> "{calls}"\n'
    )
    devin.chmod(0o755)
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{_nonroot_id_stub(tmp_path)}:{stubbin}:{os.environ['PATH']}",
    }
    for var in ("INVOCATION_ID", "RC_SVCNAME", "BRIDGE_SUPERVISED", "BRIDGE_PID"):
        env.pop(var, None)
    result = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for _ in range(80):
        if calls.exists():
            break
        time.sleep(0.1)
    assert calls.exists()
    assert calls.read_text().splitlines() == ["update fd9=no"]


def test_failing_devin_update_does_not_fail_deploy(
    deploy_clone, tmp_path: Path
):
    # best-effort means exactly that: a `devin update` that errors (or is
    # absent from PATH on cloud-only hosts) must not fail the deploy
    clone, _ = deploy_clone
    stubbin = tmp_path / "bin"
    stubbin.mkdir()
    devin = stubbin / "devin"
    devin.write_text("#!/bin/sh\nexit 1\n")
    devin.chmod(0o755)
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{_nonroot_id_stub(tmp_path)}:{stubbin}:{os.environ['PATH']}",
    }
    for var in ("INVOCATION_ID", "RC_SVCNAME", "BRIDGE_SUPERVISED", "BRIDGE_PID"):
        env.pop(var, None)
    result = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / ".self-update-rev").exists()


def test_deploy_honours_devin_local_cli_from_dot_env(
    deploy_clone, tmp_path: Path
):
    # the bridge reads DEVIN_LOCAL_CLI from .env via Settings; the deploy
    # shell only sees exported vars, so it must fall back to the file or a
    # configured CLI is silently skipped while `devin` gets updated instead
    clone, _ = deploy_clone
    calls = tmp_path / "calls"
    stubbin = tmp_path / "bin"
    stubbin.mkdir()
    custom = stubbin / "custom-devin"
    custom.write_text(f'#!/bin/sh\necho "$1" >> "{calls}"\n')
    custom.chmod(0o755)
    (clone / ".env").write_text(
        f'DEVIN_LOCAL_CLI="{custom}"\nTELEGRAM_BOT_TOKEN=not-real\n'
    )
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{_nonroot_id_stub(tmp_path)}:{os.environ['PATH']}",
    }
    for var in (
        "INVOCATION_ID",
        "RC_SVCNAME",
        "BRIDGE_SUPERVISED",
        "BRIDGE_PID",
        "DEVIN_LOCAL_CLI",
    ):
        env.pop(var, None)
    result = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls.read_text().splitlines() == ["update"]


def test_deploy_without_devin_cli_still_succeeds(
    deploy_clone, tmp_path: Path
):
    # cloud-only hosts have no local CLI at all: the update is skipped and
    # the deploy completes — best-effort means missing is not a failure
    clone, _ = deploy_clone
    pip = clone / ".venv" / "bin" / "pip"
    pip.parent.mkdir(parents=True)
    pip.write_text("#!/bin/sh\nexit 0\n")
    pip.chmod(0o755)
    # a scrubbed PATH, not a stub: real devin binaries elsewhere on the
    # host must not be found (git lives in /usr/bin here)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    env = {
        **os.environ,
        **GIT_ENV,
        "PATH": f"{_nonroot_id_stub(tmp_path)}:{fakebin}:/bin:/usr/bin",
    }
    for var in (
        "INVOCATION_ID",
        "RC_SVCNAME",
        "BRIDGE_SUPERVISED",
        "BRIDGE_PID",
        "DEVIN_LOCAL_CLI",
    ):
        env.pop(var, None)
    result = subprocess.run(
        ["sh", "deploy/self-update.sh", "main"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / ".self-update-rev").exists()
