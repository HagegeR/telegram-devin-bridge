# Running the bridge on a small Alpine VM

Lessons from a low-end home VM: Alpine Linux, OpenRC, fewer than 4 cores,
less than 2 GB RAM, possibly shared with another deployment. Host-specific
values (addresses, MACs, user IDs, hypervisor details) belong in a private
note, not here.

## What runs at boot

`rc-update show default` should list at least
`crond dnsmasq ntpd sshd tailscale telegram-devin-bridge` (plus `docker` if
the Docker transcription backend or another deployment needs it).

- `telegram-devin-bridge`: OpenRC unit from `deploy/openrc/`, uses
  `supervise-daemon`, waits up to 60 s for DNS, then runs
  `uvicorn app.main:app --host 127.0.0.1 --port 8000` from the repo venv.
  Logs: `/var/log/telegram-devin-bridge.log` (+ `.err`). Health:
  `curl -s 127.0.0.1:8000/health` → `{"status":"ok"}`.
- `dnsmasq`: local DNS cache (`deploy/dnsmasq/local-cache.conf`);
  `/etc/resolv.conf` points at 127.0.0.1 and udhcpc must not overwrite it
  (`RESOLV_CONF="no"` in `/etc/udhcpc/udhcpc.conf`).
- `ntpd`: BusyBox ntpd. A fresh Alpine has no time sync; without it TLS and
  Tailscale eventually break.
- `tailscale`: Funnel → `127.0.0.1:8000`. In `TELEGRAM_MODE=polling` no
  webhook is registered, so Funnel is only needed for `/notify`, `/doctor` and
  `/admin` from cloud sessions.
- `crond`: `/etc/periodic/15min/telegram-devin-bridge-update` runs
  `deploy/self-update.sh` (log `/var/log/telegram-devin-bridge-update.log`).
  The host tracks `origin/main`; a checkout of another branch for live testing
  is reverted within 15 min unless merged.
- Swap: with < 2 GB RAM keep swap partitions in `/etc/fstab` and confirm
  `/proc/swaps` after boot.

## After a reboot

A VM reboot must bring everything back unattended. Check:
`rc-service telegram-devin-bridge status`, `curl -s 127.0.0.1:8000/health`,
`tailscale funnel status`, `cat /proc/swaps`, `ip route` (exactly one default
route), then `.venv/bin/python -m app.doctor`.

## Pitfalls that actually happened

- **Multiple default routes** (several NICs, only one with WAN) → DNS
  timeouts, `httpx.ConnectError: [Errno -3] Try again`, slow replies. Fix: one
  NIC, or `NO_GATEWAY="eth1 eth2"` in `/etc/udhcpc/udhcpc.conf`. `app.doctor`
  warns on extra default routes.
- `apk upgrade` that bumps the kernel removes the running kernel's modules:
  Docker (overlayfs) breaks until reboot. Reboot right after such upgrades.
- Telegram never shows a second "read" checkmark for bots — not a bridge bug.
- A rejected reaction emoji (`REACTION_INVALID`) used to abort the whole
  message; reactions are best-effort now, but only use emoji from Telegram's
  allowed reaction list.

## Rules of engagement

- Code reaches the host only via merge to `main` (self-update). Hand-editing
  on the host is for `.env` and emergencies.
- Never print `.env`, the bot token or API keys; redact when tailing logs.
- If another deployment shares the VM, do not touch its files, containers,
  routing or `udhcpc.conf`; do not add persistent services when RAM is tight.
