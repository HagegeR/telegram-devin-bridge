#!/bin/sh
# Cut a release: move CHANGELOG.md Unreleased entries under the new version
# heading, push main, tag the head, push the tag (release.yml publishes it).
# Usage: deploy/release.sh vX.Y.Z   (run from a clean main checkout)
set -eu

TAG=${1:?"usage: $0 vX.Y.Z"}
echo "$TAG" | grep -qE '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ||
  { echo "error: '$TAG' is not a strict vX.Y.Z semver" >&2; exit 1; }
VER=${TAG#v}

cd "$(git rev-parse --show-toplevel)"
[ "$(git branch --show-current)" = main ] ||
  { echo "error: run from main" >&2; exit 1; }
[ -z "$(git status --porcelain)" ] ||
  { echo "error: working tree not clean" >&2; exit 1; }
git fetch -q origin '+refs/tags/v*:refs/tags/v*'
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "error: tag $TAG already exists" >&2
  exit 1
fi
git pull -q --ff-only

if ! grep -qE "^## \[v?$VER\]" CHANGELOG.md; then
  # check before editing so a failure leaves the tree clean
  awk '$0 == "## [Unreleased]" { found=1; next } /^## / { found=0 } found' \
    CHANGELOG.md | grep -q . ||
    { echo "error: CHANGELOG.md Unreleased is empty — write entries first" >&2
      exit 1; }
  # move the Unreleased body under a new version heading
  sed -i "s/^## \[Unreleased\]$/## [Unreleased]\n\n## [$TAG] - $(date +%F)/" \
    CHANGELOG.md
fi
# same extraction release.yml gates on — fail here, not in CI
awk -v ver="$VER" '
  $0 ~ "^## \\[v?" ver "\\]" { found=1; next }
  /^## / { found=0 }
  found' CHANGELOG.md | grep -q . ||
  { echo "error: no changelog entries for $TAG" >&2; exit 1; }
git diff --quiet CHANGELOG.md || {
  git commit -qam "docs: $TAG changelog section"
  git push -q origin main || {
    # protected main: keep the commit on a side branch for a PR and realign
    # local main so a squash merge cannot strand the retry on a divergent ref
    git push -q origin "HEAD:refs/heads/release/$TAG-changelog"
    git reset -q --hard origin/main
    echo "error: main is protected — open a PR from release/$TAG-changelog," >&2
    echo "  merge it, then re-run: $0 $TAG" >&2
    exit 1
  }
}

git tag -a "$TAG" -m "$TAG"
git push -q origin "$TAG"
echo "tagged $TAG — release.yml publishes the GitHub release"
