#!/usr/bin/env bash
# Installs, upgrades or removes ShelfSaver on a Debian or Ubuntu machine. Run as root (sudo).
#
# Install / upgrade (same command): builds the new version beside the old one, then stops
# ShelfSaver, swaps the program folder and starts it again. If anything goes wrong the old
# version is put back and started. Your settings, history and logs in the data folder are
# not touched.
#
#   sudo ./scripts/install.sh --media /srv/media [--media DIR ...] [--scratch DIR ...]
#                             [--user NAME] [--encoder nvenc|qsv]
#   sudo ./scripts/install.sh --uninstall [--remove-data]
#
#   --media DIR     Folder your libraries are in (repeat for several). Needed the first time;
#                   an update keeps the folders already set.
#   --scratch DIR   Folder a scratch disk may be in (optional, repeatable).
#   --user NAME     Account ShelfSaver runs as. Default: the owner of the first media folder,
#                   so converted files keep the right owner. Never root.
#   --encoder X     nvenc (NVIDIA) or qsv (Intel Quick Sync). Default: whichever works.
#   --uninstall     Stops ShelfSaver and removes its service, firewall rule and program folder.
#                   The data folder is kept unless --remove-data is also given.
#   --remove-data   With --uninstall only: also delete the data folder (settings, history, login).
#
# Your video libraries, holding folders and scratch disks are never touched.
set -euo pipefail

SERVICE=shelfsaver
PORT=8642
APP_DIR=/opt/shelfsaver
NEW_DIR="$APP_DIR.new"
OLD_DIR="$APP_DIR.old"
DATA_DIR=/var/lib/shelfsaver
CONFIG_PATH="$DATA_DIR/config.json"
UNIT_PATH="/etc/systemd/system/$SERVICE.service"
IMPORT_CHECK='import shelfsaver, uvicorn, fastapi, watchdog, mcp'
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'NOTE: %s\n' "$*"; }
fail() { printf '\nSetup did not finish: %s\n' "$*" >&2; exit 1; }

# --- Options ------------------------------------------------------------------------------------
parse_args() {
    MEDIA=()
    SCRATCH=()
    RUN_USER=""
    ENCODER=""
    UNINSTALL=0
    REMOVE_DATA=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --media) [[ $# -ge 2 ]] || fail "--media needs a folder."; MEDIA+=("$2"); shift 2 ;;
            --scratch) [[ $# -ge 2 ]] || fail "--scratch needs a folder."; SCRATCH+=("$2"); shift 2 ;;
            --user) [[ $# -ge 2 ]] || fail "--user needs an account name."; RUN_USER="$2"; shift 2 ;;
            --encoder) [[ $# -ge 2 ]] || fail "--encoder needs nvenc or qsv."; ENCODER="$2"; shift 2 ;;
            --uninstall) UNINSTALL=1; shift ;;
            --remove-data) REMOVE_DATA=1; shift ;;
            -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
            *) fail "Unknown option: $1 (see --help)." ;;
        esac
    done
    case "$ENCODER" in ""|nvenc|qsv) ;; *) fail "--encoder must be nvenc or qsv, not '$ENCODER'." ;; esac
    if [[ "$RUN_USER" == root ]]; then
        fail "ShelfSaver doesn't run as root: converted files would belong to root. Use --user with the account that owns your videos."
    fi
    if [[ $REMOVE_DATA -eq 1 && $UNINSTALL -eq 0 ]]; then
        fail "--remove-data only works together with --uninstall."
    fi
}

# --- Checks -------------------------------------------------------------------------------------

# The account that owns DIR. Root, or a user ID with no account (common on network shares), is
# refused rather than guessed: the service would then make files nobody expects.
choose_user() {
    local uid name
    uid="$(stat -c %u -- "$1")" || fail "Can't read the owner of $1."
    if [[ "$uid" -eq 0 ]]; then
        fail "$1 belongs to root. Use --user with the account that should own converted files."
    fi
    name="$(getent passwd "$uid" | cut -d: -f1)" || true
    if [[ -z "$name" ]]; then
        fail "$1 belongs to user ID $uid, which has no account on this machine. Use --user to choose one."
    fi
    printf '%s\n' "$name"
}

# FFmpeg before 7.1 drops the HDR10 mastering and light-level data when encoding with NVENC.
# Takes the first line of `ffmpeg -version`. A git build ("N-118000-g...") has no release number;
# it's assumed recent.
ffmpeg_new_enough() {
    local version major minor
    version="${1#*version }"
    version="${version%% *}"
    version="${version#n}"
    [[ "$version" =~ ^([0-9]+)(\.([0-9]+))? ]] || return 0
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[3]:-0}"
    (( major > 7 || (major == 7 && minor >= 1) ))
}

# True when PATH is the program folder or one of its swap copies: running setup from there would
# delete the copy that is running.
inside_app_dir() {
    local dir
    for dir in "$APP_DIR" "$NEW_DIR" "$OLD_DIR"; do
        [[ "$1" == "$dir" || "$1" == "$dir"/* ]] && return 0
    done
    return 1
}

# --- Settings and service file ------------------------------------------------------------------

# Write config.json: keep every key already there, set encoder/ffmpeg/ffprobe, and set the media
# and scratch roots only when given (an update without --media keeps the folders already set).
# Values reach Python as arguments, never as code. An unreadable file is kept as config.json.bad.
write_config() {
    python3 - "$1" "$2" "$3" "$4" "${#MEDIA[@]}" ${MEDIA[@]+"${MEDIA[@]}"} \
        ${SCRATCH[@]+"${SCRATCH[@]}"} <<'PY'
import json, os, sys, tempfile
path, encoder, ffmpeg, ffprobe, count = sys.argv[1:6]
media = sys.argv[6:6 + int(count)]
scratch = sys.argv[6 + int(count):]
config = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        if not isinstance(config, dict):
            raise ValueError("not settings")
    except ValueError:
        os.replace(path, path + ".bad")
        print("NOTE: The old config.json could not be read. It was kept as config.json.bad "
              "and a new one will be written.")
        config = {}
config.update(encoder=encoder, ffmpeg=ffmpeg, ffprobe=ffprobe)
if media:
    config["media_roots"] = media
if scratch:
    config["scratch_roots"] = scratch
folder = os.path.dirname(os.path.abspath(path))
fd, temp = tempfile.mkstemp(dir=folder, prefix=".config-", suffix=".json")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.replace(temp, path)
PY
}

# True when config.json already names at least one media folder.
config_has_media() {
    [[ -f "$1" ]] || return 1
    python3 - "$1" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        roots = json.load(f).get("media_roots")
except (ValueError, AttributeError):
    sys.exit(1)
sys.exit(0 if isinstance(roots, list) and roots else 1)
PY
}

# Group IDs that own the Intel render nodes. Quick Sync opens /dev/dri/renderD*; the service gets
# these as supplementary groups instead of the account being added to them system-wide.
render_groups() {
    local dev gid seen=" "
    for dev in ${DRI_GLOB:-/dev/dri/renderD*}; do
        [[ -e "$dev" ]] || continue
        gid="$(stat -c %g -- "$dev")"
        [[ "$gid" -eq 0 || "$seen" == *" $gid "* ]] && continue
        seen+="$gid "
    done
    seen="${seen# }"
    [[ -n "$seen" ]] && printf '%s\n' "${seen% }"
    return 0
}

# The systemd service file for USER / GROUP, with the render-node groups GIDS (may be empty).
service_unit() {
    cat <<EOF
[Unit]
Description=ShelfSaver: converts video libraries to save space
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$1
Group=$2
EOF
    [[ -n "$3" ]] && printf 'SupplementaryGroups=%s\n' "$3"
    cat <<EOF
WorkingDirectory=$APP_DIR
Environment=SHELFSAVER_DATA=$DATA_DIR
ExecStart=$APP_DIR/venv/bin/python -m shelfsaver
Restart=always
RestartSec=10
TimeoutStopSec=60
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
}

# --- The install itself -------------------------------------------------------------------------

SWAPPING=0       # from the swap until success, a failure puts the old version back
SUCCEEDED=0
OLD_MOVED=0
NEW_IN_PLACE=0
CONFIG_WRITTEN=0
UNIT_WRITTEN=0
WORK=""          # temporary folder: build staging and the copies kept for a rollback
RUN_UID=""
RUN_GID=""
GIDS=""

check_platform() {
    [[ $EUID -eq 0 ]] || fail "ShelfSaver setup needs to run as root. Run the same command with sudo in front."
    command -v apt-get >/dev/null || fail "This installer is for Debian and Ubuntu (it uses apt). On other distros, run ShelfSaver by hand as the README describes."
    [[ -d /run/systemd/system ]] || fail "systemd isn't running on this machine, so ShelfSaver can't be set up to start by itself. Run it by hand as the README describes."
    if inside_app_dir "$REPO_ROOT"; then
        fail "Run setup from a copy of ShelfSaver outside $APP_DIR (it replaces that folder)."
    fi
}

# Leftovers from a run that was cut off. If the swap was interrupted, put the old version back.
recover_leftovers() {
    if [[ -x "$OLD_DIR/venv/bin/python" && ! -x "$APP_DIR/venv/bin/python" ]]; then
        rm -rf -- "$APP_DIR"
        mv -- "$OLD_DIR" "$APP_DIR"
        warn "The last setup was cut off during the switch-over; the previous version was put back."
    fi
    rm -rf -- "$NEW_DIR" "$OLD_DIR"
}

install_packages() {
    local missing=()
    command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || missing+=(ffmpeg)
    command -v python3 >/dev/null || missing+=(python3 python3-venv)
    if command -v python3 >/dev/null && ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
        missing+=(python3-venv)
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then
        say "Installing ${missing[*]} ..."
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}" >/dev/null
    fi
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
        fail "ShelfSaver needs Python 3.10 or newer; this machine has $(python3 --version 2>&1)."
    python3 -c 'import venv, ensurepip' 2>/dev/null ||
        fail "Python's venv module is missing. Install it (apt-get install python3-venv), then run this again."
    FFMPEG="$(command -v ffmpeg)" || fail "FFmpeg could not be installed."
    FFPROBE="$(command -v ffprobe)" || fail "ffprobe could not be installed."
    say "Using $(python3 --version 2>&1) at $(command -v python3)"
    say "Using $FFMPEG"
    local version
    version="$("$FFMPEG" -hide_banner -version 2>/dev/null | head -n 1)"
    if ! ffmpeg_new_enough "$version"; then
        warn "This FFmpeg ($version) is older than 7.1. It drops the HDR10 brightness data when converting with NVIDIA; install FFmpeg 7.1 or newer if your library has HDR videos."
    fi
}

# Media and scratch folders, and the account to run as. An update keeps what is already set.
choose_folders_and_user() {
    local dir first=""
    if [[ ${#MEDIA[@]} -eq 0 ]] && ! config_has_media "$CONFIG_PATH"; then
        if [[ -t 0 ]]; then
            read -r -p "Which folder are your video libraries in? " dir
            [[ -n "$dir" ]] && MEDIA+=("$dir")
        fi
        [[ ${#MEDIA[@]} -gt 0 ]] || fail "Say where your videos are with --media, for example: sudo $0 --media /srv/media"
    fi
    for dir in ${MEDIA[@]+"${MEDIA[@]}"} ${SCRATCH[@]+"${SCRATCH[@]}"}; do
        [[ "$dir" == /* ]] || fail "$dir isn't a full path. Use one that starts with /."
        [[ -d "$dir" ]] || fail "$dir doesn't exist or isn't a folder."
    done
    if [[ ${#MEDIA[@]} -gt 0 ]]; then
        first="${MEDIA[0]}"
    else
        first="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["media_roots"][0])' "$CONFIG_PATH")"
    fi
    # --user wins; then the account an earlier install chose; then the owner of the videos.
    if [[ -z "$RUN_USER" && -f "$UNIT_PATH" ]]; then
        RUN_USER="$(sed -n 's/^User=//p' "$UNIT_PATH" | head -n 1)"
    fi
    if [[ -z "$RUN_USER" ]]; then
        [[ -d "$first" ]] || fail "$first doesn't exist. Use --media to say where your videos are."
        RUN_USER="$(choose_user "$first")" || exit 1
    fi
    [[ "$RUN_USER" != root ]] || fail "ShelfSaver doesn't run as root. Use --user with the account that owns your videos."
    RUN_UID="$(id -u -- "$RUN_USER" 2>/dev/null)" || fail "There is no account called $RUN_USER."
    [[ "$RUN_UID" -ne 0 ]] || fail "$RUN_USER is a root account. Use --user with a normal account."
    RUN_GID="$(id -g -- "$RUN_USER")"
    RUN_GROUP="$(id -gn -- "$RUN_USER")"
    GIDS="$(render_groups)"
    say "ShelfSaver will run as $RUN_USER (group $RUN_GROUP)."
}

prepare_data_dir() {
    mkdir -p -- "$DATA_DIR"
    if [[ "$(stat -c %u:%g -- "$DATA_DIR")" != "$RUN_UID:$RUN_GID" ]]; then
        chown -R -- "$RUN_UID:$RUN_GID" "$DATA_DIR"
    fi
    chmod 700 -- "$DATA_DIR"
    # Byte-for-byte copies for a rollback.
    [[ -f "$CONFIG_PATH" ]] && cp -p -- "$CONFIG_PATH" "$WORK/config.json"
    [[ -f "$UNIT_PATH" ]] && cp -p -- "$UNIT_PATH" "$WORK/unit"
    return 0
}

# Run a command as the service will: same account, same extra groups, from a neutral folder (so
# Python can't pick up a `shelfsaver` folder from wherever setup was started).
as_service_user() {
    local groups=(--clear-groups)
    [[ -n "$GIDS" ]] && groups=(--groups "${GIDS// /,}")
    (cd / && setpriv --reuid "$RUN_UID" --regid "$RUN_GID" "${groups[@]}" \
        env HOME="$DATA_DIR" SHELFSAVER_DATA="$DATA_DIR" "$@")
}

build_new() {
    local staging="$WORK/staging" file
    mkdir -p -- "$staging" "$NEW_DIR/scripts"
    cp -r -- "$REPO_ROOT/shelfsaver" "$staging/"
    for file in pyproject.toml LICENSE README.md; do
        [[ -f "$REPO_ROOT/$file" ]] && cp -- "$REPO_ROOT/$file" "$staging/"
    done
    find "$staging" -name __pycache__ -type d -prune -exec rm -rf {} +
    say "Setting up (this downloads a few packages and can take a minute)..."
    python3 -m venv "$NEW_DIR/venv"
    "$NEW_DIR/venv/bin/pip" install --quiet --disable-pip-version-check --no-warn-script-location "$staging"
    cp -- "$REPO_ROOT/scripts/make_compare_clips.py" "$NEW_DIR/scripts/"
    chmod -R a+rX -- "$NEW_DIR"
    as_service_user "$NEW_DIR/venv/bin/python" -c "$IMPORT_CHECK" ||
        fail "The new version could not start as $RUN_USER."
}

# ShelfSaver's own start-up check, run as the service user, so it also proves the account can open
# the GPU. NVIDIA first, then Intel; --encoder checks only that one.
detect_encoder() {
    local candidates=(nvenc qsv) name
    [[ -n "$ENCODER" ]] && candidates=("$ENCODER")
    for name in "${candidates[@]}"; do
        if as_service_user "$NEW_DIR/venv/bin/python" -c '
import sys
from shelfsaver.encode import encoder_works
from shelfsaver.encoders import ENCODERS
sys.exit(0 if encoder_works(sys.argv[1], ENCODERS[sys.argv[2]]()) else 1)' "$FFMPEG" "$name"; then
            ENCODER="$name"
            say "Converting with $([[ $name == nvenc ]] && echo 'NVIDIA NVENC' || echo 'Intel Quick Sync')."
            return 0
        fi
    done
    fail "No graphics card could convert video (tried ${candidates[*]}). For NVIDIA, install the NVIDIA driver; for Intel, install intel-media-va-driver-non-free libvpl2 libmfx-gen1.2 (on Debian these need the non-free component). Then run this again."
}

port_in_use() {
    ss -ltnH "sport = :$PORT" 2>/dev/null | grep -q .
}

swap_in() {
    SWAPPING=1
    if systemctl list-unit-files "$SERVICE.service" >/dev/null 2>&1; then
        systemctl stop "$SERVICE" 2>/dev/null || true
    fi
    if port_in_use; then
        local owner
        owner="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -n 1 | cut -d'"' -f2)"
        fail "Port $PORT is already being used by another program${owner:+ ($owner)}, for example a copy of ShelfSaver started by hand. Stop it, then run setup again."
    fi
    if [[ -d "$APP_DIR" ]]; then
        mv -- "$APP_DIR" "$OLD_DIR"
        OLD_MOVED=1
    fi
    mv -- "$NEW_DIR" "$APP_DIR"
    NEW_IN_PLACE=1
    # A venv records its own path, so check it still starts from the final folder.
    as_service_user "$APP_DIR/venv/bin/python" -c "$IMPORT_CHECK" ||
        fail "The new version could not start from $APP_DIR."

    CONFIG_WRITTEN=1
    write_config "$CONFIG_PATH" "$ENCODER" "$FFMPEG" "$FFPROBE"
    chown -- "$RUN_UID:$RUN_GID" "$CONFIG_PATH"
    chmod 600 -- "$CONFIG_PATH"
    say "Saved settings to $CONFIG_PATH"

    UNIT_WRITTEN=1
    service_unit "$RUN_USER" "$RUN_GROUP" "$GIDS" > "$UNIT_PATH"
    chmod 644 -- "$UNIT_PATH"
    systemctl daemon-reload
    systemctl enable --quiet "$SERVICE"
}

healthz_ok() {
    python3 - "$PORT" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

confirm_running() {
    systemctl start "$SERVICE"
    local waited=0
    until healthz_ok; do
        if (( waited >= 90 )); then
            journalctl -u "$SERVICE" -n 15 --no-pager 2>/dev/null || true
            fail "The new version did not start (see the lines above)."
        fi
        sleep 2
        waited=$((waited + 2))
    done
    # Something else could be answering, so also confirm the service itself stayed up.
    sleep 3
    if ! systemctl is-active --quiet "$SERVICE" || ! healthz_ok; then
        fail "The new version started and then stopped again."
    fi
}

open_firewall() {
    if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
        ufw allow "$PORT/tcp" comment ShelfSaver >/dev/null
        say "Port $PORT is open in ufw."
    elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --quiet --permanent --add-port="$PORT/tcp" && firewall-cmd --quiet --reload
        say "Port $PORT is open in firewalld."
    else
        say "No firewall is running (ufw or firewalld), so nothing needed opening."
    fi
}

remove_firewall() {
    if command -v ufw >/dev/null; then ufw delete allow "$PORT/tcp" >/dev/null 2>&1 || true; fi
    if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
        firewall-cmd --quiet --permanent --remove-port="$PORT/tcp" 2>/dev/null && firewall-cmd --quiet --reload || true
    fi
}

# Put the previous version back exactly as it was. Runs only when the swap had started.
rollback() {
    say "Putting the previous version back..."
    systemctl stop "$SERVICE" 2>/dev/null || true
    if [[ $NEW_IN_PLACE -eq 1 ]]; then rm -rf -- "$APP_DIR"; fi
    if [[ $OLD_MOVED -eq 1 ]]; then mv -- "$OLD_DIR" "$APP_DIR"; fi
    if [[ $CONFIG_WRITTEN -eq 1 ]]; then
        if [[ -f "$WORK/config.json" ]]; then cp -p -- "$WORK/config.json" "$CONFIG_PATH"
        else rm -f -- "$CONFIG_PATH"; fi
    fi
    if [[ $UNIT_WRITTEN -eq 1 ]]; then
        if [[ -f "$WORK/unit" ]]; then cp -p -- "$WORK/unit" "$UNIT_PATH"
        else systemctl disable --quiet "$SERVICE" 2>/dev/null || true; rm -f -- "$UNIT_PATH"; fi
        systemctl daemon-reload
    fi
    if [[ $OLD_MOVED -eq 1 && -f "$UNIT_PATH" ]]; then
        if systemctl start "$SERVICE"; then say "The previous version is running again."
        else warn "The previous version could not be started again: journalctl -u $SERVICE"; fi
    fi
}

on_exit() {
    local code=$?
    trap - EXIT
    if [[ $code -ne 0 && $SWAPPING -eq 1 && $SUCCEEDED -eq 0 ]]; then rollback || true; fi
    [[ -n "$WORK" ]] && rm -rf -- "$WORK"
    exit "$code"
}

on_error() {
    printf '\nSetup did not finish: this step failed: %s (line %s)\n' "$1" "$2" >&2
}

uninstall() {
    step 'Removing ShelfSaver'
    if [[ -f "$UNIT_PATH" ]]; then
        systemctl disable --now --quiet "$SERVICE" 2>/dev/null || true
        rm -f -- "$UNIT_PATH"
        systemctl daemon-reload
    fi
    remove_firewall
    rm -rf -- "$APP_DIR" "$NEW_DIR" "$OLD_DIR"
    say "Removed the service, the firewall rule and $APP_DIR."
    if [[ $REMOVE_DATA -eq 1 ]]; then
        rm -rf -- "$DATA_DIR"
        say "Deleted the settings, history and login in $DATA_DIR."
    else
        say "Your settings, history and login are kept in $DATA_DIR."
    fi
    say "Your video libraries, holding folders and scratch disks were not touched."
}

main() {
    parse_args "$@"
    check_platform
    umask 022
    if [[ $UNINSTALL -eq 1 ]]; then uninstall; return 0; fi

    WORK="$(mktemp -d)"
    trap on_exit EXIT
    set -E
    trap 'on_error "$BASH_COMMAND" "$LINENO"' ERR

    recover_leftovers
    step 'Checking Python and FFmpeg'
    install_packages
    step 'Checking the folders and the account'
    choose_folders_and_user
    step 'Preparing the data folder'
    prepare_data_dir
    step 'Building the new version (the current one keeps running meanwhile)'
    build_new
    step 'Checking the graphics card'
    detect_encoder "$FFMPEG"
    step 'Switching to the new version'
    swap_in
    step 'Starting ShelfSaver'
    confirm_running

    # The new version works; from here on nothing may undo it.
    SUCCEEDED=1
    rm -rf -- "$OLD_DIR" || warn "Could not delete the old copy in $OLD_DIR. It is harmless and will be removed next time setup runs."
    step 'Allowing access from other devices'
    open_firewall || warn "The firewall could not be changed; open port $PORT yourself if other devices can't reach the page."

    local address
    address="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^127\.' | grep -m 1 '\.' || true)"
    step 'Done'
    say "ShelfSaver is running. Open http://${address:-<this machine>}:$PORT/ in a browser."
    say "Logs: journalctl -u $SERVICE -f  (and $DATA_DIR/logs/shelfsaver.log)"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
