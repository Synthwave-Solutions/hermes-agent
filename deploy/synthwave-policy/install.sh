#!/usr/bin/env bash
# Install the synthwave-policy Hermes plugin. Does NOT edit any config.yaml and does NOT restart
# anything: it copies files and prints the exact config change you still have to make.
#
#   ./install.sh                      root profile only (~/.hermes/plugins/synthwave-policy)
#   ./install.sh --profiles all       root plus every ~/.hermes/profiles/<name>/plugins/
#   ./install.sh --profiles marcel,odis
#   ./install.sh --dry-run ...        show what would happen
#
# Why per profile: Hermes discovers user plugins in $HERMES_HOME/plugins and reads plugins.enabled
# from $HERMES_HOME/config.yaml. A profile (HERMES_HOME=~/.hermes/profiles/<name>) never sees the
# root plugin dir or the root plugins.enabled list, so each profile needs its own copy and its own
# enable line. The policy file is shared: ~/.hermes/policy/autonomy.yaml is authoritative for every
# profile; a profile's own policy/autonomy.yaml can only tighten it.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${HERMES_ROOT:-$HOME/.hermes}"
PROFILES=""
DRY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profiles) PROFILES="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

run() { if [[ $DRY -eq 1 ]]; then echo "  [dry-run] $*"; else "$@"; fi; }

install_into() {
  local home="$1" dest="$1/plugins/synthwave-policy" tmp
  echo "plugin -> $dest"
  run mkdir -p "$home/plugins"
  tmp="$home/plugins/.synthwave-policy.new.$$"
  run rm -rf "$tmp"
  run cp -r "$SRC_DIR/synthwave-policy" "$tmp"
  run find "$tmp" -name __pycache__ -type d -prune -exec rm -rf {} +
  if [[ -d "$dest" ]]; then run rm -rf "$dest.old"; run mv "$dest" "$dest.old"; fi
  run mv "$tmp" "$dest"
  run rm -rf "$dest.old"
}

homes=("$ROOT")
if [[ -n "$PROFILES" ]]; then
  if [[ "$PROFILES" == "all" ]]; then
    for d in "$ROOT"/profiles/*/; do [[ -f "$d/config.yaml" ]] && homes+=("${d%/}"); done
  else
    IFS=',' read -ra names <<< "$PROFILES"
    for n in "${names[@]}"; do
      [[ -d "$ROOT/profiles/$n" ]] || { echo "no such profile: $n" >&2; exit 2; }
      homes+=("$ROOT/profiles/$n")
    done
  fi
fi

for h in "${homes[@]}"; do install_into "$h"; done

# Shared policy file: only when absent, never overwrite an edited policy.
if [[ -f "$ROOT/policy/autonomy.yaml" ]]; then
  echo "policy: $ROOT/policy/autonomy.yaml already exists, left untouched"
else
  echo "policy -> $ROOT/policy/autonomy.yaml (mode: observe)"
  run mkdir -p "$ROOT/policy"
  run install -m 0644 "$SRC_DIR/policy/autonomy.example.yaml" "$ROOT/policy/autonomy.yaml"
fi

# CLIs next to the policy file (they import the policy_engine.py copied beside them).
run mkdir -p "$ROOT/policy/bin"
run install -m 0755 "$SRC_DIR/bin/policy_check.py" "$SRC_DIR/bin/policy_report.py" "$ROOT/policy/bin/"
run install -m 0644 "$SRC_DIR/synthwave-policy/policy_engine.py" "$ROOT/policy/bin/"

cat <<EOF

Not done by this script (do it yourself, then restart the Hermes processes that should pick it up):

1. Enable the plugin in every config.yaml whose home got a copy. Add one list item under the
   existing plugins.enabled list, for example in $ROOT/config.yaml:

     plugins:
       enabled:
       - audio-transcribe
       ...
       - synthwave-policy        # <- add this line

   A profile whose config has no plugins.enabled key needs the whole block:

     plugins:
       enabled:
       - synthwave-policy

   Equivalent CLI per home (this edits config.yaml):  HERMES_HOME=<home> hermes plugins enable synthwave-policy

2. Config files to change for this install:
EOF
for h in "${homes[@]}"; do echo "     $h/config.yaml"; done
cat <<EOF

3. Check a decision:   python3 $ROOT/policy/bin/policy_check.py terminal '{"command": "waapi send --to 31 --text hi"}'
   Read the log:       python3 $ROOT/policy/bin/policy_report.py --all
EOF
