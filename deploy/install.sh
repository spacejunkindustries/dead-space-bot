#!/usr/bin/env bash
# CORTANA droplet deploy — Ubuntu 24.04 LTS (GDD §17).
#
# Staged converge-and-verify. Run as root from a checkout:
#
#   sudo deploy/install.sh [path/to/cortana-ears]   # deploy / upgrade
#   sudo deploy/install.sh --rollback               # flip to the previous release
#   INSTALL_DRYRUN=1 deploy/install.sh              # print the plan, touch nothing
#
# Stages (each prints a banner; read the output top to bottom):
#
#   STAGE    Build a complete new release in /opt/cortana/releases/<id>/:
#            brain source tree, a fresh venv, the ears binary (sha256-verified
#            when fetched from CI). ALL network-fallible work happens here —
#            an abort anywhere in STAGE leaves the running system untouched.
#   GATE     Preflight the NEW release against the LIVE /etc/cortana:
#            the token file must exist before anything is enabled, and the
#            release's own doctor (python -m cortana.doctor) runs offline as
#            the service user via setpriv.
#   FLIP     Atomically swap the /opt/cortana/current symlink.
#   RESTART  Brain always; Ears only when its binary hash changed (hash is
#            recorded per release), so a protocol-desynced brain/ears pair
#            cannot exist after a deploy.
#   VERIFY   PASS/FAIL table: doctor, is-active after settle, ears<->brain
#            IPC handshake in the journal, DB migration version. On FAIL:
#            journal tail, automatic rollback to the previous release,
#            nonzero exit.
#
# Idempotent: re-running from the same commit that is already live is a
# fast-path no-op ending in VERIFY. The last 3 releases are kept so
# --rollback always has somewhere to go.
#
# The cortana-ears binary is NOT built here. The droplet is 2 vCPU and will
# thrash compiling Songbird with LTO — CI (.github/workflows/rust.yml) builds
# the release binary and publishes it (binary + sha256) to the `ears-bin`
# branch, which this script fetches with the clone's credentials. To
# override, pass a binary path as $1 or place it at deploy/cortana-ears.
#
# This script downloads nothing secret and writes no secrets. The Discord
# token is provided by YOU at /etc/cortana/token (mode 0600) and reaches the
# processes only via systemd LoadCredential= (GDD §18/§22, constraint 12).
#
# Deliberately absent (CLAUDE.md hard constraints):
#   - NO ffmpeg   — Songbird ≥0.4 uses Symphonia in-process (constraint 3)
#   - NO PyNaCl / discord.py[voice] — Python voice must stay impossible to
#     wire up by accident (constraint 2)

set -euo pipefail
shopt -s nullglob

# ---------------------------------------------------------------- constants
OPT=/opt/cortana
RELEASES="${OPT}/releases"
CURRENT="${OPT}/current"
ETC=/etc/cortana
VARLIB=/var/lib/cortana
SETTLE_S=10        # seconds to let systemd + the brain settle before VERIFY
KEEP_RELEASES=3    # releases kept on disk (incl. current) for rollback
NEED_FREE_GB=3     # venv + models headroom for one new release

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DRYRUN="${INSTALL_DRYRUN:-0}"

# ---------------------------------------------------------------- helpers
say()  { echo "==> $*"; }
note() { echo "    $*"; }
warn() { echo "    WARNING: $*" >&2; }
die()  { echo "" >&2; echo "FAIL: $*" >&2; exit 1; }

# Every command that mutates the system goes through run(): under
# INSTALL_DRYRUN=1 it is printed instead of executed.
run() {
    if [[ "${DRYRUN}" == 1 ]]; then
        note "dry-run: $*"
    else
        "$@"
    fi
}

# systemctl calls get their own wrapper so a dry run (or a box without
# systemd, e.g. CI) never touches the service manager.
sysctl_run() {
    if [[ "${DRYRUN}" == 1 ]]; then
        note "dry-run: systemctl $*"
    else
        systemctl "$@"
    fi
}

# Atomic symlink swap: build the new link aside, rename(2) over the old one.
flip_current() {
    ln -sfn "$1" "${OPT}/.current.tmp"
    mv -T "${OPT}/.current.tmp" "${CURRENT}"
}

# Path of the live release dir ("" when none).
live_release() { readlink -f "${CURRENT}" 2>/dev/null || true; }

# Recorded ears binary hash of a release dir ("" when none).
ears_hash_of() { awk '{print $1}' "$1/ears.sha256" 2>/dev/null || true; }

install_if_changed() {  # src dst mode — sets UNITS_CHANGED=1 on change
    local src="$1" dst="$2" mode="$3"
    if [[ -f "${dst}" ]] && cmp -s "${src}" "${dst}"; then
        note "${dst} unchanged"
    else
        run install -m "${mode}" "${src}" "${dst}"
        note "${dst} installed"
        UNITS_CHANGED=1
    fi
}

show_journal_tail() {
    [[ "${DRYRUN}" == 1 ]] && return 0
    echo "---- journalctl -u cortana-brain -n 30 ----"
    journalctl -u cortana-brain.service -n 30 --no-pager 2>/dev/null || true
    echo "---- journalctl -u cortana-ears -n 15 ----"
    journalctl -u cortana-ears.service -n 15 --no-pager 2>/dev/null || true
}

# ---------------------------------------------------------------- verify
# PASS/FAIL table for a release that is (supposed to be) live.
# Returns nonzero if anything load-bearing failed.
verify_release() {
    local rel="$1" failed=0 rows=() r

    say "VERIFY"
    if [[ "${DRYRUN}" == 1 ]]; then
        note "dry-run: would settle ${SETTLE_S}s, then check: doctor, brain/ears"
        note "dry-run: is-active, ipc_client_connected handshake in the brain"
        note "dry-run: journal, and DB user_version vs shipped migrations"
        return 0
    fi
    note "settling ${SETTLE_S}s"
    sleep "${SETTLE_S}"

    # 1. doctor (result carried over from GATE)
    rows+=("doctor preflight|${DOCTOR_RESULT}")

    # 2/3. units active
    if systemctl is-active --quiet cortana-brain.service; then
        rows+=("brain is-active|PASS")
    else
        rows+=("brain is-active|FAIL")
        failed=1
    fi
    local ears_expected=1
    [[ -x "${rel}/bin/cortana-ears" ]] || ears_expected=0
    if (( ears_expected )); then
        if systemctl is-active --quiet cortana-ears.service; then
            rows+=("ears is-active|PASS")
        else
            rows+=("ears is-active|FAIL")
            failed=1
        fi
    else
        rows+=("ears is-active|SKIP (no binary in release)")
    fi

    # 4. ears<->brain socket handshake: the brain logs ipc_client_connected
    #    when ears attaches. Only counted since the brain's current start.
    if (( ears_expected )) && systemctl is-active --quiet cortana-brain.service; then
        local since ok=0 _try
        since="$(systemctl show -p ActiveEnterTimestamp --value cortana-brain.service)"
        for _try in 1 2 3 4 5 6; do   # ears reconnects with backoff — allow ~30s
            if journalctl -u cortana-brain.service --since "${since}" --no-pager 2>/dev/null \
                    | grep -q ipc_client_connected; then
                ok=1
                break
            fi
            sleep 5
        done
        if (( ok )); then
            rows+=("ears<->brain handshake|PASS")
        else
            rows+=("ears<->brain handshake|FAIL")
            failed=1
        fi
    else
        rows+=("ears<->brain handshake|SKIP")
    fi

    # 5. DB migration version vs the migrations shipped in this release.
    local migs=( "${rel}/brain/migrations/"[0-9]*.sql )
    if (( ${#migs[@]} )) && [[ -s "${VARLIB}/cortana.db" ]] && command -v sqlite3 >/dev/null 2>&1; then
        local newest expected got
        newest="$(basename "${migs[-1]}")"
        expected=$((10#${newest%%_*}))
        got="$(sqlite3 "${VARLIB}/cortana.db" 'PRAGMA user_version;' 2>/dev/null || echo '?')"
        if [[ "${got}" == "${expected}" ]]; then
            rows+=("migrations at ${expected}|PASS")
        else
            rows+=("migrations (want ${expected}, db has ${got})|FAIL")
            failed=1
        fi
    else
        rows+=("migration version|SKIP (fresh/empty db)")
    fi

    echo
    for r in "${rows[@]}"; do
        printf '    %-44s %s\n' "${r%%|*}" "${r#*|}"
    done
    echo
    return "${failed}"
}

# ---------------------------------------------------------------- rollback
do_rollback() {
    local cur prev d
    cur="$(live_release)"
    [[ -n "${cur}" ]] || die "--rollback: ${CURRENT} does not exist — nothing is deployed"
    prev=""
    for d in "${RELEASES}"/*/; do
        d="${d%/}"
        [[ -f "${d}/.complete" ]] || continue
        [[ "$(readlink -f "${d}")" == "${cur}" ]] && continue
        prev="${d}"   # ascending name sort == date order; ends on newest other
    done
    [[ -n "${prev}" ]] || die "--rollback: no other complete release under ${RELEASES}"

    say "ROLLBACK: $(basename "${cur}") -> $(basename "${prev}")"
    local old_hash new_hash
    old_hash="$(ears_hash_of "${cur}")"
    new_hash="$(ears_hash_of "${prev}")"
    run flip_current "${prev}"
    sysctl_run restart cortana-brain.service
    if [[ -n "${new_hash}" && "${new_hash}" != "${old_hash}" ]]; then
        say "ears binary differs in target release — restarting cortana-ears"
        sysctl_run restart cortana-ears.service
    fi
    DOCTOR_RESULT="SKIP (rollback)"
    if verify_release "${prev}"; then
        say "rollback verified — now running $(basename "${prev}")"
    else
        show_journal_tail
        die "rollback to $(basename "${prev}") did not verify — manual intervention needed"
    fi
}

# ---------------------------------------------------------------- staging
record_ears_hash() {
    ( cd "${REL}/bin" && sha256sum cortana-ears ) > "${REL}/ears.sha256"
}

fetch_ears_from_branch() {
    # FETCH_HEAD already points at origin/ears-bin (fetched by the caller).
    local dest="$1" tmp="$1.new"
    git -C "${REPO_ROOT}" show FETCH_HEAD:cortana-ears > "${tmp}"
    git -C "${REPO_ROOT}" show FETCH_HEAD:cortana-ears.sha256 > "${REL}/ears.sha256"
    if ! ( cd "${REL}/bin" \
            && sed 's| cortana-ears$| cortana-ears.new|' "${REL}/ears.sha256" \
            | sha256sum --check --quiet - ); then
        rm -f "${tmp}" "${REL}/ears.sha256"
        die "cortana-ears checksum mismatch against origin/ears-bin — refusing the binary"
    fi
    chmod 0755 "${tmp}"
    mv -f "${tmp}" "${dest}"
}

# Best source for a carried-forward binary when nothing new is available.
existing_ears_binary() {
    local cur
    cur="$(live_release)"
    if [[ -n "${cur}" && -x "${cur}/bin/cortana-ears" ]]; then
        echo "${cur}/bin/cortana-ears"
        return 0
    fi
    # pre-release-layout hosts kept the binary at a fixed path
    if [[ -x "${OPT}/bin/cortana-ears" ]]; then
        echo "${OPT}/bin/cortana-ears"
        return 0
    fi
    return 1
}

stage_ears() {
    # Preference order:
    #   1. explicit binary ($1 to this script, or deploy/cortana-ears)
    #   2. origin/ears-bin — CI-published, sha256-verified
    #   3. carry the live binary forward (hash unchanged -> no ears restart)
    # If none exists we warn and continue: ears will not start until a binary
    # arrives, but the brain deploy must not be held hostage. (This guard is
    # what used to strand the droplet mid-install when CI had not published
    # the branch yet — absence degrades, never aborts.)
    local dest="${REL}/bin/cortana-ears" carried=""
    if [[ -f "${EARS_BINARY}" ]]; then
        note "ears binary: ${EARS_BINARY}"
        run install -m 0755 "${EARS_BINARY}" "${dest}"
        run record_ears_hash
    elif [[ "${DRYRUN}" != 1 ]] \
            && git -C "${REPO_ROOT}" fetch --quiet --depth 1 origin ears-bin 2>/dev/null \
            && git -C "${REPO_ROOT}" cat-file -e FETCH_HEAD:cortana-ears 2>/dev/null; then
        note "ears binary: origin/ears-bin (sha256-verified)"
        fetch_ears_from_branch "${dest}"
    elif carried="$(existing_ears_binary)"; then
        note "ears binary: carried forward from ${carried}"
        run install -m 0755 "${carried}" "${dest}"
        run record_ears_hash
    else
        warn "no cortana-ears binary available (no local file, no ears-bin branch, nothing live to carry forward)"
        warn "staging without it — cortana-ears.service cannot start until you re-run install.sh once CI has published, or drop a binary at deploy/cortana-ears"
    fi
}

prefetch_wake_models() {
    # openWakeWord's melspectrogram/embedding ONNX pair lives in the package
    # resources dir inside THIS release's venv and is not shipped in the
    # wheel. Without it the first speech frame kills the audio path, and
    # ProtectSystem=strict makes a runtime self-download impossible.
    "${REL}/venv/bin/python" - <<'PYEOF'
import openwakeword.utils
# No-arg call fetches the feature models plus the pretrained wake models
# (~50 MB) — includes hey_jarvis, the interim wake phrase.
openwakeword.utils.download_models()
PYEOF
}

prefetch_whisper_weights() {
    "${REL}/venv/bin/python" - <<'PYEOF'
from faster_whisper import download_model
download_model("small", output_dir="/opt/cortana/models/whisper/small")
PYEOF
}

stage_release() {
    say "STAGE: building release ${RELEASE_ID}"
    REL="${RELEASES}/${RELEASE_ID}"
    run install -d -m 0755 "${REL}" "${REL}/bin" "${REL}/brain"

    # --- brain source tree ---------------------------------------------
    note "brain source tree"
    run rsync -a --delete --exclude '__pycache__' \
        "${REPO_ROOT}/brain/cortana/" "${REL}/brain/cortana/"
    run rsync -a --delete "${REPO_ROOT}/brain/migrations/" "${REL}/brain/migrations/"
    run install -m 0644 "${REPO_ROOT}/brain/schema.sql" "${REL}/brain/schema.sql"
    run install -m 0644 "${REPO_ROOT}/brain/pyproject.toml" "${REL}/brain/pyproject.toml"
    run install -m 0644 "${REPO_ROOT}/brain/requirements.txt" "${REL}/brain/requirements.txt"
    run install -d -m 0755 "${REL}/brain/docs"
    run install -m 0644 "${REPO_ROOT}/docs/GDD.md" "${REL}/brain/docs/GDD.md"

    # --- ears binary ---------------------------------------------------
    stage_ears

    # --- fresh venv ----------------------------------------------------
    # Always fresh: a release directory is immutable once complete, so pip
    # never mutates a live environment and a failed install never leaves a
    # half-upgraded venv behind anything the units point at.
    note "fresh venv (this is the slow part)"
    run python3.12 -m venv "${REL}/venv"
    run "${REL}/venv/bin/pip" install --quiet --upgrade pip
    run "${REL}/venv/bin/pip" install --quiet -r "${REL}/brain/requirements.txt"
    # openwakeword pins tflite-runtime on Linux, which has no wheels for
    # Python >=3.12. CORTANA only uses its ONNX path, so install it without
    # deps; its real runtime deps are in requirements.txt.
    run "${REL}/venv/bin/pip" install --quiet --no-deps "openwakeword>=0.6.0"
    run "${REL}/venv/bin/pip" install --quiet -e "${REL}/brain"
    # ProtectSystem=strict makes /opt read-only for the service: precompile
    # bytecode now so the runtime never tries to write __pycache__.
    run "${REL}/venv/bin/python" -m compileall -q "${REL}/brain/cortana"

    # --- model prefetch (best-effort — a warning beats a runtime failure)
    if [[ "${DRYRUN}" == 1 ]]; then
        note "dry-run: would prefetch openWakeWord feature models into the release venv"
        note "dry-run: would prefetch faster-whisper 'small' weights if absent"
    else
        note "prefetching openWakeWord feature models (melspec + embedding)"
        prefetch_wake_models \
            || warn "openWakeWord model download failed — voice will not start until it succeeds (re-run install.sh with network access)"
        if [[ ! -d "${OPT}/models/whisper/small" ]]; then
            note "prefetching faster-whisper 'small' weights"
            prefetch_whisper_weights \
                || warn "whisper download failed — stt.model: small will fetch from HuggingFace at first start instead"
        fi
    fi

    run touch "${REL}/.complete"
    note "release staged at ${REL}"
}

prune_releases() {
    local cur all=( "${RELEASES}"/*/ ) d excess
    cur="$(live_release)"
    excess=$(( ${#all[@]} - KEEP_RELEASES ))
    (( excess > 0 )) || return 0
    for d in "${all[@]}"; do          # ascending name sort == oldest first
        (( excess > 0 )) || break
        d="${d%/}"
        [[ "$(readlink -f "${d}")" == "${cur}" ]] && continue
        say "pruning old release $(basename "${d}")"
        run rm -rf "${d}"
        excess=$(( excess - 1 ))
    done
}

# ============================================================== entrypoint
if [[ "${DRYRUN}" == 1 ]]; then
    say "DRY RUN — printing the plan; nothing will be touched"
elif [[ ${EUID} -ne 0 ]]; then
    die "must run as root (sudo deploy/install.sh) — or set INSTALL_DRYRUN=1 to preview"
fi

# ------------------------------------------------------- unmigrated hosts
# The one-time aura->cortana migration used to live here. It could not be
# re-run after a mid-body failure (its guard flipped on the first mv), so it
# is gone: hosts that never migrated get the manual steps instead.
if [[ -d /etc/aura || -d /var/lib/aura || -d /opt/aura ]]; then
    cat >&2 <<'AURAEOF'

FAIL: this host still has an old aura-era layout (/etc/aura, /var/lib/aura
or /opt/aura exist). The automated migration has been removed — finish the
rename by hand, then re-run install.sh:

  systemctl stop aura-brain.service aura-ears.service      2>/dev/null || true
  systemctl disable aura-brain.service aura-ears.service   2>/dev/null || true
  [ -d /etc/aura ]     && mv /etc/aura /etc/cortana
  [ -f /etc/cortana/aura.yaml ] && mv /etc/cortana/aura.yaml /etc/cortana/cortana.yaml
  [ -d /var/lib/aura ] && mv /var/lib/aura /var/lib/cortana
  [ -f /var/lib/cortana/aura.db ] && mv /var/lib/cortana/aura.db /var/lib/cortana/cortana.db
  rm -rf /opt/aura        # code + venv are rebuilt from scratch by install.sh
  sed -i 's|/opt/aura|/opt/cortana|g; s|/etc/aura|/etc/cortana|g;
          s|/run/aura|/run/cortana|g; s|/var/lib/aura|/var/lib/cortana|g;
          s|aura\.db|cortana.db|g; s|aura\.sock|cortana.sock|g' /etc/cortana/*.yaml
  rm -f /etc/systemd/system/aura-brain.service \
        /etc/systemd/system/aura-ears.service /etc/tmpfiles.d/aura.conf
  systemctl daemon-reload

AURAEOF
    exit 1
fi

# ------------------------------------------------------------- --rollback
if [[ "${1:-}" == "--rollback" ]]; then
    do_rollback
    exit 0
fi
EARS_BINARY="${1:-${SCRIPT_DIR}/cortana-ears}"

say "CORTANA deploy from ${REPO_ROOT}"

# ---------------------------------------------------------- disk preflight
avail_kb="$(df --output=avail -k "${OPT}" 2>/dev/null | tail -1 | tr -d ' ' || true)"
[[ -n "${avail_kb}" ]] || avail_kb="$(df --output=avail -k / | tail -1 | tr -d ' ')"
if (( avail_kb < NEED_FREE_GB * 1024 * 1024 )); then
    if [[ "${DRYRUN}" == 1 ]]; then
        warn "less than ${NEED_FREE_GB} GB free for ${OPT} — a real run would abort here"
    else
        die "less than ${NEED_FREE_GB} GB free for ${OPT} ($(( avail_kb / 1024 )) MB available) — free disk space first (old releases live in ${RELEASES})"
    fi
fi

# ---------------------------------------------------------------- apt deps
# GDD §17.3. Only touches apt when something is actually missing, so a
# converged re-run needs no network here. piper is not packaged by Ubuntu;
# install it separately to /usr/local/bin/piper (see the checklist).
APT_PKGS=(
    build-essential autoconf automake libtool m4 cmake pkg-config
    libopus-dev
    python3.12 python3.12-venv
    sqlite3 rsync curl
)
missing=()
for p in "${APT_PKGS[@]}"; do
    dpkg -s "${p}" >/dev/null 2>&1 || missing+=("${p}")
done
if (( ${#missing[@]} )); then
    say "Installing system packages: ${missing[*]}"
    export DEBIAN_FRONTEND=noninteractive
    run apt-get update -qq
    run apt-get install -y -qq "${missing[@]}"
else
    say "System packages already installed"
fi

# ---------------------------------------------------------------- aura user
# The unix user stays `aura` deliberately (file ownership continuity;
# invisible to Discord).
if id -u aura >/dev/null 2>&1; then
    say "User 'aura' already exists"
else
    say "Creating system user 'aura' (nologin)"
    run useradd --system --shell /usr/sbin/nologin \
        --home-dir "${VARLIB}" --no-create-home aura
fi

# ---------------------------------------------------------------- host layout
say "Creating directories (GDD §17.2)"
run install -d -m 0755 "${OPT}" "${RELEASES}"
run install -d -m 0755 "${OPT}/models/wake" "${OPT}/models/whisper" "${OPT}/models/piper"
run install -d -m 0750 -o root -g aura "${ETC}"
run install -d -m 0750 -o aura -g aura "${VARLIB}"
# Pre-create the database with the right owner: the natural way to run the
# gazetteer seeder is plain `sudo python -m cortana.nlu.seed ...`, which would
# otherwise create cortana.db as root:root and crash cortana-brain (User=aura)
# with "attempt to write a readonly database" on its first migration.
if [[ ! -f "${VARLIB}/cortana.db" ]]; then
    run install -m 0640 -o aura -g aura /dev/null "${VARLIB}/cortana.db"
fi

# ------------------------------------------------------------------- STAGE
# Release id: <date>-<sha>. A clean checkout whose sha is already staged
# and complete is reused instead of rebuilt (idempotent fast path).
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short=12 HEAD 2>/dev/null || echo nogit)"
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain -- brain deploy config docs 2>/dev/null)" ]]; then
    GIT_SHA="${GIT_SHA}-dirty"
fi
RELEASE_ID="$(date +%Y%m%d-%H%M%S)-${GIT_SHA}"

EXISTING_REL=""
if [[ "${GIT_SHA}" != *dirty* && "${GIT_SHA}" != nogit ]]; then
    for d in "${RELEASES}"/*-"${GIT_SHA}"; do
        [[ -f "${d}/.complete" ]] && EXISTING_REL="${d}"
    done
fi

SAME_RELEASE=0
if [[ -n "${EXISTING_REL}" && "$(live_release)" == "$(readlink -f "${EXISTING_REL}")" ]]; then
    SAME_RELEASE=1
fi

if (( SAME_RELEASE )); then
    REL="${EXISTING_REL}"
    say "STAGE: release $(basename "${REL}") is already live — nothing to stage"
elif [[ -n "${EXISTING_REL}" ]]; then
    REL="${EXISTING_REL}"
    say "STAGE: reusing already-staged release $(basename "${REL}")"
else
    stage_release   # sets REL
fi

# ------------------------------------------------------------------ config
# Copy examples only if absent — never clobber a live, tuned config.
say "Installing config examples (existing files kept)"
declare -A CONFIGS=(
    ["${REPO_ROOT}/config/cortana.yaml.example"]="${ETC}/cortana.yaml"
    ["${REPO_ROOT}/config/routing.yaml.example"]="${ETC}/routing.yaml"
    ["${REPO_ROOT}/config/gazetteer.yaml.example"]="${ETC}/gazetteer.yaml"
    ["${REPO_ROOT}/ears/ears.yaml.example"]="${ETC}/ears.yaml"
)
for src in "${!CONFIGS[@]}"; do
    dst="${CONFIGS[${src}]}"
    if [[ -f "${dst}" ]]; then
        note "${dst} exists — kept"
    else
        run install -m 0640 -o root -g aura "${src}" "${dst}"
        note "${dst} installed from $(basename "${src}")"
    fi
done

# cortana-brain.service loads /etc/cortana/anthropic unconditionally
# (LoadCredential= fails unit start when the source is missing), so guarantee
# it exists. Empty is fine: an empty credential reads as "no key" and the
# override channel stays off. Never overwrite a real key.
if [[ ! -f "${ETC}/anthropic" ]]; then
    run install -m 0600 -o root -g root /dev/null "${ETC}/anthropic"
    note "created empty ${ETC}/anthropic (chat override channel off)"
fi

# -------------------------------------------------------------------- GATE
say "GATE: preflight against live ${ETC}"

# Both units LoadCredential= the token; enabling them without it guarantees a
# cryptic credential failure on first start. Abort loudly instead — nothing
# has been enabled or flipped yet.
if [[ ! -s "${ETC}/token" ]]; then
    if [[ "${DRYRUN}" == 1 ]]; then
        warn "no ${ETC}/token — a real run would abort here"
    else
        cat >&2 <<TOKENEOF

FAIL: ${ETC}/token is missing or empty. Both services load it via
LoadCredential= and cannot start without it. Create it, then re-run:

  printf '%s' 'YOUR_BOT_TOKEN' > /etc/cortana/token
  chmod 0600 /etc/cortana/token && chown root:root /etc/cortana/token

Nothing live was changed (the staged release is kept and will be reused).
TOKENEOF
        exit 1
    fi
else
    token_mode="$(stat -c %a "${ETC}/token")"
    [[ "${token_mode}" == 600 ]] \
        || warn "${ETC}/token mode is ${token_mode}, expected 0600 (chmod 0600 ${ETC}/token)"
fi

# Failure alerts degrade without a webhook — warn every run until it exists.
if [[ ! -s "${ETC}/alert_webhook" ]]; then
    warn "no ${ETC}/alert_webhook — unit failures will only reach ${VARLIB}/alerts.log, not Discord."
    warn "to fix: printf '%s' 'https://discord.com/api/webhooks/...' > ${ETC}/alert_webhook && chmod 0600 ${ETC}/alert_webhook"
fi

# Run the NEW release's doctor, offline, as the service user, against the
# live config — a bad operator edit or a broken release surfaces here, before
# the flip, instead of as a crash-loop after it. Transitional: releases built
# before cortana.doctor exists get a warning, not an abort.
DOCTOR_RESULT="SKIP (no doctor module in release)"
if [[ "${DRYRUN}" == 1 ]]; then
    note "dry-run: would run 'python -m cortana.doctor --config ${ETC}/cortana.yaml'"
    note "dry-run: offline, as aura via setpriv, aborting the deploy on failure"
    DOCTOR_RESULT="SKIP (dry-run)"
elif "${REL}/venv/bin/python" -c \
        'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("cortana.doctor") else 1)' \
        2>/dev/null; then
    note "running release doctor (offline) as aura"
    doctor_rc=0
    setpriv --reuid aura --regid aura --clear-groups \
        env HOME="${VARLIB}" \
        "${REL}/venv/bin/python" -m cortana.doctor --config "${ETC}/cortana.yaml" \
        || doctor_rc=$?
    if (( doctor_rc == 0 )); then
        DOCTOR_RESULT=PASS
    else
        DOCTOR_RESULT="FAIL (exit ${doctor_rc})"
        die "doctor rejected the new release against the live config (exit ${doctor_rc}). Nothing live was changed — fix the reported problem and re-run install.sh."
    fi
else
    warn "cortana.doctor not present in this release — skipping the preflight gate"
fi

# ----------------------------------------------------------------- systemd
say "Installing systemd units, alert hook and tmpfiles"
UNITS_CHANGED=0
install_if_changed "${SCRIPT_DIR}/cortana-brain.service" /etc/systemd/system/cortana-brain.service 0644
install_if_changed "${SCRIPT_DIR}/cortana-ears.service" /etc/systemd/system/cortana-ears.service 0644
install_if_changed "${SCRIPT_DIR}/cortana-alert@.service" /etc/systemd/system/cortana-alert@.service 0644
# alert.sh lives at a fixed path, NOT under current/: OnFailure fires exactly
# when a deploy may have left current/ broken, and the alerter must survive that.
install_if_changed "${SCRIPT_DIR}/alert.sh" "${OPT}/alert.sh" 0755
install_if_changed "${SCRIPT_DIR}/cortana.tmpfiles.conf" /etc/tmpfiles.d/cortana.conf 0644
run systemd-tmpfiles --create /etc/tmpfiles.d/cortana.conf
if (( UNITS_CHANGED )); then
    sysctl_run daemon-reload
fi
sysctl_run enable --quiet cortana-brain.service cortana-ears.service

# -------------------------------------------------------------------- FLIP
# Captured BEFORE the flip: the rollback target and the live ears hash.
PREV_REL="$(live_release)"
OLD_EARS_HASH=""
if [[ -n "${PREV_REL}" ]]; then
    OLD_EARS_HASH="$(ears_hash_of "${PREV_REL}")"
elif [[ -x "${OPT}/bin/cortana-ears" ]]; then
    OLD_EARS_HASH="$(sha256sum "${OPT}/bin/cortana-ears" | awk '{print $1}')"
fi

if (( SAME_RELEASE )); then
    say "FLIP: current already points at $(basename "${REL}")"
else
    say "FLIP: current -> $(basename "${REL}")"
    run flip_current "${REL}"
fi

# ----------------------------------------------------------------- RESTART
say "RESTART"
EARS_RESTARTED=0
NEW_EARS_HASH="$(ears_hash_of "${REL}")"

if (( SAME_RELEASE )) && (( ! UNITS_CHANGED )); then
    if [[ "${DRYRUN}" != 1 ]] && ! systemctl is-active --quiet cortana-brain.service; then
        note "no changes, but cortana-brain is not running — starting it"
        sysctl_run restart cortana-brain.service
    else
        note "no code or unit changes — brain restart skipped"
    fi
else
    note "restarting cortana-brain (new release/units live)"
    sysctl_run restart cortana-brain.service
fi

if [[ -z "${NEW_EARS_HASH}" ]]; then
    note "no ears binary in this release — cortana-ears left alone"
elif [[ "${NEW_EARS_HASH}" != "${OLD_EARS_HASH}" ]]; then
    note "ears binary hash changed — restarting cortana-ears"
    sysctl_run restart cortana-ears.service
    EARS_RESTARTED=1
elif [[ "${DRYRUN}" != 1 ]] && ! systemctl is-active --quiet cortana-ears.service; then
    note "cortana-ears not running — starting it"
    sysctl_run restart cortana-ears.service
    EARS_RESTARTED=1
else
    note "ears binary unchanged — cortana-ears left running (buffers through the brain restart)"
fi

# ------------------------------------------------------------------ VERIFY
if verify_release "${REL}"; then
    if [[ "${DRYRUN}" == 1 ]]; then
        say "DRY RUN complete — nothing was touched"
    else
        say "Deploy verified: $(basename "${REL}")"
        prune_releases
    fi
else
    show_journal_tail
    if [[ -n "${PREV_REL}" && "${PREV_REL}" != "$(readlink -f "${REL}")" ]]; then
        say "VERIFY FAILED — rolling back to $(basename "${PREV_REL}")"
        run flip_current "${PREV_REL}"
        sysctl_run restart cortana-brain.service
        if (( EARS_RESTARTED )); then
            sysctl_run restart cortana-ears.service
        fi
        die "deploy of $(basename "${REL}") failed verification and was rolled back to $(basename "${PREV_REL}") — see the journal tail above. The bad release is kept in ${RELEASES} for inspection."
    fi
    die "deploy failed verification and there is no previous release to roll back to — see the journal tail above"
fi

# --------------------------------------------------------------- checklist
cat <<'CHECKLIST'

==> Done. First-time setup that still needs a human (skip what's done):

  1. Piper TTS binary at /usr/local/bin/piper
     (https://github.com/OHF-Voice/piper1-gpl — not in Ubuntu's archive).

  2. Model files (GDD §4 Assets):
       /opt/cortana/models/wake/     openWakeWord ONNX chain (melspec, embedding,
                                     and the trained "hey cortana" model)
       /opt/cortana/models/whisper/  Whisper small int8 CTranslate2 weights
       /opt/cortana/models/piper/    Piper voice .onnx + .onnx.json

  3. Edit /etc/cortana/{cortana,routing,gazetteer,ears}.yaml — installed
     copies are examples with placeholder IDs.

  4. Seed the gazetteer AS THE SERVICE USER so the database stays writable:
       sudo -u aura /opt/cortana/current/venv/bin/python -m cortana.nlu.seed \
           --db /var/lib/cortana/cortana.db ...

  5. Failure alerts to Discord: put a webhook URL in /etc/cortana/alert_webhook
     (mode 0600). Without it, alerts only land in /var/lib/cortana/alerts.log.

  6. Firewall: ufw deny incoming except SSH. CORTANA opens no listening ports.

  Useful afterwards:
       journalctl -u cortana-brain -u cortana-ears -f
       sudo deploy/install.sh --rollback     # flip back to the previous release

CHECKLIST
