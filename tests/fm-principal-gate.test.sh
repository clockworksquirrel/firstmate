#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"

TMP_ROOT=$(fm_test_tmproot fm-principal-gate)
HOME_ROOT="$TMP_ROOT/home"
STATE_ROOT="$HOME_ROOT/state"
GATE="$ROOT/bin/fm-principal-gate.py"

mkdir -p "$STATE_ROOT"
chmod 0700 "$STATE_ROOT"
printf '%s\n' 'Implement the accepted feature without changing platform authority.' > "$TMP_ROOT/intent.md"
printf '%s\n' 'Reviewer A approves the plan.' > "$TMP_ROOT/pre-a.md"
printf '%s\n' 'Reviewer B requests one correction.' > "$TMP_ROOT/pre-b-change.md"
printf '%s\n' 'Reviewer B approves the corrected plan.' > "$TMP_ROOT/pre-b.md"
printf '%s\n' 'Coordinator authorizes the reviewed plan.' > "$TMP_ROOT/authorize.md"
printf '%s\n' 'Implementation and focused tests passed.' > "$TMP_ROOT/implemented.md"
printf '%s\n' 'Reviewer A approves the implementation.' > "$TMP_ROOT/post-a.md"
printf '%s\n' 'Reviewer B approves the implementation.' > "$TMP_ROOT/post-b.md"
printf '%s\n' 'Coordinator reconciles both reviews.' > "$TMP_ROOT/reconcile.md"

gate() {
  FM_HOME="$HOME_ROOT" FM_STATE_OVERRIDE="$STATE_ROOT" \
    FM_CONFIG_OVERRIDE="$HOME_ROOT/config" python3 "$GATE" "$@"
}

gate init task-1 --intent-file "$TMP_ROOT/intent.md" \
  --reviewer-a alpha-reviewer --reviewer-b beta-reviewer >/dev/null

status=$(gate status task-1)
[ "$(printf '%s' "$status" | jq -r '.phase')" = "pre-review" ] \
  || fail "a new task should wait for pre-implementation reviews"
[ "$(printf '%s' "$status" | jq -r '.captain_contact')" = "firstmate-only" ] \
  || fail "Firstmate should remain the only captain-facing contact"

set +e
gate authorize task-1 --artifact "$TMP_ROOT/authorize.md" >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "authorization should refuse before both reviewers approve"

gate review task-1 --stage pre --reviewer alpha-reviewer \
  --verdict approved --artifact "$TMP_ROOT/pre-a.md" >/dev/null
gate review task-1 --stage pre --reviewer beta-reviewer \
  --verdict changes-required --artifact "$TMP_ROOT/pre-b-change.md" >/dev/null

set +e
gate authorize task-1 --artifact "$TMP_ROOT/authorize.md" >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "a current changes-required verdict should block authorization"

gate review task-1 --stage pre --reviewer beta-reviewer \
  --verdict approved --artifact "$TMP_ROOT/pre-b.md" >/dev/null
gate authorize task-1 --artifact "$TMP_ROOT/authorize.md" >/dev/null

set +e
gate review task-1 --stage post --reviewer alpha-reviewer \
  --verdict approved --artifact "$TMP_ROOT/post-a.md" >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "post-review should refuse before implementation evidence"

gate implemented task-1 --artifact "$TMP_ROOT/implemented.md" >/dev/null
gate review task-1 --stage post --reviewer alpha-reviewer \
  --verdict approved --artifact "$TMP_ROOT/post-a.md" >/dev/null
gate review task-1 --stage post --reviewer beta-reviewer \
  --verdict approved --artifact "$TMP_ROOT/post-b.md" >/dev/null
gate reconcile task-1 --artifact "$TMP_ROOT/reconcile.md" >/dev/null

status=$(gate status task-1)
[ "$(printf '%s' "$status" | jq -r '.phase')" = "accepted" ] \
  || fail "both post-reviews and reconciliation should accept the task"
[ "$(printf '%s' "$status" | jq -r '.next_action')" = \
  "first mate returns the verified outcome to the captain" ] \
  || fail "the accepted outcome should return through Firstmate"
[ "$(printf '%s' "$status" | jq -r '.reviews.pre["reviewer-a"].verdict')" = "approved" ] \
  || fail "reviewer A's current pre-review should remain visible"
[ "$(printf '%s' "$status" | jq -r '.reviews.pre["reviewer-b"].verdict')" = "approved" ] \
  || fail "reviewer B's corrected pre-review should replace its old verdict"

mode=$(stat -f '%Lp' "$STATE_ROOT/principal-coordination/task-1.json" 2>/dev/null \
  || stat -c '%a' "$STATE_ROOT/principal-coordination/task-1.json")
[ "$mode" = "600" ] || fail "coordination state must be mode 0600"

set +e
gate init ../escape --intent-file "$TMP_ROOT/intent.md" \
  --reviewer-a alpha --reviewer-b beta >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "path-shaped task ids must be rejected"

set +e
gate init bad-alias --intent-file "$TMP_ROOT/intent.md" \
  --reviewer-a $'alpha\nreviewer' --reviewer-b beta >/dev/null 2>&1
code=$?
set -e
[ "$code" -ne 0 ] || fail "reviewer aliases containing controls must be rejected"

printf '%s\n' '{"schema":"firstmate.principal-coordination.v1","task_id":7,"reviewers":{"reviewer-a":{},"reviewer-b":"beta"},"events":[]}' \
  > "$STATE_ROOT/principal-coordination/bad-state.json"
chmod 0600 "$STATE_ROOT/principal-coordination/bad-state.json"
set +e
gate status bad-state >"$TMP_ROOT/bad-state.out" 2>"$TMP_ROOT/bad-state.err"
code=$?
set -e
[ "$code" -ne 0 ] || fail "malformed private state must be rejected"
assert_not_contains "$(cat "$TMP_ROOT/bad-state.err")" "Traceback" \
  "malformed private state should return a bounded error"

pass "principal state requires both configured review verdicts in transition order"

reject() {
  local message=$1 rc=0
  shift
  "$@" >"$TMP_ROOT/refused.out" 2>"$TMP_ROOT/refused.err" || rc=$?
  [ "$rc" -ne 0 ] || fail "expected refusal: $message"
  assert_contains "$(cat "$TMP_ROOT/refused.err" "$TMP_ROOT/refused.out")" "$message" \
    "wrong refusal for $*"
  assert_not_contains "$(cat "$TMP_ROOT/refused.err")" Traceback "refusal must be bounded"
}

approve_pair() {
  local task=$1 stage=$2
  gate review "$task" --stage "$stage" --reviewer alpha-reviewer \
    --verdict approved --artifact "$TMP_ROOT/$stage-a.md" >/dev/null
  gate review "$task" --stage "$stage" --reviewer beta-reviewer \
    --verdict approved --artifact "$TMP_ROOT/$stage-b.md" >/dev/null
}

init_task() {
  gate init "$1" --intent-file "$TMP_ROOT/intent.md" \
    --reviewer-a alpha-reviewer --reviewer-b beta-reviewer >/dev/null
}

authorize_task() {
  init_task "$1"
  approve_pair "$1" pre
  gate authorize "$1" --artifact "$TMP_ROOT/authorize.md" >/dev/null
}

# `require` must not seed directories, locks, or task state, including failures.
EMPTY_HOME="$TMP_ROOT/empty-home"
out=$(FM_HOME="$EMPTY_HOME" FM_STATE_OVERRIDE="$EMPTY_HOME/state" \
  FM_CONFIG_OVERRIDE="$EMPTY_HOME/config" python3 "$GATE" require missing --phase authorized)
assert_contains "$out" '"active": false' "absent profile must be inactive"
[ ! -e "$EMPTY_HOME" ] || fail "inactive require created a home"
mkdir -p "$HOME_ROOT/config"
printf 'standard\n' > "$HOME_ROOT/config/coordination-profile"
gate require missing --phase accepted >/dev/null
printf 'unknown\n' > "$HOME_ROOT/config/coordination-profile"
reject "unknown coordination profile" gate require task-1 --phase authorized
printf '\n' > "$HOME_ROOT/config/coordination-profile"
reject "unknown coordination profile" gate require task-1 --phase accepted
printf 'principal-review\n' > "$HOME_ROOT/config/coordination-profile"
reject "does not exist" gate require missing --phase authorized
gate require missing --phase authorized --kind scout >/dev/null
gate require missing --phase authorized --kind secondmate >/dev/null
reject "unavailable" env FM_HOME="$EMPTY_HOME" FM_STATE_OVERRIDE="$EMPTY_HOME/state" \
  FM_CONFIG_OVERRIDE="$HOME_ROOT/config" python3 "$GATE" require missing --phase authorized
[ ! -e "$EMPTY_HOME" ] || fail "failing require created a home"
before=$(cat "$STATE_ROOT/principal-coordination/task-1.json")
gate require task-1 --phase accepted >/dev/null
[ "$before" = "$(cat "$STATE_ROOT/principal-coordination/task-1.json")" ] \
  || fail "require rewrote task evidence"
mv "$STATE_ROOT/principal-coordination/.lock" "$TMP_ROOT/saved-lock"
reject "unavailable" gate require task-1 --phase accepted
[ ! -e "$STATE_ROOT/principal-coordination/.lock" ] || fail "require created its lock"
mv "$TMP_ROOT/saved-lock" "$STATE_ROOT/principal-coordination/.lock"
pass "require honors explicit profiles, exempts scouts, and never creates gate state"

init_task worker-check
before=$(cat "$STATE_ROOT/principal-coordination/worker-check.json")
for worker in worker-check another-worker; do
  FM_TASK_ID="$worker" reject "worker-marked FM_TASK_ID" gate review worker-check \
    --stage pre --reviewer alpha-reviewer --verdict approved --artifact "$TMP_ROOT/pre-a.md"
  FM_TASK_ID="$worker" reject "worker-marked FM_TASK_ID" gate authorize worker-check \
    --artifact "$TMP_ROOT/authorize.md"
  FM_TASK_ID="$worker" reject "worker-marked FM_TASK_ID" gate reconcile task-1 \
    --artifact "$TMP_ROOT/reconcile.md"
  FM_TASK_ID="$worker" reject "worker-marked FM_TASK_ID" gate revise worker-check \
    --stage plan --artifact "$TMP_ROOT/intent.md" --reason correction
done
[ "$before" = "$(cat "$STATE_ROOT/principal-coordination/worker-check.json")" ] \
  || fail "worker refusal changed state"
FM_TASK_ID=worker-check gate require task-1 --phase accepted >/dev/null
pass "worker markers refuse approval mutations, including another task, but permit reads"

# Corrections invalidate BOTH reviewers. Revisions retain events and actual bytes.
cp "$TMP_ROOT/intent.md" "$TMP_ROOT/revision-plan.md"
cp "$TMP_ROOT/implemented.md" "$TMP_ROOT/revision-implementation.md"
gate init revised --intent-file "$TMP_ROOT/revision-plan.md" \
  --reviewer-a alpha-reviewer --reviewer-b beta-reviewer >/dev/null
approve_pair revised pre
plan_snapshot=$(jq -r '.intent.snapshot' "$STATE_ROOT/principal-coordination/revised.json")
printf 'Changed plan after both reviews\n' > "$TMP_ROOT/revision-plan.md"
reject "evidence changed" gate authorize revised --artifact "$TMP_ROOT/authorize.md"
out=$(gate status revised)
[ "$(printf '%s' "$out" | jq -r .phase)" = revision-required ] || fail "stale plan looked current"
printf '%s' "$out" | jq -e '.reviews.pre == {"reviewer-a":null,"reviewer-b":null}' >/dev/null \
  || fail "changed plan retained a current review"
cmp "$plan_snapshot" "$TMP_ROOT/intent.md" || fail "original plan bytes were not preserved"
events_before=$(jq -c .events "$STATE_ROOT/principal-coordination/revised.json")
gate revise revised --stage plan --artifact "$TMP_ROOT/revision-plan.md" --reason 'corrected plan' >/dev/null
[ "$(jq -c '.events[:-1]' "$STATE_ROOT/principal-coordination/revised.json")" = "$events_before" ] \
  || fail "plan reset rewrote historical events"
gate review revised --stage pre --reviewer beta-reviewer \
  --verdict approved --artifact "$TMP_ROOT/pre-b.md" >/dev/null
reject "both current pre" gate authorize revised --artifact "$TMP_ROOT/authorize.md"
gate review revised --stage pre --reviewer alpha-reviewer \
  --verdict approved --artifact "$TMP_ROOT/pre-a.md" >/dev/null
gate authorize revised --artifact "$TMP_ROOT/authorize.md" >/dev/null
FM_TASK_ID=revised gate implemented revised --artifact "$TMP_ROOT/revision-implementation.md" >/dev/null
approve_pair revised post
implementation_snapshot=$(jq -r '.implementation.snapshot' "$STATE_ROOT/principal-coordination/revised.json")
printf 'Corrected implementation\n' > "$TMP_ROOT/revision-implementation.md"
reject "evidence changed" gate reconcile revised --artifact "$TMP_ROOT/reconcile.md"
gate status revised | jq -e '.reviews.post == {"reviewer-a":null,"reviewer-b":null}' >/dev/null \
  || fail "changed implementation retained a current post-review"
cmp "$implementation_snapshot" "$TMP_ROOT/implemented.md" || fail "original implementation bytes lost"
gate revise revised --stage implementation --artifact "$TMP_ROOT/revision-implementation.md" \
  --reason 'fix post-review findings' >/dev/null
gate require revised --phase authorized >/dev/null
gate review revised --stage post --reviewer beta-reviewer \
  --verdict approved --artifact "$TMP_ROOT/post-b.md" >/dev/null
reject "both current post" gate reconcile revised --artifact "$TMP_ROOT/reconcile.md"
gate review revised --stage post --reviewer alpha-reviewer \
  --verdict approved --artifact "$TMP_ROOT/post-a.md" >/dev/null
gate reconcile revised --artifact "$TMP_ROOT/reconcile.md" >/dev/null
gate require revised --phase accepted >/dev/null
gate status revised | jq -e '.plan_revision == 2 and .implementation_revision == 2' >/dev/null \
  || fail "revisions were not advanced independently"
printf 'Changed after acceptance\n' >> "$TMP_ROOT/revision-implementation.md"
reject "evidence changed" gate require revised --phase accepted
gate revise revised --stage implementation --artifact "$TMP_ROOT/revision-implementation.md" \
  --reason 'new correction after acceptance' >/dev/null
reject "requires accepted" gate require revised --phase accepted
gate revise revised --stage plan --artifact "$TMP_ROOT/revision-plan.md" --reason 'explicit reset' >/dev/null
reject "requires authorized" gate require revised --phase authorized
gate status revised | jq -e '.implementation_revision == 0 and .phase == "pre-review"' >/dev/null \
  || fail "plan reset retained implementation or authorization"
pass "changed artifacts invalidate both reviewers and explicit resets preserve old evidence"

# Review and coordinator reports are also rehashed, not just the two subjects.
init_task changed-report
cp "$TMP_ROOT/pre-a.md" "$TMP_ROOT/changing-review.md"
gate review changed-report --stage pre --reviewer alpha-reviewer \
  --verdict approved --artifact "$TMP_ROOT/changing-review.md" >/dev/null
gate review changed-report --stage pre --reviewer beta-reviewer \
  --verdict approved --artifact "$TMP_ROOT/pre-b.md" >/dev/null
printf 'Now requests changes\n' > "$TMP_ROOT/changing-review.md"
reject "evidence changed" gate authorize changed-report --artifact "$TMP_ROOT/authorize.md"
authorize_task changed-post-report
gate implemented changed-post-report --artifact "$TMP_ROOT/implemented.md" >/dev/null
cp "$TMP_ROOT/post-a.md" "$TMP_ROOT/changing-post-review.md"
gate review changed-post-report --stage post --reviewer alpha-reviewer \
  --verdict approved --artifact "$TMP_ROOT/changing-post-review.md" >/dev/null
gate review changed-post-report --stage post --reviewer beta-reviewer \
  --verdict approved --artifact "$TMP_ROOT/post-b.md" >/dev/null
printf 'New post-review concern\n' >> "$TMP_ROOT/changing-post-review.md"
reject "evidence changed" gate reconcile changed-post-report --artifact "$TMP_ROOT/reconcile.md"
pass "authorization and reconciliation recheck the review artifacts themselves"

# Existing unversioned approvals are never silently promoted to v2 authority.
printf '%s\n' '{"schema":"firstmate.principal-coordination.v1","task_id":"legacy","reviewers":{"reviewer-a":"alpha-reviewer","reviewer-b":"beta-reviewer"},"events":[{"sequence":1,"actor":"coordinator","kind":"accepted"}]}' \
  > "$STATE_ROOT/principal-coordination/legacy.json"
chmod 0600 "$STATE_ROOT/principal-coordination/legacy.json"
reject "legacy evidence" gate require legacy --phase accepted
gate revise legacy --stage plan --artifact "$TMP_ROOT/intent.md" --reason 'migrate old record' >/dev/null
reject "requires authorized" gate require legacy --phase authorized
jq -e '.events[0].kind == "accepted" and .plan_revision == 1' \
  "$STATE_ROOT/principal-coordination/legacy.json" >/dev/null || fail "legacy evidence lost"
pass "legacy approval records require an explicit new plan review round"

# Black-box hook checks invoke the real entrypoints. An impossible backend or
# missing task metadata stops successful gate checks before any tool can launch.
run_spawn_hook() {
  FM_HOME="$HOME_ROOT" FM_STATE_OVERRIDE="$STATE_ROOT" FM_CONFIG_OVERRIDE="$HOME_ROOT/config" \
    FM_ROOT_OVERRIDE="$ROOT" FM_SPAWN_NO_GUARD=1 \
    bash "$ROOT/bin/fm-spawn.sh" "$@" --backend principal-test-invalid 2>&1
}
init_task pending-ship
reject "requires authorized" run_spawn_hook pending-ship nowhere --mode local-only --yolo off
authorize_task approved-ship
reject "backend" run_spawn_hook approved-ship nowhere --mode local-only --yolo off
assert_not_contains "$(cat "$TMP_ROOT/refused.out")" principal-review "authorized ship did not pass its gate"
reject "backend" run_spawn_hook review-scout nowhere --scout
assert_not_contains "$(cat "$TMP_ROOT/refused.out")" principal-review "scout was blocked by the principal gate"

run_merge_hook() {
  local script=$1
  shift
  FM_HOME="$HOME_ROOT" FM_STATE_OVERRIDE="$STATE_ROOT" FM_CONFIG_OVERRIDE="$HOME_ROOT/config" \
    FM_ROOT_OVERRIDE="$ROOT" bash "$ROOT/bin/$script" "$@" 2>&1
}
reject "requires accepted" run_merge_hook fm-pr-merge.sh approved-ship https://github.com/example/repo/pull/1
reject "requires accepted" run_merge_hook fm-pr-merge.sh approved-ship https://gitlab.example/group/repo/-/merge_requests/1
reject "requires accepted" run_merge_hook fm-merge-local.sh approved-ship
# PR acceptance advances to its local metadata check without contacting a forge.
reject "task metadata is unavailable" run_merge_hook fm-pr-merge.sh task-1 https://github.com/example/repo/pull/1
printf 'standard\n' > "$HOME_ROOT/config/coordination-profile"
reject "task metadata is unavailable" run_merge_hook fm-pr-merge.sh pending-ship https://github.com/example/repo/pull/1
printf 'unknown\n' > "$HOME_ROOT/config/coordination-profile"
reject "unknown coordination profile" run_spawn_hook review-scout nowhere --scout
reject "unknown coordination profile" run_merge_hook fm-pr-merge.sh task-1 https://github.com/example/repo/pull/1
reject "unknown coordination profile" run_merge_hook fm-merge-local.sh task-1
printf 'principal-review\n' > "$HOME_ROOT/config/coordination-profile"
pass "real spawn and both merge entrypoints enforce the gate before their next preflight"

# Complete a local fast-forward in a disposable repository after acceptance.
# No forge, terminal, or model runs; tmux and external commands are stubs.
fm_git_worktree "$TMP_ROOT/local-project" "$TMP_ROOT/local-worktree" fm/local-gated
git -C "$TMP_ROOT/local-worktree" -c user.name=Test -c user.email=test@example.invalid \
  commit --allow-empty -qm 'Local gate fixture'
fm_write_meta "$STATE_ROOT/local-gated.meta" "project=$TMP_ROOT/local-project" \
  "worktree=$TMP_ROOT/local-worktree" "window=fm-local-gated" \
  "kind=ship" "mode=local-only" "harness=codex"
fakebin=$(fm_test_make_spawn_fakebin "$TMP_ROOT/fake-tools" gh gh-axi glab ssh curl no-mistakes)
mkdir -p "$TMP_ROOT/guard-root/bin" "$HOME_ROOT/data"
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP_ROOT/guard-root/bin/fm-guard.sh"
chmod +x "$TMP_ROOT/guard-root/bin/fm-guard.sh"
run_local_merge() {
  FM_HOME="$HOME_ROOT" FM_STATE_OVERRIDE="$STATE_ROOT" FM_CONFIG_OVERRIDE="$HOME_ROOT/config" \
    FM_DATA_OVERRIDE="$HOME_ROOT/data" FM_ROOT_OVERRIDE="$TMP_ROOT/guard-root" \
    PATH="$fakebin:$PATH" bash "$ROOT/bin/fm-merge-local.sh" local-gated
}
authorize_task local-gated
local_before=$(git -C "$TMP_ROOT/local-project" rev-parse HEAD)
reject "requires accepted" run_local_merge
[ "$local_before" = "$(git -C "$TMP_ROOT/local-project" rev-parse HEAD)" ] \
  || fail "unaccepted merge changed the branch"
cp "$TMP_ROOT/implemented.md" "$TMP_ROOT/local-implementation.md"
gate implemented local-gated --artifact "$TMP_ROOT/local-implementation.md" >/dev/null
approve_pair local-gated post
gate reconcile local-gated --artifact "$TMP_ROOT/reconcile.md" >/dev/null
# Change evidence during the local Git preflight, after the first require call.
# The final require must catch it before git merge runs.
REAL_GIT=$(command -v git)
export REAL_GIT
cat > "$fakebin/git" <<'SH'
#!/usr/bin/env bash
if [ "${3:-}" = rev-parse ] && [ "${4:-}" = --short ] && [ -n "${FM_TEST_MUTATE_ARTIFACT:-}" ]; then
  printf 'Changed during merge preflight\n' >> "$FM_TEST_MUTATE_ARTIFACT"
fi
exec "$REAL_GIT" "$@"
SH
chmod +x "$fakebin/git"
FM_TEST_MUTATE_ARTIFACT="$TMP_ROOT/local-implementation.md" reject "evidence changed" run_local_merge
[ "$local_before" = "$(git -C "$TMP_ROOT/local-project" rev-parse HEAD)" ] \
  || fail "the final gate let stale evidence move the branch"
gate revise local-gated --stage implementation --artifact "$TMP_ROOT/local-implementation.md" \
  --reason 'review the changed preflight evidence' >/dev/null
approve_pair local-gated post
gate reconcile local-gated --artifact "$TMP_ROOT/reconcile.md" >/dev/null
run_local_merge >/dev/null
[ "$(git -C "$TMP_ROOT/local-project" rev-parse HEAD)" = \
  "$(git -C "$TMP_ROOT/local-worktree" rev-parse HEAD)" ] || fail "accepted local merge did not land"
pass "local merge leaves the branch untouched until both reviews and reconciliation pass"

# Complete a real spawn entrypoint using a terminal stub that records commands
# without executing them. No model or provider is started by this fixture.
fm_test_spawn_home "$HOME_ROOT" claude
fm_test_spawn_brief "$HOME_ROOT" approved-ship
fm_git_worktree "$TMP_ROOT/spawn-project" "$TMP_ROOT/spawn-worktree" wt-approved-ship
fm_fake_exit0 "$fakebin" claude
out=$(FM_FAKE_LAUNCH_LOG="$TMP_ROOT/launch.log" \
  fm_test_run_spawn "$HOME_ROOT" "$TMP_ROOT/spawn-worktree" "$fakebin" \
    approved-ship "$TMP_ROOT/spawn-project" --backend tmux --mode local-only --yolo off) \
  || fail "authorized ship did not reach the simulated launch: $out"
assert_contains "$out" 'spawned approved-ship' "spawn did not finish after authorization"
[ -s "$TMP_ROOT/launch.log" ] || fail "simulated terminal received no launch command"
pass "authorized ship reaches a simulated terminal launch without running a model"
