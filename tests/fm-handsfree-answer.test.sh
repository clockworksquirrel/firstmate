#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TMP_ROOT=$(fm_test_tmproot fm-handsfree-answer)
HOME_ROOT="$TMP_ROOT/home"
CONFIG_ROOT="$HOME_ROOT/config"
PARSER="$ROOT/bin/fm-handsfree-answer.py"
WRAPPER="$ROOT/bin/fm-handsfree-answer.sh"
EXPECTED_DEFAULT="${FM_TEST_EXPECT_DEFAULT:-prompt}"
export FM_GLOBAL_CONFIG_OVERRIDE="$TMP_ROOT/global-config"

mkdir -p "$CONFIG_ROOT"
chmod 0700 "$CONFIG_ROOT"

[ "$(FM_HOME="$HOME_ROOT" "$WRAPPER" mode)" = "$EXPECTED_DEFAULT" ] \
  || fail "a fresh home must use the branch approval default"

payload() {
  printf '%s' '{"schema":"firstmate.handsfree-answer.v1","task_id":"task-1","answer":"approve the reviewed change","label":"Approve reviewed change","close_mode":"release"}'
}

set +e
out=$(payload | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER" 2>"$TMP_ROOT/default.err")
code=$?
set -e
if [ "$EXPECTED_DEFAULT" = prompt ]; then
  [ "$code" = 3 ] && [ -z "$out" ] || fail "main default must request confirmation"
else
  [ "$code" = 0 ] || fail "development default must omit extra confirmation"
fi
[ "$(FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" "$WRAPPER" mode)" = \
  "$EXPECTED_DEFAULT" ] || fail "the effective default mode should be visible"

cp "$ROOT/docs/examples/handsfree-approval.json" "$CONFIG_ROOT/handsfree-approval.json"
chmod 0600 "$CONFIG_ROOT/handsfree-approval.json"
[ "$(FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" "$WRAPPER" mode)" = \
  "prompt" ] || fail "the explicit prompt example should select prompt on either branch"

FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  "$WRAPPER" set-mode prompt >/dev/null
[ "$(FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" "$WRAPPER" mode)" = \
  "prompt" ] || fail "prompt mode should persist"
policy_mode=$(stat -f '%Lp' "$CONFIG_ROOT/handsfree-approval.json" 2>/dev/null \
  || stat -c '%a' "$CONFIG_ROOT/handsfree-approval.json")
[ "$policy_mode" = "600" ] || fail "the persisted approval mode must be owner-only 0600"

set +e
out=$(payload | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER" 2>"$TMP_ROOT/prompt.err")
code=$?
set -e
[ "$code" = 3 ] || fail "prompt mode should require confirmation"
[ -z "$out" ] || fail "prompt mode must emit no keyed answer before confirmation"

confirmed='{"schema":"firstmate.handsfree-answer.v1","task_id":"task-1","answer":"approve the reviewed change","label":"Approve reviewed change","close_mode":"release","confirmed":true}'
out=$(printf '%s' "$confirmed" | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER") || fail "confirmed prompt-mode answer should validate"
[ "$out" = $'task-1\tapprove the reviewed change\tApprove reviewed change\trelease' ] \
  || fail "confirmed prompt-mode answer emitted the wrong keyed row"

FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  "$WRAPPER" set-mode no_prompt >/dev/null
[ "$(FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" "$WRAPPER" mode)" = \
  "no_prompt" ] || fail "no_prompt mode should persist"
jq -e '.version == 1 and .mode == "no_prompt"' \
  "$CONFIG_ROOT/handsfree-approval.json" >/dev/null \
  || fail "set-mode no_prompt should write the supported persistent schema"
out=$(payload | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER") || fail "no_prompt should omit only the bridge confirmation"
[ "$out" = $'task-1\tapprove the reviewed change\tApprove reviewed change\trelease' ] \
  || fail "no_prompt emitted the wrong keyed row"

if command -v tasks-axi >/dev/null 2>&1; then
  CAPTAIN_HOME="$TMP_ROOT/captain-home"
  mkdir -p "$CAPTAIN_HOME/data" "$CAPTAIN_HOME/state" "$CAPTAIN_HOME/config"
  chmod 0700 "$CAPTAIN_HOME/state" "$CAPTAIN_HOME/config"
  FM_HOME="$CAPTAIN_HOME" FM_CONFIG_OVERRIDE="$CAPTAIN_HOME/config" \
    "$WRAPPER" set-mode no_prompt >/dev/null
  cp "$ROOT/.tasks.toml" "$CAPTAIN_HOME/.tasks.toml"
  cat > "$CAPTAIN_HOME/data/backlog.md" <<'EOF'
## In flight

## Queued

## Done
EOF
  fakebin=$(fm_fakebin "$CAPTAIN_HOME")
  fm_fake_exit0 "$fakebin" tmux treehouse no-mistakes gh gh-axi
  (cd "$CAPTAIN_HOME" && tasks-axi add bridge-task "Apply the reviewed change" \
    --kind ship --repo sample >/dev/null) \
    || fail "could not create the captain-held integration fixture"
  PATH="$fakebin:$PATH" REAL_TASKS_AXI="$(command -v tasks-axi)" \
    FM_ROOT_OVERRIDE="$ROOT" FM_HOME="$CAPTAIN_HOME" \
    FM_STATE_OVERRIDE="$CAPTAIN_HOME/state" FM_DATA_OVERRIDE="$CAPTAIN_HOME/data" \
    FM_CONFIG_OVERRIDE="$CAPTAIN_HOME/config" \
    "$ROOT/bin/fm-captain-hold.sh" hold bridge-task \
      --reason "captain approval needed" >/dev/null \
    || fail "could not hold the integration fixture for the captain"
  bridge_payload='{"schema":"firstmate.handsfree-answer.v1","task_id":"bridge-task","answer":"approve the reviewed change","label":"Approve reviewed change","close_mode":"release"}'
  printf '%s' "$bridge_payload" \
    | PATH="$fakebin:$PATH" REAL_TASKS_AXI="$(command -v tasks-axi)" \
      FM_ROOT_OVERRIDE="$ROOT" FM_HOME="$CAPTAIN_HOME" \
      FM_STATE_OVERRIDE="$CAPTAIN_HOME/state" FM_DATA_OVERRIDE="$CAPTAIN_HOME/data" \
      FM_CONFIG_OVERRIDE="$CAPTAIN_HOME/config" "$WRAPPER" >/dev/null \
    || fail "the built-in hands-free wrapper did not release the captain-held task"
  show=$(cd "$CAPTAIN_HOME" && tasks-axi show bridge-task --full) \
    || fail "could not read the released task"
  assert_contains "$show" "state: queued" \
    "a hands-free release should return held work to the queue"
  assert_contains "$show" "approve the reviewed change" \
    "the captain's captured words should be recorded by the existing answer owner"
fi

reconcile='{"schema":"firstmate.handsfree-answer.v1","task_id":"task-1","answer":"reconcile","label":"Reconcile","close_mode":"done"}'
set +e
printf '%s' "$reconcile" | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER" >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "hands-free input must not enter the reconcile path"

chmod 0644 "$CONFIG_ROOT/handsfree-approval.json"
set +e
payload | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER" >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "an unsafe policy file must stop the bridge"

bad_task='{"schema":"firstmate.handsfree-answer.v1","task_id":"../escape","answer":"approve","label":"Approve","close_mode":"done","confirmed":true}'
set +e
printf '%s' "$bad_task" | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER" >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "path-shaped task ids must be rejected"

duplicate='{"schema":"firstmate.handsfree-answer.v1","task_id":"task-1","task_id":"task-2","answer":"approve","label":"Approve","close_mode":"done"}'
set +e
printf '%s' "$duplicate" | FM_HOME="$HOME_ROOT" FM_CONFIG_OVERRIDE="$CONFIG_ROOT" \
  python3 "$PARSER" >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "duplicate JSON keys must be rejected"

pass "hands-free answers preserve prompt policy and emit only the existing keyed intake format"
