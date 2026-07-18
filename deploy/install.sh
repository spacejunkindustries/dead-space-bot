#!/usr/bin/env bash
# CORTANA droplet install — Ubuntu 24.04 LTS (GDD §17).
#
# Idempotent: safe to re-run for upgrades. Run as root from a checkout:
#   sudo deploy/install.sh [path/to/cortana-ears]
#
# The cortana-ears binary is NOT built here. The droplet is 2 vCPU and will
# thrash compiling Songbird with LTO — the RUST workflow
# (.github/workflows/rust.yml) builds the release binary on every merge to
# main touching ears/ and publishes it to the `ears-bin` branch, which this
# script fetches automatically with the clone's credentials. (The same
# workflow also uploads an `cortana-ears` artifact, but artifacts expire after
# 90 days — the ears-bin branch is the canonical source.) To override, pass
# a binary path as $1 or place it at deploy/cortana-ears before running.
#
# This script downloads nothing secret and writes no secrets. The Discord
# token is provided by YOU at /etc/cortana/token (mode 0600) and reaches the
# processes only via systemd LoadCredential= (GDD §18/§22).

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "error: must run as root (sudo deploy/install.sh)" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
EARS_BINARY="${1:-${SCRIPT_DIR}/cortana-ears}"

echo "==> CORTANA install from ${REPO_ROOT}"

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
        --home-dir /var/lib/cortana --no-create-home aura
else
    echo "==> User 'aura' already exists"
fi

# ---------------------------------------------------------------- migration
# One-time migration from the original "aura" layout. Everything the corp
# has accumulated — config, database (callsigns, incidents, optouts), token,
# API key, models — moves over; the old units are retired. The unix user
# stays `aura` deliberately (file ownership continuity; invisible to Discord).
if [[ -d /etc/aura && ! -d /etc/cortana ]]; then
    echo "==> Migrating aura layout -> cortana (one-time)"
    systemctl stop aura-brain.service aura-ears.service 2>/dev/null || true
    systemctl disable aura-brain.service aura-ears.service 2>/dev/null || true
    mv /etc/aura /etc/cortana
    [[ -f /etc/cortana/aura.yaml ]] && mv /etc/cortana/aura.yaml /etc/cortana/cortana.yaml
    [[ -d /var/lib/aura ]] && mv /var/lib/aura /var/lib/cortana
    [[ -f /var/lib/cortana/aura.db ]] && mv /var/lib/cortana/aura.db /var/lib/cortana/cortana.db
    if [[ -d /opt/aura ]]; then
        mv /opt/aura /opt/cortana
        # venv scripts carry absolute /opt/aura shebangs — rebuild fresh below.
        rm -rf /opt/cortana/brain/venv /opt/cortana/brain/aura
        rm -f /opt/cortana/bin/aura-ears
    fi
    # Rewrite every path inside the migrated configs (wake.model, tts.voice,
    # ipc.socket, database.path, token_file, api_key_file, ears.yaml).
    sed -i 's|/opt/aura|/opt/cortana|g; s|/etc/aura|/etc/cortana|g; s|/run/aura|/run/cortana|g; s|/var/lib/aura|/var/lib/cortana|g; s|aura\.db|cortana.db|g; s|aura\.sock|cortana.sock|g' \
        /etc/cortana/*.yaml
    rm -f /etc/systemd/system/aura-brain.service /etc/systemd/system/aura-ears.service \
          /etc/tmpfiles.d/aura.conf
    systemctl daemon-reload
    echo "    migrated: /etc/cortana, /var/lib/cortana, /opt/cortana; old units removed"
fi

# ---------------------------------------------------------------- host layout
# GDD §17.2
echo "==> Creating directories"
install -d -m 0755 /opt/cortana /opt/cortana/bin /opt/cortana/brain
install -d -m 0755 /opt/cortana/models/wake /opt/cortana/models/whisper /opt/cortana/models/piper
install -d -m 0750 -o root -g aura /etc/cortana
install -d -m 0750 -o aura -g aura /var/lib/cortana
# Pre-create the database with the right owner: the natural way to run the
# gazetteer seeder is plain `sudo python -m cortana.nlu.seed ...`, which would
# otherwise create cortana.db as root:root and crash cortana-brain (User=aura)
# with "attempt to write a readonly database" on its first migration.
if [[ ! -f /var/lib/cortana/cortana.db ]]; then
    install -m 0640 -o aura -g aura /dev/null /var/lib/cortana/cortana.db
fi

# ---------------------------------------------------------------- ears binary
# Stage to a temp file then atomically rename into place. rename(2) succeeds
# even when the destination is a running executable ("Text file busy" on an
# in-place write): the running process keeps the old inode and the next start
# picks up the new binary. This makes re-running install.sh over a live
# cortana-ears.service safe — no need to stop it first.
EARS_DEST=/opt/cortana/bin/cortana-ears
EARS_TMP="${EARS_DEST}.new"
if [[ -f "${EARS_BINARY}" ]]; then
    echo "==> Installing cortana-ears binary from ${EARS_BINARY}"
    install -m 0755 "${EARS_BINARY}" "${EARS_TMP}"
    mv -f "${EARS_TMP}" "${EARS_DEST}"
elif git -C "${REPO_ROOT}" fetch --quiet --depth 1 origin ears-bin 2>/dev/null \
        && git -C "${REPO_ROOT}" cat-file -e FETCH_HEAD:cortana-ears 2>/dev/null; then
    # CI publishes the release binary (built on every merge to main) to the
    # ears-bin branch, reachable with the same credentials as the clone.
    # The cat-file guard matters: running install.sh in the minute before CI
    # finishes publishing used to hard-abort the whole script mid-install
    # (set -e) — leaving migrations done but no service units installed.
    echo "==> Installing cortana-ears binary from origin/ears-bin"
    git -C "${REPO_ROOT}" show FETCH_HEAD:cortana-ears > "${EARS_TMP}"
    git -C "${REPO_ROOT}" show FETCH_HEAD:cortana-ears.sha256 \
        | sed "s|cortana-ears|${EARS_TMP##*/}|" \
        | (cd /opt/cortana/bin && sha256sum --check --quiet -) \
        || { echo "ERROR: cortana-ears checksum mismatch"; rm -f "${EARS_TMP}"; exit 1; }
    chmod 0755 "${EARS_TMP}"
    mv -f "${EARS_TMP}" "${EARS_DEST}"
else
    echo "==> cortana-ears binary not found at ${EARS_BINARY} and no ears-bin branch — skipping"
    echo "    (download the 'cortana-ears' CI artifact and re-run, or copy it"
    echo "     to /opt/cortana/bin/cortana-ears yourself)"
fi

# ---------------------------------------------------------------- brain tree
# GDD §17.2: /opt/cortana/brain/ holds venv + package. The package is deployed
# as a source tree (editable install) because migrations/ and schema.sql are
# resolved relative to the cortana package at runtime (cortana/core/db.py).
echo "==> Deploying Brain source tree to /opt/cortana/brain"
rsync -a --delete "${REPO_ROOT}/brain/cortana/" /opt/cortana/brain/cortana/ \
    --exclude '__pycache__'
rsync -a --delete "${REPO_ROOT}/brain/migrations/" /opt/cortana/brain/migrations/
install -m 0644 "${REPO_ROOT}/brain/schema.sql" /opt/cortana/brain/schema.sql
install -m 0644 "${REPO_ROOT}/brain/pyproject.toml" /opt/cortana/brain/pyproject.toml
install -m 0644 "${REPO_ROOT}/brain/requirements.txt" /opt/cortana/brain/requirements.txt
install -d -m 0755 /opt/cortana/brain/docs
install -m 0644 "${REPO_ROOT}/docs/GDD.md" /opt/cortana/brain/docs/GDD.md

# ---------------------------------------------------------------- brain venv
echo "==> Building Brain venv at /opt/cortana/brain/venv"
if [[ ! -x /opt/cortana/brain/venv/bin/python ]]; then
    python3.12 -m venv /opt/cortana/brain/venv
fi
/opt/cortana/brain/venv/bin/pip install --quiet --upgrade pip
/opt/cortana/brain/venv/bin/pip install --quiet -r /opt/cortana/brain/requirements.txt
# openwakeword pins tflite-runtime on Linux, which has no wheels for
# Python >=3.12. CORTANA only uses its ONNX path (inference_framework="onnx"),
# so install it without deps; its real runtime deps are in requirements.txt.
/opt/cortana/brain/venv/bin/pip install --quiet --no-deps "openwakeword>=0.6.0"
/opt/cortana/brain/venv/bin/pip install --quiet -e /opt/cortana/brain
# ProtectSystem=strict makes /opt read-only for the service: precompile
# bytecode now so the runtime never tries to write __pycache__.
/opt/cortana/brain/venv/bin/python -m compileall -q /opt/cortana/brain/cortana

# ---------------------------------------------------------------- models
# openWakeWord's melspectrogram/embedding ONNX pair lives in its package
# resources dir and is NOT shipped in the wheel — without this download the
# first speech frame kills the audio path at runtime (and ProtectSystem=
# strict makes a runtime self-download impossible: /opt is read-only to the
# service). Install-time is the only moment this can happen. Best-effort:
# a warning here beats a silent runtime failure later.
echo "==> Fetching openWakeWord feature models (melspec + embedding)"
/opt/cortana/brain/venv/bin/python - <<'PYEOF' || echo "WARNING: openWakeWord model download failed — voice will not start until it succeeds (re-run install.sh with network access)"
import openwakeword.utils
# No-arg call fetches the feature models plus the pretrained wake models
# (~50 MB total) — includes hey_jarvis, the interim wake phrase.
openwakeword.utils.download_models()
PYEOF

# Whisper weights: pre-fetch into the model dir the GDD documents so first
# start never blocks on (or crash-loops against) HuggingFace. Best-effort.
if [[ ! -d /opt/cortana/models/whisper/small ]]; then
    echo "==> Fetching faster-whisper 'small' weights to /opt/cortana/models/whisper/small"
    /opt/cortana/brain/venv/bin/python - <<'PYEOF' || echo "WARNING: whisper download failed — stt.model: small will fetch from HuggingFace at first start instead"
from faster_whisper import download_model
download_model("small", output_dir="/opt/cortana/models/whisper/small")
PYEOF
fi

# ---------------------------------------------------------------- config
# Copy examples only if absent — never clobber a live, tuned config.
echo "==> Installing config examples (existing files untouched)"
declare -A CONFIGS=(
    ["${REPO_ROOT}/config/cortana.yaml.example"]=/etc/cortana/cortana.yaml
    ["${REPO_ROOT}/config/routing.yaml.example"]=/etc/cortana/routing.yaml
    ["${REPO_ROOT}/config/gazetteer.yaml.example"]=/etc/cortana/gazetteer.yaml
    ["${REPO_ROOT}/ears/ears.yaml.example"]=/etc/cortana/ears.yaml
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

# ---------------------------------------------------------------- credentials
# cortana-brain.service loads /etc/cortana/anthropic unconditionally (LoadCredential=
# fails unit start if the source is missing), so guarantee it exists. Empty is
# fine: an empty credential reads as "no key" and the override channel stays
# off. Never overwrite a real key.
if [[ ! -f /etc/cortana/anthropic ]]; then
    install -m 0600 -o root -g root /dev/null /etc/cortana/anthropic
    echo "==> Created empty /etc/cortana/anthropic (chat override channel off)"
fi

# ---------------------------------------------------------------- systemd
echo "==> Installing systemd units and tmpfiles"
install -m 0644 "${SCRIPT_DIR}/cortana-brain.service" /etc/systemd/system/cortana-brain.service
install -m 0644 "${SCRIPT_DIR}/cortana-ears.service" /etc/systemd/system/cortana-ears.service
install -m 0644 "${SCRIPT_DIR}/cortana.tmpfiles.conf" /etc/tmpfiles.d/cortana.conf
systemd-tmpfiles --create /etc/tmpfiles.d/cortana.conf
systemctl daemon-reload
systemctl enable cortana-brain.service cortana-ears.service

# A live Brain keeps running OLD code after the rsync above — worse, its lazy
# imports (first voice "help", first STT load) would then read NEW module
# files mid-flight, raising ImportError hours later at 02:00. Restart it now
# if it is running; a stopped/fresh install is left alone (the checklist's
# `systemctl start` covers that). Ears is safe live (atomic rename, old
# inode) and restarts on its own schedule.
if systemctl is-active --quiet cortana-brain.service; then
    echo "==> Restarting cortana-brain (source tree was just replaced under it)"
    systemctl restart cortana-brain.service
fi

# ---------------------------------------------------------------- checklist
cat <<'CHECKLIST'

==> Install complete. Before starting CORTANA, finish these by hand:

  1. Token (never in config, env, or repo — GDD §22):
       printf '%s' 'YOUR_BOT_TOKEN' > /etc/cortana/token
       chmod 0600 /etc/cortana/token && chown root:root /etc/cortana/token

  2. Piper TTS binary at /usr/local/bin/piper
     (https://github.com/OHF-Voice/piper1-gpl — not in Ubuntu's archive).

  3. Model files (GDD §4 Assets):
       /opt/cortana/models/wake/     openWakeWord ONNX chain (melspec, embedding,
                                  and the trained "hey cortana" model)
       /opt/cortana/models/whisper/  Whisper small int8 CTranslate2 weights
       /opt/cortana/models/piper/    Piper voice .onnx + .onnx.json

  4. Edit /etc/cortana/cortana.yaml, routing.yaml, gazetteer.yaml, ears.yaml —
     the installed copies are examples with placeholder IDs.

  5. Seed the gazetteer (scoped to your region, ~100-500 systems — do NOT
     load the full SDE) and run /gazetteer rebuild once the bot is up.
     Run the seeder AS THE SERVICE USER so the database stays writable:
       sudo -u aura /opt/cortana/brain/venv/bin/python -m cortana.nlu.seed \
           --db /var/lib/cortana/cortana.db ...

  6. If /opt/cortana/bin/cortana-ears is missing, re-run this script once the
     ears-bin branch exists (rust.yml publishes it on every merge to main
     touching ears/), or install a binary there yourself (mode 0755).

  7. Firewall: ufw deny incoming except SSH. CORTANA opens no listening ports.

  Then:
       systemctl start cortana-brain cortana-ears
       journalctl -u cortana-brain -u cortana-ears -f

CHECKLIST
