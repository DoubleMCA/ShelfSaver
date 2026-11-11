#!/bin/sh
# Container start-up: become the right user, then run ShelfSaver.
#
# Why this exists: converted files must belong to the same user as the library
# (on Unraid that is nobody:users, 99:100). Running as root would give every new file
# to root. PUID/PGID/UMASK are the usual names for these settings in container templates.
#
# Starts as root only long enough to prepare the config folder and the GPU devices, then
# drops to PUID:PGID with setpriv (part of util-linux, already in the image, nothing extra).
# If the container was started as a non-root user already (Docker's `user:` option), there is
# nothing to drop and the same steps are skipped.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"

# Refuse anything that is not plain digits: these values end up in chown/setpriv arguments.
case "$PUID$PGID$UMASK" in
  *[!0-9]*) echo "PUID, PGID and UMASK must be numbers (got PUID=$PUID PGID=$PGID UMASK=$UMASK)" >&2; exit 64 ;;
esac

umask "$UMASK"
# Settings, history and the login live here; tools that look for a home folder find a writable one.
export HOME="${SHELFSAVER_DATA:-/config}"

if [ "$(id -u)" -ne 0 ]; then
  exec "$@"
fi

mkdir -p "$HOME"
# Only fix ownership when the folder itself is wrong: a recursive chown on every start would be
# slow on a big config folder and would hide a mount that is meant to belong to someone else.
if [ "$(stat -c '%u:%g' "$HOME")" != "$PUID:$PGID" ]; then
  chown -R "$PUID:$PGID" "$HOME"
fi

if [ "$PUID" -eq 0 ]; then
  echo "Running as root (PUID=0): converted files will belong to root." >&2
  exec "$@"
fi

# GPU access. Quick Sync opens /dev/dri/renderD*, which belongs to a group (usually "render")
# whose number differs between hosts, so the number is read from the device instead of guessed.
# Without it a non-root user gets "Permission denied" and Quick Sync silently does not work.
groups=""
for dev in /dev/dri/* /dev/nvidia*; do
  [ -e "$dev" ] || continue
  gid="$(stat -c '%g' "$dev")"
  [ "$gid" -eq 0 ] && continue          # root-group devices: nothing to add, and 0 would grant too much
  case ",$groups," in *",$gid,"*) ;; *) groups="${groups:+$groups,}$gid" ;; esac
done

if [ -n "$groups" ]; then
  exec setpriv --reuid "$PUID" --regid "$PGID" --groups "$groups" "$@"
fi
exec setpriv --reuid "$PUID" --regid "$PGID" --clear-groups "$@"
