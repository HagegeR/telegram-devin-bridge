# Deployment: Alpine Linux + Tailscale Funnel

![The bridge host reaches Telegram through a Tailscale Funnel](assets/deployment-funnel.jpg)

Runbook for hosting the bridge on a small Alpine host, exposed to Telegram via
Tailscale Funnel — the VM keeps no open inbound ports; the Funnel terminates
TLS and forwards `https://<host>.<tailnet>.ts.net` to `127.0.0.1:8000`.
Tested on Alpine 3.23, kernel 6.18, x86_64. Docker is **not** required.

## 1. Prerequisites

```sh
apk add python3 py3-pip git
python3 -m venv --help   # sanity check
```

`deploy/self-update.sh` requires `flock`. Alpine's BusyBox provides it; if
`command -v flock` fails, run `apk add util-linux-misc`.

## 2. Install the app

```sh
git clone https://github.com/HagegeR/telegram-devin-bridge /root/telegram-devin-bridge
cd /root/telegram-devin-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Configure `.env`

```sh
cp .env.example .env && chmod 600 .env
```

Pitfalls (all hit in practice):

- **Delete empty optional lines** — `.env.example` ships `TELEGRAM_HOME_CHANNEL=`,
  `BOT_USERNAME=`, `DEVIN_SESSION_INSTRUCTIONS=`; an empty string fails
  `int | None` parsing and pydantic refuses to start. Remove the lines entirely.
- Generate secrets: `openssl rand -hex 32` for `TELEGRAM_WEBHOOK_SECRET` and
  `DOCTOR_SECRET` (a separate secret from `NOTIFY_SECRET`).
- Use an **absolute** `DATABASE_PATH` (e.g. `/root/telegram-devin-bridge/bridge.sqlite3`).
- Set `TELEGRAM_ALLOWED_USERS` (your Telegram user ID) or the bot will deny everyone.
- Use a **dedicated bot token** — a token already used by another poller or
  webhook consumer conflicts.
- `DEVIN_API_KEY` must be a v1 API key (`apk_user_…` personal or `apk_…`
  service key from Settings → API Keys). A service-user token (`cog_…`) is
  NOT accepted by /v1 (403 Unauthorized) — it goes in
  `DEVIN_SERVICE_USER_API_KEY` together with `DEVIN_ORG_ID` and is only used
  for `/usage`.

## 4. Tailscale Funnel

```sh
apk add tailscale
rc-update add tailscale default
rc-service tailscale start
tailscale up --hostname devin-bridge   # prints a login URL — open it once
```

In the admin console:

- **DNS page** → enable **HTTPS Certificates**.
- **Access controls** (https://login.tailscale.com/admin/acls/file) → add:

```json
"nodeAttrs": [{"target": ["autogroup:member"], "attr": ["funnel"]}]
```

If these steps are missing, `tailscale funnel` fails with greppable errors:

- `Funnel not available; HTTPS must be enabled. See https://tailscale.com/s/https.`
  → enable HTTPS certificates on the DNS page.
- `Funnel not available; "funnel" node attribute not set. See
  https://tailscale.com/s/no-funnel.` → add the `nodeAttrs` block to the ACL.

Then:

```sh
tailscale set --accept-dns=false   # keep MagicDNS out of /etc/resolv.conf
tailscale funnel --bg 8000
```

Public URL is `https://<hostname>.<tailnet>.ts.net` (e.g.
`https://devin-bridge.tail12b72d.ts.net`) — set it as `PUBLIC_BASE_URL` in
`.env`. Funnel config is stored in tailscaled and persists across reboots.

## 5. OpenRC service

`deploy/openrc/telegram-devin-bridge` is the **webhook + Tailscale Funnel**
unit: it runs uvicorn as root from `/root/telegram-devin-bridge` under
supervise-daemon with respawn. Do not confuse it with `deploy/vm/install.sh`,
which is the **polling-mode** installer (service account, `/opt`, `app.poll`) —
pick one deployment style, not both.

```sh
cp deploy/openrc/telegram-devin-bridge /etc/init.d/telegram-devin-bridge
chmod +x /etc/init.d/telegram-devin-bridge
rc-update add telegram-devin-bridge default
rc-service telegram-devin-bridge start
curl -s http://127.0.0.1:8000/health
curl -s https://<hostname>.<tailnet>.ts.net/health
```

Logs go to `/var/log/telegram-devin-bridge.log`.

Boot persistence is: `rc-update add` for `tailscale`, `dnsmasq`, and
`telegram-devin-bridge`; the funnel config stored in tailscaled state;
`RESOLV_CONF="no"` / `NO_GATEWAY` in `/etc/udhcpc/udhcpc.conf`; and the MTU
`post-up` line in `/etc/network/interfaces`. The unit's `start_pre` waits up
to 60s for DNS before starting (never fails the boot), and `depend()` orders
after `tailscale`/`dnsmasq`/`dns`.

## 6. Register the webhook

```sh
cd /root/telegram-devin-bridge
.venv/bin/python -m app.set_webhook
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

`url` must equal `<PUBLIC_BASE_URL>/telegram/webhook`; watch
`pending_update_count` and `last_error_message`.

## 7. Host network hardening (things that actually broke)

- **Local DNS cache** (ISP resolvers flaked and blocked api.telegram.org):

  ```sh
  apk add dnsmasq
  cp deploy/dnsmasq/local-cache.conf /etc/dnsmasq.d/
  echo 'conf-dir=/etc/dnsmasq.d,*.conf' >> /etc/dnsmasq.conf
  printf 'nameserver 127.0.0.1\noptions timeout:2 attempts:3\n' > /etc/resolv.conf
  ```

  Prevent udhcpc from overwriting resolv.conf: set `RESOLV_CONF="no"` in
  `/etc/udhcpc/udhcpc.conf`. Then `rc-update add dnsmasq default && rc-service
  dnsmasq start`. The `all-servers` option queries all upstreams in parallel,
  which masks individual resolver packet loss.

- **Second NIC with a dead WAN path** blackholes outbound traffic whenever the
  default route flips to it: set `NO_GATEWAY="eth1"` in
  `/etc/udhcpc/udhcpc.conf` and `ip route del default dev eth1`.

- **Jumbo MTU** (`mtu 9000` on a 1500-byte LAN) silently drops frames. Persist
  with a `post-up` line under the iface in `/etc/network/interfaces`:

  ```
  iface eth0 inet dhcp
      post-up ip link set dev eth0 mtu 1500
  ```

## 8. Diagnostics

```sh
.venv/bin/python -m app.doctor              # human-readable checks
.venv/bin/python -m app.doctor --json
curl -H "Authorization: Bearer $DOCTOR_SECRET" http://127.0.0.1:8000/doctor
```

Useful commands:

```sh
rc-service telegram-devin-bridge status|restart
tail -f /var/log/telegram-devin-bridge.log
tailscale funnel status
```

## Self-update

`deploy/self-update.sh` takes a host-wide lock and records a deploy marker, pulls `origin/main` (or
`SELF_UPDATE_BRANCH`), checks out the remote head — the host checkout is
deploy-only and local changes are discarded on purpose — reinstalls requirements
if `requirements.txt` changed, and restarts the service detached. Admins can
trigger it from Telegram with
`/update` (or `/update check` for a dry run; requires
`TELEGRAM_ADMIN_USER_IDS`).

Cron install (busybox run-parts requires NO file extension):

```sh
cp deploy/openrc/telegram-devin-bridge-update /etc/periodic/15min/
chmod +x /etc/periodic/15min/telegram-devin-bridge-update
```

The branch is configured once in `/etc/conf.d/telegram-devin-bridge`
(`SELF_UPDATE_BRANCH="main"`) and read by both the service environment and
the cron wrapper — set it there, not in the cron file.

## Remote control from Devin cloud sessions

Two paths; use the narrowest that works. Details for the session side live in
`docs/devin-knowledge.md`.

### Admin API (narrow, no shell)

Set `ADMIN_SECRET` (`openssl rand -hex 32`) in `.env` and restart. Give the
Devin session the secrets `BRIDGE_PUBLIC_BASE_URL` (the funnel URL) and
`BRIDGE_ADMIN_SECRET`. Actions: `doctor`, `logs`, `get-env`, `set-env`
(allowlisted keys only), `restart`, `update` — see README "Admin API".

### Tailscale SSH (full shell)

On the VM:

```sh
tailscale set --ssh
```

> **Tagging the VM (`tag:bridge`)**: Tailscale SSH rules only accept tags (or
> `autogroup:self`) as `dst`, not `hosts` aliases, so the VM must carry a tag.
> `tailscale up --advertise-tags=tag:bridge --hostname=devin-bridge --ssh --accept-dns=false`
> forces a **re-login** (it prints an auth URL) and the Funnel is offline until
> you authenticate. Before that: add `tag:bridge` to `tagOwners` **and** to the
> funnel `nodeAttrs` target (a tagged node is no longer in `autogroup:member`).
> After login, re-toggle the funnel: `tailscale funnel --https=443 off && tailscale funnel --bg 8000`.


In the admin console ACL (https://login.tailscale.com/admin/acls/file):

```json
"tagOwners": {"tag:devin": ["autogroup:admin"]},
"hosts": {"devin-bridge": "100.127.21.58"},
```

an `acls` entry `{"action":"accept","src":["tag:devin"],"dst":["devin-bridge:22"]}`,
and an `ssh` entry
`{"action":"accept","src":["tag:devin"],"dst":["devin-bridge"],"users":["root"]}`.

Then Settings → Keys → Generate auth key: **Reusable, Ephemeral,
Pre-approved**, Tags `tag:devin`; store it as the Devin secret
`TAILSCALE_AUTHKEY`.

Tailscale SSH authenticates by tailnet identity — it does **not** use
sshd/`authorized_keys`; `tailscale set --ssh` only affects tailnet
connections, LAN sshd is unchanged.

## 9. Alternative: polling mode

`TELEGRAM_MODE=polling` needs no public URL, funnel, or webhook — see
`deploy/vm/install.sh` for a service-account install using
`python -m app.poll`.

## Devin Knowledge

`docs/devin-knowledge.md` documents this deployment for Devin itself. Publish
it with:

```sh
.venv/bin/python -m app.publish_knowledge            # uses DEVIN_API_KEY
.venv/bin/python -m app.publish_knowledge --dry-run  # show payload
```
