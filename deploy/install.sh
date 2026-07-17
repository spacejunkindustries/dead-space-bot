#!/usr/bin/env bash
# AURA droplet install — Ubuntu 24.04 LTS (GDD §17).
#
# Idempotent: safe to re-run for upgrades. Run as root from a checkout:
#   sudo deploy/install.sh [path/to/aura-ears]
#
# The aura-ears binary is NOT built here. The droplet is 2 vCPU and will
# thrash compiling Songbird with LTO — CI builds the release binary and
# uploads it as the `aura-ears` workflow artifact (.github/workflows/ci.yml).
# Download that artifact and pass its path as $1, or place it at
# deploy/aura-ears before running. If neither exists, the install completes
# and the checklist tells you what is missing.
#
# This script downloads nothing secret and writes no secrets. The Discord
# token is provided by YOU at /etc/aura/token (mode 0600) and reaches the
# processes only via systemd LoadCredential= (GDD §18/§22).

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "error: must run as root (sudo deploy/install.sh)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
EARS_BINARY="${1:-${SCRIPT_DIR}/aura-ears}"

echo "==> AURA install from ${REPO_ROOT}"

# ---------------------------------------------------------------- apt deps
# GDD §17.3. Deliberately absent (CLAUDE.md hard constraints):
#   - NO ffmpeg   — Songbird ≥0.4 uses Symphonia in-process (constraint 3)
#   - NO PyNaCl / discord.py[voice] — Python voice must stay impossible
#     to wire up by accident (constraint 2)
# piper is not packaged by Ubuntu; install it separately to
# /usr/local/bin/piper (see the checklist below).
echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    build-essential autoconf automake libtool m4 cmake pkg-config \
    libopus-dev \
    python3.12 python3.12-venv \
    sqlite3 rsync

# ---------------------------------------------------------------- aura user
if ! id -u aura >/dev/null 2>&1; then
    echo "==> Creating system user 'aura' (nologin)"
    useradd --system --shell /usr/sbin/nologin \
        --home-dir /var/lib/aura --no-create-home aura
else
    echo "==> User 'aura' already exists"
fi

# ---------------------------------------------------------------- host layout
# GDD §17.2
echo "==> Creating directories"
install -d -m 0755 /opt/aura /opt/aura/bin /opt/aura/brain
install -d -m 0755 /opt/aura/models/wake /opt/aura/models/whisper /opt/aura/models/piper
install -d -m 0750 -o root -g aura /etc/aura
install -d -m 0750 -o aura -g aura /var/lib/aura

# ---------------------------------------------------------------- ears binary
# Stage to a temp file then atomically rename into place. rename(2) succeeds
# even when the destination is a running executable ("Text file busy" on an
# in-place write): the running process keeps the old inode and the next start
# picks up the new binary. This makes re-running install.sh over a live
# aura-ears.service safe — no need to stop it first.
EARS_DEST=/opt/aura/bin/aura-ears
EARS_TMP="${EARS_DEST}.new"
if [[ -f "${EARS_BINARY}" ]]; then
    echo "==> Installing aura-ears binary from ${EARS_BINARY}"
    install -m 0755 "${EARS_BINARY}" "${EARS_TMP}"
    mv -f "${EARS_TMP}" "${EARS_DEST}"
elif git -C "${REPO_ROOT}" fetch --quiet --depth 1 origin ears-bin 2>/dev/null; then
    # CI publishes the release binary (built on every merge to main) to the
    # ears-bin branch, reachable with the same credentials as the clone.
    echo "==> Installing aura-ears binary from origin/ears-bin"
    git -C "${REPO_ROOT}" show FETCH_HEAD:aura-ears > "${EARS_TMP}"
    git -C "${REPO_ROOT}" show FETCH_HEAD:aura-ears.sha256 \
        | sed "s|aura-ears|${EARS_TMP##*/}|" \
        | (cd /opt/aura/bin && sha256sum --check --quiet -) \
        || { echo "ERROR: aura-ears checksum mismatch"; rm -f "${EARS_TMP}"; exit 1; }
    chmod 0755 "${EARS_TMP}"
    mv -f "${EARS_TMP}" "${EARS_DEST}"
else
    echo "==> aura-ears binary not found at ${EARS_BINARY} and no ears-bin branch — skipping"
    echo "    (download the 'aura-ears' CI artifact and re-run, or copy it"
    echo "     to /opt/aura/bin/aura-ears yourself)"
fi

# ---------------------------------------------------------------- brain tree
# GDD §17.2: /opt/aura/brain/ holds venv + package. The package is deployed
# as a source tree (editable install) because migrations/ and schema.sql are
# resolved relative to the aura package at runtime (aura/core/db.py).
echo "==> Deploying Brain source tree to /opt/aura/brain"
rsync -a --delete "${REPO_ROOT}/brain/aura/" /opt/aura/brain/aura/ \
    --exclude '__pycache__'
rsync -a --delete "${REPO_ROOT}/brain/migrations/" /opt/aura/brain/migrations/
install -m 0644 "${REPO_ROOT}/brain/schema.sql" /opt/aura/brain/schema.sql
install -m 0644 "${REPO_ROOT}/brain/pyproject.toml" /opt/aura/brain/pyproject.toml
install -m 0644 "${REPO_ROOT}/brain/requirements.txt" /opt/aura/brain/requirements.txt
install -d -m 0755 /opt/aura/brain/docs
install -m 0644 "${REPO_ROOT}/docs/GDD.md" /opt/aura/brain/docs/GDD.md

# ---------------------------------------------------------------- brain venv
echo "==> Building Brain venv at /opt/aura/brain/venv"
if [[ ! -x /opt/aura/brain/venv/bin/python ]]; then
    python3.12 -m venv /opt/aura/brain/venv
fi
/opt/aura/brain/venv/bin/pip install --quiet --upgrade pip
/opt/aura/brain/venv/bin/pip install --quiet -r /opt/aura/brain/requirements.txt
# openwakeword pins tflite-runtime on Linux, which has no wheels for
# Python >=3.12. AURA only uses its ONNX path (inference_framework="onnx"),
# so install it without deps; its real runtime deps are in requirements.txt.
/opt/aura/brain/venv/bin/pip install --quiet --no-deps "openwakeword>=0.6.0"
/opt/aura/brain/venv/bin/pip install --quiet -e /opt/aura/brain
# ProtectSystem=strict makes /opt read-only for the service: precompile
# bytecode now so the runtime never tries to write __pycache__.
/opt/aura/brain/venv/bin/python -m compileall -q /opt/aura/brain/aura

# ---------------------------------------------------------------- config
# Copy examples only if absent — never clobber a live, tuned config.
echo "==> Installing config examples (existing files untouched)"
declare -A CONFIGS=(
    ["${REPO_ROOT}/config/aura.yaml.example"]=/etc/aura/aura.yaml
    ["${REPO_ROOT}/config/routing.yaml.example"]=/etc/aura/routing.yaml
    ["${REPO_ROOT}/config/gazetteer.yaml.example"]=/etc/aura/gazetteer.yaml
    ["${REPO_ROOT}/ears/ears.yaml.example"]=/etc/aura/ears.yaml
)
for src in "${!CONFIGS[@]}"; do
    dst="${CONFIGS[${src}]}"
    if [[ -f "${dst}" ]]; then
        echo "    ${dst} exists — kept"
    else
        install -m 0640 -o root -g aura "${src}" "${dst}"
        echo "    ${dst} installed from $(basename "${src}")"
    fi
done

# ---------------------------------------------------------------- systemd
echo "==> Installing systemd units and tmpfiles"
install -m 0644 "${SCRIPT_DIR}/aura-brain.service" /etc/systemd/system/aura-brain.service
install -m 0644 "${SCRIPT_DIR}/aura-ears.service" /etc/systemd/system/aura-ears.service
install -m 0644 "${SCRIPT_DIR}/aura.tmpfiles.conf" /etc/tmpfiles.d/aura.conf
systemd-tmpfiles --create /etc/tmpfiles.d/aura.conf
systemctl daemon-reload
systemctl enable aura-brain.service aura-ears.service

# ---------------------------------------------------------------- checklist
cat <<'CHECKLIST'

==> Install complete. Before starting AURA, finish these by hand:

  1. Token (never in config, env, or repo — GDD §22):
       printf '%s' 'YOUR_BOT_TOKEN' > /etc/aura/token
       chmod 0600 /etc/aura/token && chown root:root /etc/aura/token

  2. Piper TTS binary at /usr/local/bin/piper
     (https://github.com/OHF-Voice/piper1-gpl — not in Ubuntu's archive).

  3. Model files (GDD §4 Assets):
       /opt/aura/models/wake/     openWakeWord ONNX chain (melspec, embedding,
                                  and the trained "aura command" model)
       /opt/aura/models/whisper/  Whisper small int8 CTranslate2 weights
       /opt/aura/models/piper/    Piper voice .onnx + .onnx.json

  4. Edit /etc/aura/aura.yaml, routing.yaml, gazetteer.yaml, ears.yaml —
     the installed copies are examples with placeholder IDs.

  5. Seed the gazetteer (scoped to your region, ~100-500 systems — do NOT
     load the full SDE) and run /gazetteer rebuild once the bot is up.

  6. If /opt/aura/bin/aura-ears is missing, fetch the 'aura-ears' artifact
     from the latest green CI run and install it there (mode 0755).

  7. Firewall: ufw deny incoming except SSH. AURA opens no listening ports.

  Then:
       systemctl start aura-brain aura-ears
       journalctl -u aura-brain -u aura-ears -f

CHECKLIST
