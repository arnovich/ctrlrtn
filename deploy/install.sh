#!/usr/bin/env bash
# ctrlrtn bare-metal installer/upgrader (Debian/Ubuntu-ish, systemd).
#
#   sudo deploy/install.sh                 # install or upgrade to origin/main
#   sudo deploy/install.sh --ref v0.1.0    # pin a tag/branch/commit
#
# Idempotent: re-running upgrades the checkout and restarts the service;
# your /etc/ctrlrtn/config.yaml and the recorded database are never
# touched. Read this script before running it — it is short on purpose.
set -euo pipefail

REPO="${REPO:-https://github.com/arnovich/ctrlrtn.git}"
DIR="${DIR:-/opt/ctrlrtn}"
REF="main"
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 2; }

# 1. Service user (no shell, no login).
id ctrlrtn >/dev/null 2>&1 \
  || useradd --system --home-dir /var/lib/ctrlrtn --shell /usr/sbin/nologin ctrlrtn

# 2. uv (the only build tool needed).
if ! command -v uv >/dev/null 2>&1; then
  echo "installing uv (astral.sh) ..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

# 3. Checkout at $DIR, pinned to $REF.
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch --tags origin
else
  git clone "$REPO" "$DIR"
fi
git -C "$DIR" checkout --detach "origin/$REF" 2>/dev/null \
  || git -C "$DIR" checkout --detach "$REF"

# 4. Venv + install (with the TUI extra: the console over `ssh -t` is the
#    main way to watch a headless box).
#    --clear because this script is an UPGRADER too: uv refuses to reuse an
#    existing venv ("A virtual environment already exists"), so without it
#    every re-run died here — after step 3 had already moved HEAD to the new
#    sha, leaving a box that looked upgraded and ran the old code. A fresh
#    venv per upgrade also drops packages a dependency change removed.
uv venv --clear --python 3.12 "$DIR/.venv"
uv pip install --python "$DIR/.venv/bin/python" "$DIR[tui]"

# 4b. A wrapper so `ctrlrtn` on this box always reads the service
#     config (absolute db_path) instead of a cwd-relative default.
cat > /usr/local/bin/ctrlrtn <<WRAP
#!/bin/sh
export CTRLRTN_CONFIG="\${CTRLRTN_CONFIG:-/etc/ctrlrtn/config.yaml}"
exec "$DIR/.venv/bin/ctrlrtn" "\$@"
WRAP
chmod 0755 /usr/local/bin/ctrlrtn

# 5. Config — created once, never overwritten. Absolute db_path on purpose:
#    serve and the CLI must agree on the database regardless of cwd.
mkdir -p /etc/ctrlrtn
if [ ! -f /etc/ctrlrtn/config.yaml ]; then
  cat > /etc/ctrlrtn/config.yaml <<'YAML'
# ctrlrtn config — see docs/configure.md for every key.
db_path: /var/lib/ctrlrtn/router.db
host: 127.0.0.1        # loopback: only this box can reach the router.
port: 4000             # NEVER bind publicly: the /ctrlrtn/ control plane is
                       # unauthenticated by design (docs/deploy.md).
log_requests: true
YAML
  chmod 0640 /etc/ctrlrtn/config.yaml
  chown root:ctrlrtn /etc/ctrlrtn/config.yaml
fi

# 6. Unit, enable, start (StateDirectory creates /var/lib/ctrlrtn).
cp "$DIR/deploy/ctrlrtn.service" /etc/systemd/system/ctrlrtn.service
systemctl daemon-reload
systemctl enable --now ctrlrtn
systemctl restart ctrlrtn

# 7. Prove it serves.
# Poll rather than probe once: a restart on a loaded box (or one opening a
# large router.db) can take several seconds, and reporting a healthy router as
# a failed install strands the caller in the same "looks upgraded, isn't" state
# --clear above just fixed.
up=""
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:4000/healthz >/dev/null; then up=1; break; fi
  sleep 1
done
if [ -n "$up" ]; then
  echo "ctrlrtn is up: http://127.0.0.1:4000"
  echo "point your app at it:  ANTHROPIC_BASE_URL=http://127.0.0.1:4000"
  echo "inspect on this box:   sudo -u ctrlrtn ctrlrtn usecases"
  echo "watch it live:         sudo -u ctrlrtn ctrlrtn console"
else
  echo "healthz failed — check: journalctl -u ctrlrtn -n 50" >&2
  exit 1
fi
