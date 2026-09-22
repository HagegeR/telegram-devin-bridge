# Production hardening checklist

The defaults are safe to develop against; this is what to tighten before
exposing a deployment to anyone but yourself.

## Access

- [ ] `TELEGRAM_ALLOWED_USERS` / `TELEGRAM_ALLOWED_CHAT_IDS` are set, and
      `TELEGRAM_ADMIN_USER_IDS` is limited to admins.
- [ ] `TELEGRAM_ALLOW_ALL_USERS` stays `false` unless you truly intend a
      public bot — with it on, **anyone** who finds the bot can run Devin
      sessions on your account and ACU budget.
- [ ] `TELEGRAM_ALLOW_ALL_USERS` is never combined with admin commands — keep
      `TELEGRAM_ADMIN_USER_IDS` set regardless.

## Secrets

- [ ] Every `*_SECRET` value is a real random token (`openssl rand -hex 32`),
      not a placeholder — `python -m app.doctor` flags leftover
      `replace-with-*` values.
- [ ] Rotate `TELEGRAM_WEBHOOK_SECRET`, `NOTIFY_SECRET`, `DOCTOR_SECRET`, and
      `ADMIN_SECRET` on a schedule and after any suspected exposure — they are
      deliberately not in the `/admin set-env` allowlist, so edit `.env` on
      the host and restart the service.
- [ ] `ADMIN_SECRET` is unset on hosts that don't need the `/admin` API — the
      endpoint is disabled entirely when the secret is unset.
- [ ] The Devin key is a dedicated **v1** key with the smallest usable scope.

## Host and network

- [ ] The bridge runs as an unprivileged user — the Dockerfile already
      creates one (`bridge`, uid 10001); match that on VM installs.
- [ ] Webhook mode is behind HTTPS (Tailscale Funnel or your reverse proxy);
      never expose port 8000 plaintext.
- [ ] `TELEGRAM_HOME_CHANNEL` / `NOTIFY_SECRET` targets are reviewable — they
      decide where notifications land.
- [ ] `.env` is mode `600` and outside the checkout or covered by `.gitignore`
      (the default layout already is).

## Operations

- [ ] `python -m app.doctor` is clean after every deploy and upgrade.
- [ ] `SELF_UPDATE_CHANNEL` is set to `stable` or a version pin rather than
      `main` on hosts that shouldn't ride unreleased commits.
- [ ] Backups of `DATABASE_PATH` run on a schedule — a plain copy while the
      process is stopped, or `sqlite3` `.backup` online.
- [ ] Logging excludes secrets — the bridge redacts configured secret values
      in admin log output; keep tokens out of command text sent to the bot.
