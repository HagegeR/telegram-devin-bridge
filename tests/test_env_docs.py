"""Every Settings field must be documented in both places a user looks:
`.env.example` (the copy-to-.env template) and `docs/configuration.md`
(the reference table). AGENTS.md names them as required surfaces for new
env vars — this test makes drift a CI failure instead of a docs review."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _settings_env_names() -> set[str]:
    src = (ROOT / "app" / "config.py").read_text()
    body = re.search(r"class Settings.*?(?=\nclass |\Z)", src, re.DOTALL)
    assert body, "Settings class not found in app/config.py"
    return {
        name.upper()
        for name in re.findall(r"^\s{4}([a-z][a-z0-9_]*)\s*:", body.group(0), re.MULTILINE)
    }


def _env_example_names() -> set[str]:
    names = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" in line:
            names.add(line.split("=", 1)[0].lstrip("#").strip())
    return names


def test_every_settings_field_is_in_env_example() -> None:
    missing = _settings_env_names() - _env_example_names()
    assert not missing, f".env.example is missing: {sorted(missing)}"


def test_every_settings_field_is_in_configuration_docs() -> None:
    docs = (ROOT / "docs" / "configuration.md").read_text()
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", docs))
    missing = _settings_env_names() - documented
    assert not missing, f"docs/configuration.md is missing: {sorted(missing)}"
