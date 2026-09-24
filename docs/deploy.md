# Deploying ctrlrtn

The router is a single lightweight process (uvicorn + SQLite). Deploying it is
mostly about *where it listens*. Its security model is: only things you trust
can reach it.

## The security model, first

- **The `/ctrlrtn/` control plane is unauthenticated by design** (localhost
  assumption). Anyone who can reach the router can `POST /ctrlrtn/outcome` and
  poison your experiment statistics. A Host-header check blocks DNS rebinding
  (browser pages reaching a loopback-bound port via a rebound domain). If your
  apps address the router by an internal DNS name, add it to `control_hosts`.
- **Anyone who can reach the proxy can send traffic through it** (with their
  own key; the router holds none), polluting your recorded corpus.
- **The database contains your full prompts and responses.** Credentials are
  redacted at capture time and never stored: a named list covering the
  standard headers (`authorization`, `x-api-key`, `api-key`, cookies) and
  common `?key=`-style query parameters. An upstream using an exotic custom
  credential carrier is out of scope. The recorded *content* remains as
  sensitive as your application's data, so treat backups accordingly.
- Experiments and routes are controlled via the **CLI on the box** (they
  write the SQLite directly). There is no remote admin surface to secure.
- A shadow experiment discloses each selected live prompt to its candidate
  provider. Confirm data residency and provider policy before starting one.
  Candidate responses are retained even though users never see them, and the
  calls still consume provider quota, bandwidth, and billed tokens.

Consequence: **never expose the router publicly.** Loopback by default; a
private network plus a firewall when it must cross hosts.

## Topology A: same box as your app (recommended)

Run the router next to the application it fronts, bound to `127.0.0.1`
(the default). This is the strongest isolation available: the router has no
network presence, and the app's API key never leaves the box.

```bash
# on the server, as root. Read it first; it is short.
git clone https://github.com/arnovich/ctrlrtn /tmp/ctrlrtn \
  && sudo /tmp/ctrlrtn/deploy/install.sh
```

`deploy/install.sh` is **idempotent**: it creates a `ctrlrtn` system user,
installs into `/opt/ctrlrtn` (pinned to `--ref`, default `main`), writes
`/etc/ctrlrtn/config.yaml` once (never overwritten), installs the hardened
systemd unit `deploy/ctrlrtn.service` with state in `/var/lib/ctrlrtn`, and
verifies `/healthz`. **Re-running it is the upgrade path.**

Then point the app at it (its systemd env file or `.env`):

```
ANTHROPIC_BASE_URL=http://127.0.0.1:4000     # or OPENAI_BASE_URL
```

## Topology B: a dedicated router box (multi-app)

When several apps share one router, give it its own host:

1. Put the router and app hosts on a **private network**; bind the router to
   its private IP (`host:` in the config).
2. Firewall the router port to the app hosts' private IPs only (a network
   firewall or nftables). The public interface serves nothing.
3. Apps use `ANTHROPIC_BASE_URL=http://<router-private-ip>:4000`. Keys
   transit the private network, which is acceptable on an isolated LAN; put a
   TLS-terminating proxy in front if your threat model needs more.
4. **Namespace your route tags per app** (`x-ctrlrtn-route: newsroom:editor`,
   `support:editor`): tag keys are global to a router, and two apps both
   sending `tag:editor` would silently merge their statistics.

## Docker

```bash
docker compose up -d        # see compose.yaml
```

Keep the port published on `127.0.0.1:4000:4000`. Docker's port publishing
**bypasses ufw-style host firewalls**; a bare `4000:4000` silently exposes the
router to the internet. Inside the container the router binds `0.0.0.0`; the
published address decides reachability. Every `CTRLRTN_*` environment
variable works in the container.

## Operating it

```bash
sudo -u ctrlrtn ctrlrtn usecases          # spend per use-case
sudo -u ctrlrtn ctrlrtn console           # monitor + confirmed controls over ssh -t
sudo -u ctrlrtn ctrlrtn experiment ...    # A/Bs, routes, evals: all on-box
journalctl -u ctrlrtn -f                  # service logs
```

`/usr/local/bin/ctrlrtn` is a wrapper the installer adds so every CLI
invocation reads the service config, and therefore the same absolute `db_path`
as `serve`.

The console's experiment, shadow, and route actions write the same local SQLite
state as the CLI and take effect on the gateway's next snapshot refresh
(normally within about 10 seconds). Launch it with `--routing-config` and
`--routing-repo` to enable the `g` preview-and-activate workflow for a
configuration checkout outside the service's working directory. The service
user needs read access to that Git checkout; no network Git credentials are
required for validation or activation. The keys are listed in
[console.md](console.md).

- **Backups**: the database holds your **routes and experiments**, not just
  recordings. Losing it silently reverts every switch to pass-through. A
  rolling on-box backup:
  `sudo -u ctrlrtn sqlite3 /var/lib/ctrlrtn/router.db ".backup /var/lib/ctrlrtn/backup-$(date +%a).db"`
  (cron it; the content is sensitive, see above). SQLite + WAL requires a local
  filesystem: never put the live DB on a network mount.
- **Upgrades**: re-run `deploy/install.sh`. It rebuilds the venv from scratch
  (`uv venv --clear`) and waits up to 30s for `/healthz` before reporting
  success, so a re-run either upgrades the box or exits non-zero; it never
  leaves the checkout on the new sha with the old code still serving. Restart
  is quick; the proxy is stateless, in-flight LLM calls fail and clients retry.
- **Retention**: manual retention is the default. Preview an age-based payload
  prune with `ctrlrtn prune --older-than-days 30`; add `--apply` only after
  reviewing the count. To apply a reviewed age automatically on every gateway
  startup, set the positive `retention_days` configuration value. The gateway
  applies the same job-aware transaction before accepting traffic and fails
  startup if the policy cannot be applied. It does not compact a live
  database. Derived metrics remain available, and payloads frozen by queued or
  running jobs are reported and skipped.
- **Databases recorded before capture-time redaction existed**: run
  `ctrlrtn scrub-credentials` once. New traces never store credential headers.

Applying `prune` transactionally clears selected query strings, request and
response headers, and bodies. This is logical SQLite deletion, not guaranteed
forensic erasure or immediate file-size reduction. For physical database
reclamation, stop every gateway, worker, and mutating CLI using the database,
then run the same command with `--apply --compact`. Every writable ctrlrtn
process holds a shared local maintenance lock; compaction requires its
exclusive form before opening SQLite, checkpoints the WAL, and runs `VACUUM`.
It fails if another ctrlrtn writer is open or an external SQLite user keeps the
checkpoint busy. The lock is local-filesystem only, matching the database's
deployment contract. Exclusive compaction requires POSIX advisory locks;
ordinary routing remains available on platforms without them, but the compact
operation fails closed.

Filesystem snapshots and backups can still retain old bytes after compaction.
Expire those copies under the deployment's backup-retention policy when
media-level erasure is required. That policy is outside the router process and
must name every copy class: local rolling backups, off-box backups, volume and
filesystem snapshots, replicas, and operator exports. For each class, declare a
maximum age, deletion owner, verification method, and any legal hold.
`retention_days` applies only to payload fields in the live SQLite database; it
never expires or rewrites a backup.

Logical deletion plus `VACUUM` is not cryptographic erasure. Deployments that
require a key-destruction guarantee must encrypt the database, WAL, temporary
files, backups, and snapshots with deployment-owned keys and document how key
rotation and destruction cover every retained copy. ctrlrtn does not manage
those keys and does not report secure media erasure.
