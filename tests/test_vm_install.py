"""Shell-level coverage for the zypper retry in deploy/vm/install.sh.

Runs the real installer with a stub-only PATH: fake ``id`` reports root,
a fake ``zypper`` fails a set number of times before succeeding (counting
invocations in a file), and a fake ``python3.14`` vends a no-op venv so
nothing real is installed. Every other command the script needs is
symlinked from /bin into the stub dir, which also hides apt-get/systemctl
so the zypper branch and the manual-init path are selected.
"""

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="deploy/vm/install.sh is POSIX-only"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "vm" / "install.sh"

# real commands install.sh calls outside stubs
TOOLS = [
    "dirname", "tar", "mkdir", "cp", "rm", "rmdir", "sed", "grep", "mv",
    "chmod", "cat", "touch", "find", "env", "printf", "uname",
]

ZYPER_STUB = """#!/bin/sh
n=$(cat "$ZYPPER_COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$ZYPPER_COUNT"
echo "zypper $*" >> "$ZYPPER_LOG"
if [ "$n" -le "${ZYPPER_FAILS:-0}" ]; then exit 104; fi
exit 0
"""

PY_STUB = """#!/bin/sh
if [ "$1" = "-c" ]; then exit 0; fi
if [ "$1" = "--version" ]; then echo "Python 3.14.0"; exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    mkdir -p "$3/bin"
    printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/python"
    chmod +x "$3/bin/python"
    exit 0
fi
exit 0
"""

STUBS = {
    "id": '#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; else exit 0; fi\n',
    "zypper": ZYPER_STUB,
    "python3.14": PY_STUB,
    "chown": "#!/bin/sh\nexit 0\n",
    "sleep": "#!/bin/sh\nexit 0\n",
}


def run_install(tmp_path: Path, fails: int) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        stub = bin_dir / name
        stub.write_text(body)
        stub.chmod(0o755)
    for tool in TOOLS:
        for base in ("/usr/bin", "/bin"):
            target = Path(base) / tool
            if target.exists():
                (bin_dir / tool).symlink_to(target)
                break
    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "BRIDGE_HOME": str(tmp_path / "opt"),
        "DATA_DIR": str(tmp_path / "data"),
        "ZYPPER_COUNT": str(tmp_path / "count"),
        "ZYPPER_LOG": str(tmp_path / "zypper.log"),
        "ZYPPER_FAILS": str(fails),
    }
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_zypper_retries_past_a_transient_mirror_failure(tmp_path: Path) -> None:
    result = run_install(tmp_path, fails=1)
    assert result.returncode == 0, result.stderr + result.stdout
    assert (tmp_path / "count").read_text().strip() == "2"


def test_zypper_gives_up_after_three_failures(tmp_path: Path) -> None:
    result = run_install(tmp_path, fails=99)
    assert result.returncode != 0
    assert (tmp_path / "count").read_text().strip() == "3"
