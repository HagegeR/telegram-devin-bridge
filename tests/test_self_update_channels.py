"""Shell-level coverage for deploy/self-update.sh channel resolution.

Builds a bare 'origin' plus a deploy clone in a tmp dir and runs the real
script with --check, asserting which revision each channel resolves to.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

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


@pytest.fixture()
def deploy_clone(tmp_path: Path):
    """Bare origin on main + a clone kept one release behind, like the host."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    clone = tmp_path / "clone"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-b", "main", str(work))
    git(work, "remote", "add", "origin", str(origin))

    def commit(name: str) -> None:
        (work / name).write_text(name)
        git(work, "add", name)
        git(work, "commit", "-qm", name)

    commit("a")  # clone's HEAD stays here
    git(work, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(clone))

    commit("b")
    git(work, "tag", "-a", "v1.2.0", "-m", "v1.2.0")
    commit("c")
    git(work, "tag", "-a", "v1.2.1", "-m", "v1.2.1")
    commit("d")
    git(work, "tag", "-a", "v2.0.0", "-m", "v2.0.0")
    git(work, "push", "-q", "origin", "main", "v1.2.0", "v1.2.1", "v2.0.0")

    # a branch whose name looks version-ish must stay a branch channel
    git(work, "push", "-q", "origin", "main:refs/heads/v2-hotfix")
    # a tag on a commit not merged into main must never be selected
    git(work, "checkout", "-qb", "side", "HEAD~1")
    commit("e")
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


def test_versionish_branch_name_stays_a_branch(deploy_clone):
    clone, _ = deploy_clone
    assert track_of(check(clone, "v2-hotfix")) == "v2-hotfix"


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


def test_nonchannel_names_stay_branch_mode(deploy_clone):
    # 'latest'/'tags'-style aliases are not channels: they fall through to
    # branch mode and fail the branch fetch instead of silently tracking tags.
    clone, _ = deploy_clone
    assert check(clone, "latest").returncode != 0
