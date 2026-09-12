#!/usr/bin/env bash
# fm-handsfree-answer.sh - feed one validated hands-free answer to Firstmate.
#
# Usage:
#   printf '%s' '<firstmate.handsfree-answer.v1 JSON>' | fm-handsfree-answer.sh
#   fm-handsfree-answer.sh mode
#   fm-handsfree-answer.sh set-mode <prompt|no_prompt>
#   fm-handsfree-answer.sh set-global-mode <prompt|no_prompt>
#   fm-handsfree-answer.sh policy   # effective mode, source, and policy path
#
# bin/fm-handsfree-answer.py owns policy and input validation.
# bin/fm-captain-hold.sh owns task identity, answer provenance, close mode,
# replay behavior, and every authority check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "${1:-submit}" in
  mode|policy)
    [ "$#" -eq 1 ] || exit 2
    exec python3 "$SCRIPT_DIR/fm-handsfree-answer.py" "$1"
    ;;
  set-mode|set-global-mode)
    [ "$#" -eq 2 ] || exit 2
    exec python3 "$SCRIPT_DIR/fm-handsfree-answer.py" "$1" "$2"
    ;;
  submit)
    [ "$#" -le 1 ] || exit 2
    python3 "$SCRIPT_DIR/fm-handsfree-answer.py" submit \
      | "$SCRIPT_DIR/fm-captain-hold.sh" answers --any-origin \
          --source "the built-in hands-free approval bridge"
    ;;
  *)
    exit 2
    ;;
esac
