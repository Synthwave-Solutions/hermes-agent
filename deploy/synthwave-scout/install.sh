#!/usr/bin/env bash
# Install the Scout runner files. Does NOT create or change any cron job, profile or config.
#
#   ./install.sh                     -> ~/.hermes/scripts/scout/ (default profile)
#   ./install.sh --profile NAME ...  -> also ~/.hermes/profiles/NAME/scripts/scout/
#   ./install.sh --all-profiles      -> also every profile that has a profile.yaml
#
# Why per profile: the cron scheduler only runs scripts that resolve INSIDE the job's own
# <HERMES_HOME>/scripts/ (symlinks are resolved first, so a link to ~/.hermes/scripts is refused).
# A Scout job for profile NAME therefore needs its own copy in profiles/NAME/scripts/scout/.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${HERMES_ROOT:-$HOME/.hermes}"
FILES=(scout_collect.py scout_ledger.py prompt.md)
STAMP="$(date +%Y%m%d-%H%M%S)"

targets=("$ROOT/scripts/scout")
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      [[ -n "${2:-}" ]] || { echo "--profile needs a name" >&2; exit 2; }
      [[ -d "$ROOT/profiles/$2" ]] || { echo "unknown profile: $2" >&2; exit 2; }
      targets+=("$ROOT/profiles/$2/scripts/scout"); shift 2 ;;
    --all-profiles)
      for p in "$ROOT"/profiles/*/; do
        [[ -f "$p/profile.yaml" ]] && targets+=("${p%/}/scripts/scout")
      done
      shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

for f in "${FILES[@]}"; do
  [[ -f "$SRC/$f" ]] || { echo "missing source file: $SRC/$f" >&2; exit 1; }
done

for dest in "${targets[@]}"; do
  mkdir -p "$dest"
  for f in "${FILES[@]}"; do
    if [[ -f "$dest/$f" ]]; then
      if cmp -s "$SRC/$f" "$dest/$f"; then
        echo "unchanged  $dest/$f"; continue
      fi
      cp -p "$dest/$f" "$dest/$f.bak-$STAMP"
      echo "backup     $dest/$f.bak-$STAMP"
    fi
    install -m 0644 "$SRC/$f" "$dest/$f"
    echo "installed  $dest/$f"
  done
  chmod 0755 "$dest/scout_collect.py" "$dest/scout_ledger.py"
done

echo "Done. Installed into ${#targets[@]} location(s). No cron jobs were created or changed."
