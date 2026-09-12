#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=bin/fm-classify-lib.sh
. "$ROOT/bin/fm-classify-lib.sh"

TMP_ROOT=$(fm_test_tmproot fm-handsfree-tool)
export FM_HOME="$TMP_ROOT/home"
export FM_CONFIG_OVERRIDE="$FM_HOME/config"
export FM_GLOBAL_CONFIG_OVERRIDE="$TMP_ROOT/global-config"
export FM_ROOT_OVERRIDE="$ROOT"
export PYTHONDONTWRITEBYTECODE=1
PARSER="$ROOT/bin/fm-handsfree-tool.py"
mkdir -p "$FM_CONFIG_OVERRIDE"
chmod 0700 "$FM_CONFIG_OVERRIDE"

set_mode() {
  python3 "$ROOT/bin/fm-handsfree-answer.py" set-mode "$1" >/dev/null
}

action='{"session_id":"session-cli","task_id":"worker-cli","tool":"bash","args":{"command":"printf action-secret-cli"}}'
invoke() {
  local expected=$1 command=$2 payload=$3
  shift 3
  local code=0
  printf '%s' "$payload" | env "$@" python3 "$PARSER" "$command" \
    >"$TMP_ROOT/out.json" 2>"$TMP_ROOT/err" || code=$?
  [ "$code" = "$expected" ] || fail "$command returned $code, expected $expected"
  if rg -q 'action-secret-cli|user-words-secret-cli' "$TMP_ROOT/out.json" "$TMP_ROOT/err"; then
    fail "CLI leaked captured action or user words"
  fi
}

set_mode no_prompt
invoke 0 check "$action" FM_TASK_ID=worker-cli
jq -e '.status == "allow" and .reason == "no_prompt"' "$TMP_ROOT/out.json" >/dev/null \
  || fail "no_prompt must permit the custom layer without a question"
[ ! -d "$FM_HOME/state" ] || fail "no_prompt should not create pending state"
pass "CLI no_prompt allows without an extra question or pending record"

set_mode prompt
invoke 3 check "$action" FM_TASK_ID=worker-cli
request_id=$(jq -r .request_id "$TMP_ROOT/out.json")
record="$FM_HOME/state/handsfree-tools/requests/$request_id.json"
[ -f "$record" ] || fail "prompt did not persist a private request"
jq -e '.action.args.command == "printf action-secret-cli" and .action.session_id == "session-cli" and .action.task_id == "worker-cli"' \
  "$record" >/dev/null || fail "request lost the exact action binding"
assert_contains "$(<"$FM_HOME/state/worker-cli.status")" \
  "needs-decision [key=handsfree-$request_id]" "FirstMate did not receive the keyed decision"
assert_contains "$(status_open_decisions "$FM_HOME/state/worker-cli.status")" \
  "handsfree-$request_id"$'\tneeds-decision\t' \
  "FirstMate's existing decision reader did not recognize the pending action"
if rg -q action-secret-cli "$FM_HOME/state/worker-cli.status"; then
  fail "status event leaked action arguments"
fi
invoke 3 check "$action" FM_TASK_ID=worker-cli
[ "$(jq -r .request_id "$TMP_ROOT/out.json")" = "$request_id" ] \
  || fail "same pending action did not deduplicate"
[ "$(wc -l < "$FM_HOME/state/worker-cli.status" | tr -d ' ')" = 1 ] \
  || fail "pending deduplication repeated its decision event"
invoke 0 pending '{}'
jq -e --arg id "$request_id" 'length == 1 and .[0].request_id == $id' "$TMP_ROOT/out.json" >/dev/null \
  || fail "FirstMate pending intake did not expose the request reference"
pass "CLI prompt persists the exact action and reports one decision to FirstMate"

resolution=$(jq -nc --arg id "$request_id" \
  '{request_id:$id,decision:"allow",user_words:"user-words-secret-cli"}')
invoke 2 resolve "$resolution" FM_TASK_ID=worker-cli
jq -e '.status == "pending"' "$record" >/dev/null || fail "worker resolved its own request"
invoke 0 resolve "$resolution"
[ -z "$(status_open_decisions "$FM_HOME/state/worker-cli.status")" ] \
  || fail "primary resolution did not close FirstMate's open decision"
jq -e '.resolution.user_words == "user-words-secret-cli"' "$record" >/dev/null \
  || fail "resolver lost exact user words"
changed=$(printf '%s' "$action" | jq -c --arg id "$request_id" \
  '.request_id=$id | .args.command="different action"')
invoke 2 check "$changed" FM_TASK_ID=worker-cli
changed=$(printf '%s' "$action" | jq -c --arg id "$request_id" \
  '.request_id=$id | .session_id="different-session"')
invoke 2 check "$changed" FM_TASK_ID=worker-cli
invoke 0 check "$action" FM_TASK_ID=worker-cli
invoke 4 check "$action" FM_TASK_ID=worker-cli
invoke 4 resolve "$resolution"
pass "CLI primary resolution grants one exact retry; workers, mismatches and replays are rejected"

node "$ROOT/tests/fm-handsfree-tool.test.mjs" "$ROOT" "$TMP_ROOT" \
  || fail "HandsFreeBridge native hook behavior failed"
pass "HandsFreeBridge CLI and native hook checks passed without external calls"
