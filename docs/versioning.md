# Versioning and release channels

The bridge uses [SemVer](https://semver.org/) tags (`vMAJOR.MINOR.PATCH`) on
`main`, and every deployment picks an **update channel** that decides which
revisions it receives.

## Update channels

Set `SELF_UPDATE_CHANNEL` where the updater reads its environment
(`/etc/conf.d/telegram-devin-bridge` on the reference Alpine host, or the
service environment for `deploy/vm` installs). The legacy
`SELF_UPDATE_BRANCH` still works as a fallback when no channel is set.

| Channel | You get | Pick it when |
| --- | --- | --- |
| `main` (or any branch name) | latest `origin/main` — the classic behavior | You want everything the moment it merges |
| `stable` | newest `vX.Y.Z` tag reachable from `origin/main` | You want releases only, never unreleased work |
| `v1` | newest tag within major 1 (`v1.*.*`) | Stability: features + fixes, no breaking changes |
| `v1.2` | newest tag within minor 1.2 (`v1.2.*`) | Maximum stability: fixes only |
| `v1.2.3` | exactly that tag | Pinning a known-good build |

`SELF_UPDATE_CHANNEL` is a code-selection knob, so like `SELF_UPDATE_COMMAND`
and `SELF_UPDATE_BRANCH` it can never be set through the `/admin` API.

`/update` in Telegram and the admin `update` action both honor the channel —
the updater resolves it the same way either path invokes it.

## Bump rules

Decide the next tag's bump by the *worst* change since the last tag:

- **MAJOR** (`vX.0.0`) — anything breaking for an existing deployment:
  an env var renamed, removed, or changed in meaning; a required setting added;
  an endpoint contract changed (admin actions, `/notify` payload, webhook
  route); a bot command removed or its behavior inverted; a deploy mode
  dropped; a DB schema change that can't migrate itself.
- **MINOR** (`vX.Y.0`) — backward-compatible features: new bot commands,
  endpoints, optional env vars, transcription backends, deploy options, or
  behavior that is additive only.
- **PATCH** (`vX.Y.Z`) — fixes and chores only: bug fixes, docs, internal
  refactors, dependency bumps that change no external behavior.

When in doubt between MINOR and MAJOR: if a correctly-configured existing
`.env` could break or change meaning on update, it's MAJOR.

## Cutting a release

1. Land the changes on `main` through PRs as usual.
2. Update [CHANGELOG.md](../CHANGELOG.md): move the `Unreleased` entries under
   a new `## [vX.Y.Z] - YYYY-MM-DD` heading and merge that (it can ride along
   in the last feature PR).
3. Tag the merge commit and push:

   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

4. Create the GitHub release (`gh release create vX.Y.Z --generate-notes`) —
   GitHub's auto-notes plus the changelog section are the release body.

Hosts on `stable`, `vX`, or `vX.Y` pick the new tag up on their next
self-update run (cron or `/update`); hosts on `main` already had the code and
see no effective change.

## What is *not* versioned

There is no version string inside the app — a deployment's version is the git
revision it runs (`git rev-parse HEAD` on the host, or the self-update log;
`git describe --tags` also resolves on tag-channel hosts since they fetch
tags). Keep it that way: bumping a `__version__` would add a second source of
truth.
