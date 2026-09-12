# Principal coordination profile

This profile records plan and implementation review rounds and checks them at worker launch and merge.
It is optional for standalone Firstmate homes and selected by the integration's primary setup.
Firstmate coordinates two independent reviewers and keeps the user in one conversation; the gate checks recorded evidence, not reviewer independence.

## One conversational interface

The user talks only to the Firstmate coordinator.
The coordinator owns intake, delegation, progress, clarification questions, user decisions, reconciliation, and the final result.
Workers and reviewers may run in separate isolated sessions, but they return evidence to the coordinator rather than contacting the user.
Optional worker inspection may exist, but ordinary work never requires it.

The user is the human principal and retains final authority over actions on their systems.
Firstmate is the coordinator and sole model reconciler.
The coding worker implements only the authorized scope.
Reviewer A and reviewer B must inspect independently, and the coding worker must not approve its own work.
These are workflow responsibilities; distinct aliases and the `firstmate-only` status label do not authenticate roles or bind a conversation.

## Select the profile

The primary integration setup in `fm-local-runtime.py` writes `principal-review` to the private profile configuration.
See [local integration setup](local-opencode.md) for that setup workflow.
Standalone homes can opt in by writing the same selection:

```text
principal-review
```

The path is `config/coordination-profile` under the selected `FM_HOME`, or under `FM_CONFIG_OVERRIDE` when supplied.
Writing this file directly also selects the profile.
An absent file or the exact value `standard` leaves the runtime gate inactive.
An empty, unknown, malformed, or unreadable selection refuses instead of falling back to another profile.
A conversational request or loading the skill alone does not enable the runtime hooks.

Approval prompting is a separate setting, owned by [`.firstmate-defaults.json`](../.firstmate-defaults.json) and the [hands-free approval policy](handsfree-approvals.md).
Public main defaults to `prompt`.
Keep a private `no_prompt` development variant identical except for that one tracked default value in `.firstmate-defaults.json`; do not duplicate the default in gate code or skills.
Neither approval mode enables, disables, or satisfies principal review.

## Record one lifecycle

The command header and help in [`bin/fm-principal-gate.py`](../bin/fm-principal-gate.py) own the CLI and state mechanics.
Prepare one complete intent and plan artifact, then initialize private state for the same task id the spawn and merge commands will use:

```sh
bin/fm-principal-gate.py init <task-id> \
  --intent-file <plan-file> \
  --reviewer-a <fresh-alias-a> \
  --reviewer-b <fresh-alias-b>
```

The coordinator records each review returned by its separately run reviewers:

```sh
bin/fm-principal-gate.py review <task-id> --stage pre \
  --reviewer <alias> --verdict <approved|changes-required> \
  --artifact <review-file>
```

The gate binds each submitted review to the current plan revision, and post-reviews also to the current implementation revision.
The coordinator must verify which revision a returned report actually reviewed before recording it; the CLI does not infer that from the report's content.
After both current pre-reviews approve, the coordinator records authorization:

```sh
bin/fm-principal-gate.py authorize <task-id> --artifact <reconciliation-file>
```

Authorization rehashes the current plan and review artifacts before recording its event.
The coding worker then implements in an isolated project copy through the normal `ship` task launch path.
Record the implementation evidence before post-review:

```sh
bin/fm-principal-gate.py implemented <task-id> --artifact <implementation-evidence>
```

Record both fresh post-implementation reviews with `--stage post`.
After both current post-reviews approve, the coordinator records acceptance, rechecking the plan, implementation, reviews, and authorization artifact:

```sh
bin/fm-principal-gate.py reconcile <task-id> --artifact <final-reconciliation>
```

At any point, the coordinator can show the current phase and next action:

```sh
bin/fm-principal-gate.py status <task-id>
```

`status` is read-only and reports revision numbers, current review summaries, and stale evidence.
Changed or missing current evidence reports `revision-required` and removes the affected stage's two reviews from the current summary, while retaining their historical records.
Use `require` for a blocking decision; a successful `status` command alone does not mean that work may proceed.

## Revise or reset a review round

Use versioned source artifact files and retain them while their evidence is current.
Changing a current source artifact does not silently approve a new version.
Start an explicit reset with `revise`, supplying the corrected artifact and a reason:

```sh
bin/fm-principal-gate.py revise <task-id> --stage plan \
  --artifact <revised-plan-file> --reason 'Update the reviewed scope'

bin/fm-principal-gate.py revise <task-id> --stage implementation \
  --artifact <revised-implementation-evidence> --reason 'Address post-review findings'
```

A plan revision invalidates both pre-reviews, authorization, implementation, post-reviews, and acceptance for the previous plan.
It starts a new pre-review round with implementation revision zero.
An implementation revision requires an already authorized, recorded implementation and unchanged current pre-review evidence.
It retains that plan's authorization, invalidates both post-reviews and acceptance, and requires fresh post-reviews before reconciliation.
Both operations preserve prior events and snapshots, work after acceptance, and can reset a round even when the supplied bytes are unchanged.

A later verdict from one reviewer can supersede its earlier verdict within an open round only while the current evidence remains unchanged.
If correcting the plan or implementation changes its artifact, reset the appropriate round instead of carrying the other reviewer's approval forward.
If a current review or coordinator report changes, use a plan reset for pre-review or authorization evidence, and an implementation reset for post-review or acceptance evidence.

Legacy `firstmate.principal-coordination.v1` records cannot satisfy `require` or acquire new approvals.
An explicit plan revision migrates them to a fresh v2 review round while retaining their old events.
It cannot recover source bytes that were never snapshotted by the older implementation.

## Runtime checks

The read-only gate command supports these checks:

```sh
bin/fm-principal-gate.py require <task-id> --phase authorized --kind ship
bin/fm-principal-gate.py require <task-id> --phase accepted
```

With `principal-review` selected, a `ship` task launch requires current plan authorization, and merge requires acceptance for the current plan and implementation.
Missing state, absent approvals, or changed required evidence refuses.
`require` creates no directories, state, locks, snapshots, or events, including when it fails.

[`bin/fm-spawn.sh`](../bin/fm-spawn.sh) checks fresh `ship` task launches during preflight, checks relaunches after resolving the recorded kind, and rechecks before submitting the launch command.
Planning and review tasks with kind `scout`, and coordinator launches with kind `secondmate`, do not require a `ship` task's approval record.
The profile is still parsed first, so an unknown profile refuses even for these exempt kinds.
[`bin/fm-pr-merge.sh`](../bin/fm-pr-merge.sh) checks acceptance before PR bookkeeping or forge calls and again before the GitHub or GitLab merge request.
[`bin/fm-merge-local.sh`](../bin/fm-merge-local.sh) checks before local merge preflight and again before the fast-forward.
Existing user approval holds, delivery-mode, merge-authority, and forge checks still apply.

## Cooperative role checks

A process carrying a non-empty `FM_TASK_ID` is refused by `review`, `authorize`, `reconcile`, and `revise`, including when it names another task.
Processes with task kind `ship` or `scout` receive this worker marker through spawn.
They can return evidence to Firstmate, which records approvals and resets from its coordinator process.
The marker does not prohibit `implemented`, `status`, or `require`.
It is not an authenticated role: a process running as the same operating-system user can remove the marker or change the private files.

## Evidence and privacy

Private v2 records under `state/principal-coordination/` store revision identities, event order, timestamps, reviewer aliases, source paths, byte counts, hashes, and snapshot paths.
`FM_STATE_OVERRIDE` selects a different state directory when supplied.
Submitted artifact bytes are copied into `artifacts/`, named by SHA-256, so replacing an original does not erase already captured evidence.
The gate and artifact directories are mode `0700`; task files and snapshots are mode `0600`.
Original source paths are rehashed for current checks; an unchanged archived copy does not make a changed original valid.
Keep these records private and exclude secrets and raw transcripts from submitted artifacts because their contents will be retained.

The coordinator must distinguish owner observations, extracted facts, inferences, disagreements, and open questions in the submitted artifacts; hashing does not classify their contents.
External research uses the configured DeepAPI research workflow.
Partial output is preserved and continued only for unfinished scope.

## Limits

This is cooperative same-user workflow enforcement, not authentication or operating-system containment.
The gate checks supplied artifacts and recorded verdicts, not actual Git contents, branch or PR heads, report truth, reviewer identity, or session independence.
Changing code without updating the submitted implementation evidence is outside what these hashes detect.
The coordinator must check that evidence describes the actual work and arrange separate reviewer sessions and sanitized packets.
No gate command launches a model, selects a provider, or sends a message; the single-conversation workflow remains the coordinator's responsibility.
The hooks release the gate lock before the subsequent action, so a check-to-action race remains despite the final rechecks.
Direct commands outside the three hooked entrypoints are not contained by this gate.
The profile does not grant project merge authority, destructive-action permission, or platform access, and does not change the selected delivery mode.

## Verification

[`tests/fm-principal-gate.test.sh`](../tests/fm-principal-gate.test.sh) exercises revisions, preserved evidence, hash changes, worker refusals, inactive and unknown profiles, read-only checks, spawn/merge refusals, a simulated terminal launch, and a local fast-forward in a disposable repository.
The broader [`PR merge`](../tests/fm-pr-merge.test.sh), [`batch spawn`](../tests/fm-spawn-batch.test.sh), and [`relaunch`](../tests/fm-control-relaunch.test.sh) regressions check existing entrypoint behavior with fake external tools.
These checks do not generate model output or prove live reviewer independence.
