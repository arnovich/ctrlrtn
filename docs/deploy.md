# Deploying ctrlrtn

The router is a single lightweight process (uvicorn + SQLite). Deploying it is
mostly about *where it listens* — its security model is "only things you trust
can reach it".

## The security model, first

- **The `/ctrlrtn/` control plane is unauthenticated by design** (localhost
  assumption). Anyone who can reach the router can `POST /ctrlrtn/outcome` and
  poison your experiment statistics. A Host-header check blocks DNS-rebinding
  (browser pages reaching a loopback-bound port via a rebound domain); if
  your apps address the router by an internal DNS name, add it to
  `control_hosts`.
- **Anyone who can reach the proxy can send traffic through it** (with their
  own key — the router holds none), polluting your recorded corpus.
- **The database contains your full prompts and responses.** Credentials are
  redacted at capture time and never stored — a named list covering the
  standard headers (`authorization`, `x-api-key`, `api-key`, cookies) and
  common `?key=`-style query params; an upstream using an exotic custom
  credential carrier is out of scope. The recorded *content* remains as
  sensitive as your application's data — treat backups accordingly.
  (Databases from before July 2026: run `ctrlrtn scrub-credentials`
  once.)
- Experiments and routes are controlled via the **CLI on the box** (they
  write the SQLite directly) — there is no remote admin surface to secure.
- A shadow experiment discloses each selected live prompt to its candidate
  provider. Confirm data residency and provider policy before starting one.
  Candidate responses are retained even though users never see them, and calls
  still consume provider quota, bandwidth, and billed tokens.

Consequence: **never expose the router publicly.** Loopback by default;
a private network + firewall when it must cross hosts.

## Topology A — same box as your app (recommended)

Run the router next to the application it fronts, bound to `127.0.0.1`
(the default). This is the strongest isolation available — the router has no
network presence — and the app's API key never leaves the box.

```bash
# on the server, as root — read it first, it is short:
git clone https://github.com/arnovich/ctrlrtn /tmp/gr \
  && sudo /tmp/gr/deploy/install.sh
```

The installer is **idempotent**: it creates a `ctrlrtn` system user,
installs into `/opt/ctrlrtn` (pinned to `--ref`, default `main`), writes
`/etc/ctrlrtn/config.yaml` once (never overwritten), installs a hardened
systemd unit with state in `/var/lib/ctrlrtn`, and verifies `/healthz`.
**Re-running it is the upgrade path.**

Then point the app at it (its systemd env file / `.env`):

```
ANTHROPIC_BASE_URL=http://127.0.0.1:4000     # or OPENAI_BASE_URL
```

## Topology B — a dedicated router box (multi-app)

When several apps share one router, give it its own host:

1. Put router + app hosts on a **private network** (e.g. a cloud-provider private
   network); bind the router to its private IP (`host:` in the config).
2. Firewall the router port to the app hosts' private IPs only (cloud
   firewall or nftables). The public interface serves nothing.
3. Apps use `ANTHROPIC_BASE_URL=http://<router-private-ip>:4000`. Keys
   transit the private network — acceptable on a provider-isolated LAN;
   put a TLS-terminating proxy in front if your threat model needs more.
4. **Namespace your route tags per app** (`x-ctrlrtn-route: newsroom:composer`,
   `hugin:editor`): tag keys are global to a router, and two apps both
   sending `tag:editor` would silently merge their statistics.

## Docker

```bash
docker compose up -d        # see compose.yaml
```

Keep the port published on `127.0.0.1:4000:4000` — Docker's port publishing
**bypasses ufw-style host firewalls**; a bare `4000:4000` silently exposes
the router to the internet.

## Operating it

```bash
sudo -u ctrlrtn ctrlrtn usecases          # spend per use-case
sudo -u ctrlrtn ctrlrtn console           # monitor + confirmed controls over ssh -t
sudo -u ctrlrtn ctrlrtn experiment ...    # A/Bs, routes, evals — all on-box
journalctl -u ctrlrtn -f                       # service logs
```

In the console, `e` starts a reviewed live split; `s` stops the selected split;
`a` adopts its candidate; and `p`/`c` set or clear a selected use-case's route.
Use `h` to start a reviewed online shadow and `z` to stop the selected shadow;
its detail pane shows the latest served/candidate pair and any attrition.
These controls write the same local SQLite state as the CLI and take effect on
the gateway's next snapshot refresh (normally within about 10 seconds).
Launch with `--routing-config` and `--routing-repo` to enable the console's `g`
preview/activation workflow for a configuration checkout outside the service's
working directory. The service user needs read access to that Git checkout; no
network Git credentials are required for validation or activation.

To monitor an application-owned execution canary ledger, pass it explicitly:

```bash
sudo -u ctrlrtn ctrlrtn console \
  --canary-state /var/lib/ctrlrtn/execution-canary.db
```

The console opens that ledger read-only for monitoring. Pressing `k` on an
active campaign requires confirmation and opens a short-lived writer solely to
fence and roll back that campaign. File permissions therefore govern rollback
access; the console cannot activate execution.

Signing keys themselves belong in the application's secret manager or service
environment, not this database. Deploy a new key to all executor processes
before running `execution-plan canary-key-rotate`; keep both secrets available
through the configured overlap. Use `canary-key-list` to verify the audit chain.
Emergency `canary-key-revoke` (or console `v`) rejects the signer immediately
and rolls affected active campaigns back on their next trust check; the console
also performs the campaign rollback in the same confirmed operation.

(`/usr/local/bin/ctrlrtn` is a wrapper the installer adds so every CLI
invocation reads the service config — same absolute `db_path` as `serve`.)

- **Backups**: the database holds your **routes and experiments**, not just
  recordings — losing it silently reverts every switch to pass-through. A
  rolling on-box backup:
  `sudo -u ctrlrtn sqlite3 /var/lib/ctrlrtn/router.db ".backup /var/lib/ctrlrtn/backup-$(date +%a).db"`
  (cron it; content is sensitive — see above). SQLite + WAL requires a local
  filesystem: never put the live DB on a network mount.
- **Upgrades**: re-run `install.sh`. It rebuilds the venv from scratch
  (`uv venv --clear`) and waits up to 30s for `/healthz` before reporting
  success, so a re-run either upgrades the box or exits non-zero — it never
  leaves the checkout on the new sha with the old code still serving. Restart
  is quick; the proxy is stateless, in-flight LLM calls fail and clients retry.
- **Retention**: manual retention is the default. Preview an age-based payload
  prune with `ctrlrtn prune --older-than-days 30`; add `--apply` only after
  reviewing the count. To apply a reviewed age automatically on every gateway
  startup, set the positive `retention_days` configuration value. The gateway
  applies the same job-aware transaction before accepting traffic and fails
  startup if the policy cannot be applied. It does not compact a live database.
  Derived metrics remain available, and payloads frozen by queued/running jobs
  are reported and skipped.
- **Databases from before July 2026**: run `ctrlrtn scrub-credentials`
  once (newer versions never store credential headers).

Applying `prune` transactionally clears selected query strings,
request/response headers, and bodies. This is logical SQLite deletion, not
guaranteed forensic erasure or immediate file-size reduction. For physical
database reclamation, stop every gateway, worker, and mutating CLI using the
database, then run the same command with `--apply --compact`. Every writable
ctrlrtn process holds a shared local maintenance lock; compaction requires its
exclusive form before opening SQLite, checkpoints the WAL, and runs `VACUUM`.
It fails if another ctrlrtn writer is open or an external SQLite user keeps the
checkpoint busy. The lock is local-filesystem only, matching the database's
deployment contract. Exclusive compaction requires POSIX advisory locks;
ordinary routing remains available on platforms without them, but the compact
operation fails closed.

Filesystem snapshots and backups can still retain old bytes after compaction.
Expire those copies under the deployment's backup-retention policy when
media-level erasure is required.

That policy is outside the router process and must name every copy class: local
rolling backups, off-box backups, volume/filesystem snapshots, replicas, and
operator exports. For each class, declare a maximum age, deletion owner,
verification method, and any legal hold. `retention_days` applies only to payload
fields in the live SQLite database; it never expires or rewrites a backup.

Logical deletion plus `VACUUM` is not cryptographic erasure. Deployments that
require a key-destruction guarantee must encrypt the database, WAL, temporary
files, backups, and snapshots with deployment-owned keys and document how key
rotation and destruction cover every retained copy. ctrlrtn does not manage those
keys and does not report secure media erasure.

# Remote execution endpoint

The optional remote execution component is an application-owned ASGI app built
with `create_remote_executor_app`; it is not a route on the ctrlrtn gateway and is
not enabled by the standard CLI. The embedding application must explicitly
bootstrap a dedicated nonce database, construct its bounded canary runner, and
inject live public-key/grant sources plus pure operation and baseline callbacks.

Terminate TLS at the ASGI server or at a trusted proxy configured to set the
ASGI scope scheme to `https`. The endpoint ignores forwarding headers, requires
the configured external authority exactly, and refuses an HTTP scope. Keep the
nonce database on durable local storage shared by every local executor process.
Do not split requests across hosts with independent SQLite files; multi-host
deployment requires a shared transactional admission backend.

Set `minimum_retention_seconds` to cover the longer of campaign audit retention
and result-idempotency retention. Grant expiry is included automatically. Store
bootstrap is explicit; normal startup must open the existing database without
`create=True`, so a lost volume stops admission rather than resetting replay
history. Private signing keys and provider/tool credentials remain in the
application's secret manager and runtime closures, never in requests or SQLite.

For canary operator migration, register only an Ed25519 public PEM with
`execution-plan canary-operator-key-register`; distribute the corresponding
public-key record to verifier configuration before starting a newly signed
campaign. Existing HMAC campaigns require explicit `allow_legacy_hmac=True`
while draining. Do not overwrite or re-label their artifacts. After they stop,
remove the shared secret bytes and revoke the legacy key ID. Rotate asymmetric
keys with `canary-operator-key-rotate`; the ledger refuses principal changes and
asymmetric-to-legacy downgrades.
