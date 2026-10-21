#!/usr/bin/env python3
"""Print pyproject.toml requirements for plain `pip install`.

Deploy hosts can carry a pip too old for `--group` (PEP 735) and the app is
never packaged, so `pip install .` is not an option: this emits
requirements-format lines — always [project.dependencies] plus each named
[dependency-groups] entry — for `pip install $DEP`-style consumers.
"""
import sys
import tomllib
from pathlib import Path

data = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
)
deps = list(data["project"]["dependencies"])
groups = data.get("dependency-groups", {})
for name in sys.argv[1:]:
    deps += groups[name]
print("\n".join(deps))
