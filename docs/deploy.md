# Deploying ctrlrtn

The proxy is a single lightweight process (uvicorn + SQLite). Deploying it is
mostly about *where it listens*. Its security model is: only things you trust
can reach it.

## The security model, first

- **The `/ctrlrtn/` control plane is unauthenticated by design** (localhost
  assumption). Anyone who can reach the proxy can post to `/ctrlrtn/outcome`,
  `/ctrlrtn/workflow-events` and `/ctrlrtn/tool-operation-events`, reporting
  outcomes and step events for any task, and so poison experiment statistics.
  A Host-header check blocks DNS rebinding (a browser page reaching a
  loopback-bound port through a rebound domain). If your apps address the
  proxy by a DNS name, including through a TLS proxy in front of it, add that
  name to `control_hosts`.
- **Anyone who can reach the proxy can send traffic through it**, with their
  own key, polluting your recorded corpus. With a named provider that has a
  `credential:` block the proxy does hold a key: every request on that mount
  is sent on the operator's credential, whatever the client sent, and
  nothing authenticates the client. Such requests get the same Host check as
  the control plane, so a rebound browser page is refused, but every host
  that can reach the port can spend on that key. Put a `budgets` ceiling on
  it and treat the whole listener as sensitive, not only the control plane.
- **The database contains your full prompts and responses.** Credentials are
  redacted at capture time and never stored. The redaction is a named list:
  the standard headers (`authorization`, `x-api-key`, `api-key`, cookies) and
  common `?key=`-style query parameters. An upstream using an exotic custom
  credential carrier is out of scope. The recorded *content* remains as
  sensitive as your application's data, so treat backups accordingly.
- Experiments and routes are controlled via the **CLI on the box** (they
  write the SQLite directly). There is no remote admin surface to secure.
- A shadow experiment discloses each selected live prompt to its candidate
  provider. Confirm data residency and provider policy before starting one.
  Candidate responses are retained even though users never see them, and the
  calls still consume provider quota, bandwidth, and billed tokens.
- `replay-eval`, `calibration-set` and `calibrate` send recorded prompts,
  tool results and both models' outputs to the Anthropic API directly, not
  through the proxy, and `calibration-set` writes prompts and outputs to a
  local file. Handle that file like the database.
- The proxy's own log line names the path, never the query string, and
  uvicorn's access log is off, so a `?key=` credential does not reach the
  journal. `log_level: debug` adds task ids, use-case keys and model names.

Consequence: **never expose the proxy publicly.** Loopback by default; a
private network plus a firewall when it must cross hosts.

## Topology A: same box as your app (recommended)

Run the proxy next to the application it fronts, bound to `127.0.0.1`
(the default). This is the strongest isolation available: the proxy has no
network presence, and the app's API key never leaves the box.

```bash
# on the server, as root. Read it first; it is short.
git clone https://github.com/arnovich/ctrlrtn /tmp/ctrlrtn \
  && sudo /tmp/ctrlrtn/deploy/install.sh
```

`deploy/install.sh` runs as root and is **idempotent**. It creates a
`ctrlrtn` system user, installs `uv` from `astral.sh` if it is missing,
checks out `/opt/ctrlrtn` at `--ref` (default `main`; pass a tag for a
pinned deployment), and writes `/etc/ctrlrtn/config.yaml` and
`/etc/ctrlrtn/env` once, never overwriting them. It then installs the
sandboxed systemd unit `deploy/ctrlrtn.service`, replacing any local edits
to the unit, with state in `/var/lib/ctrlrtn`, and verifies `/healthz`.
**Re-running it is the upgrade path.** Provider-owned credentials
(`providers.<name>.credential.env`) go in `/etc/ctrlrtn/env` as
`KEY=value` lines; the unit reads it as its `EnvironmentFile`, and it is
`root:ctrlrtn` mode `0640`.

Then point the app at it (its systemd env file or `.env`):

```
ANTHROPIC_BASE_URL=http://127.0.0.1:4000
OPENAI_BASE_URL=http://127.0.0.1:4000/v1
```

## Topology B: a dedicated proxy box (multi-app)

When several apps share one proxy, give it its own host:

1. Put the proxy and app hosts on a **private network**; bind the proxy to
   its private IP (`host:` in the config).
2. Firewall the proxy port to the app hosts' private IPs only (a network
   firewall or nftables). The public interface serves nothing.
3. Apps use `ANTHROPIC_BASE_URL=http://<proxy-private-ip>:4000`. Keys and
   full prompts cross the private network as plain HTTP, which is acceptable
   on an isolated LAN; put a TLS-terminating proxy in front otherwise, and
   add its DNS name to `control_hosts`. Every app host on that network can
   also spend on any provider-owned credential the proxy holds.
4. **Namespace your route tags per app** (`x-ctrlrtn-route: newsroom:editor`,
   `support:editor`): tag keys are global to a proxy, and two apps both
   sending `tag:editor` would silently merge their statistics.

## Docker

```bash
docker compose up -d        # see compose.yaml
```

Keep the port published on `127.0.0.1:4000:4000`. Docker's port publishing
**bypasses ufw-style host firewalls**; a bare `4000:4000` silently exposes the
proxy to the internet. Inside the container the proxy binds `0.0.0.0`; the
published address decides reachability. Every `CTRLRTN_*` environment
variable works in the container.

## Day-two operations

`/usr/local/bin/ctrlrtn` is a wrapper the installer adds so every CLI
invocation reads the service config, and therefore the same absolute
`db_path` as `serve`. Run it as the service user: `sudo -u ctrlrtn ctrlrtn`.
Console and CLI writes land in the same SQLite state and take effect on the
proxy's next snapshot refresh, normally within about 10 seconds.

| Task | Command | Note |
| --- | --- | --- |
| Check health | `curl -sf http://127.0.0.1:4000/healthz` | Prints `ok`. The installer polls it for up to 30 seconds after a restart. |
| Read the logs | `journalctl -u ctrlrtn -f` | One line per recorded call when `log_requests` is on. |
| Watch it over SSH | `ssh -t HOST sudo -u ctrlrtn ctrlrtn console` | Add `--routing-config` and `--routing-repo` for the `g` preview-and-activate action; the service user needs read access to that Git checkout, and no network Git credentials are needed. Keys are in [console.md](console.md). |
| Inspect spend | `ctrlrtn usecases`, `ctrlrtn spend`, `ctrlrtn budget` | All on-box; see [configure.md](configure.md) for what each reports. |
| Back up | `sudo -u ctrlrtn sqlite3 /var/lib/ctrlrtn/router.db ".backup /var/lib/ctrlrtn/backup-$(date +%a).db"` | Cron it. The database holds your routes and experiments, not just recordings; losing it silently reverts every switch to pass-through. The content is sensitive. SQLite with WAL needs a local filesystem: never put the live database on a network mount. |
| Upgrade | Re-run `deploy/install.sh` (the clone-and-run pair above) | It rebuilds the venv from scratch and waits up to 30 seconds for `/healthz`, so a re-run either upgrades the box or exits non-zero; it never leaves the checkout on the new sha with the old code serving. Restart is quick; in-flight LLM calls fail and clients retry. |
| Prune | `ctrlrtn prune --older-than-days 30`, then add `--apply` | The default is a dry run; apply only after reviewing the count. Pruning clears query strings, headers, and bodies for selected traces in one transaction. Derived metrics stay, and payloads frozen by queued or running jobs are reported and skipped. A positive `retention_days` in the config applies the same job-aware prune before `serve` accepts traffic and fails startup if it cannot. |
| Compact | `ctrlrtn prune --older-than-days 30 --apply --compact` | Stop every proxy, worker, and mutating CLI on the database first. Compaction takes the exclusive maintenance lock, checkpoints the WAL, and runs `VACUUM`. It fails if another ctrlrtn writer is open or an external SQLite user keeps the checkpoint busy. The lock needs POSIX advisory locks; ordinary serving works without them, but compaction fails closed. |
| Rotate the price table | `systemctl edit ctrlrtn` to add `Environment=CTRLRTN_PRICES=/etc/ctrlrtn/prices.toml`, then `systemctl restart ctrlrtn` | Prices are read once at startup. The file format is in [configure.md](configure.md). |

`prune` is logical deletion and `--compact` reclaims disk; neither touches
backups or snapshots, and neither is cryptographic erasure.
