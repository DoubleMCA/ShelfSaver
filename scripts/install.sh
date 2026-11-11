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

main() {
    parse_args "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
