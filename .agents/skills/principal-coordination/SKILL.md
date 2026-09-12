---
name: principal-coordination
description: >-
  Run the principal-review coordination profile when the user explicitly requests the Pinchpoint or Fable workflow, or when config/coordination-profile selects principal-review.
  Coordinates separate reviewers behind one Firstmate conversation and uses the revision-aware artifact gate; reviewing or editing this implementation alone does not invoke the workflow.
user-invocable: false
metadata:
  internal: true
---

# Principal coordination

Use this profile only when the user requests it or the home selects `principal-review`.
It is optional for standalone homes; the primary integration setup in `fm-local-runtime.py` selects it by writing the private profile configuration.
For runtime enforcement, verify that the selected home's `config/coordination-profile` contains `principal-review`, respecting `FM_CONFIG_OVERRIDE`.
An absent file or `standard` leaves the gate inactive; an empty, unknown, or unreadable selection refuses.
Loading this skill alone does not activate the hooks.
Keep approval defaults in `.firstmate-defaults.json`: public main uses `prompt`, and a private `no_prompt` development variant must differ only in that tracked default value, with the same implementation.
Neither approval mode changes principal-review requirements.

## Authority and contact

The user is the human principal and retains final authority over actions on their systems.
Firstmate is the sole conversational contact, model orchestrator, and reconciler.
Workers and reviewers return bounded evidence to the coordinator and never require the user to open, track, or answer in their sessions.
The coordinator asks every needed question and returns progress, decisions, and the final outcome in the same user conversation.

The coordinator may reconcile evidence but cannot invent user authority.
The coding worker cannot approve its own implementation.
Reviewer A and reviewer B act independently and do not see one another's private reasoning or transcripts.
Use fresh task-scoped reviewer aliases and keep provider, model, credential, and routing details in private control records.
Aliases and the `firstmate-only` status label do not authenticate reviewers or enforce a conversation boundary.

## Gated lifecycle

`bin/fm-principal-gate.py` owns transition mechanics, revision identities, evidence snapshots, and the read-only `require` command.
Read [the operator reference](../../../docs/principal-coordination.md) for command examples, reset behavior, hook locations, and evidence retention; use the script's help for exact arguments.

1. Write one complete intent and plan artifact that states scope, non-goals, evidence, risks, verification, and rollback.
2. Initialize the task with distinct reviewer aliases.
3. Give independently prepared packets to reviewer A and reviewer B.
4. Check which plan revision each returned report reviewed, then record each pre-implementation review from the coordinator process.
5. Let the coordinator authorize implementation only after both current reviews approve.
6. Send the authorized scope to one bounded coding worker in an isolated project copy through the normal `ship` task launch path.
7. Record implementation evidence that describes the actual work, then obtain fresh independent post-implementation reviews of that plan and implementation revision.
8. Let the coordinator reconcile only after both current post-reviews approve.
9. Return the verified result to the user through the coordinator.

Use `changes-required` when a reviewer requests correction.
A later review from the same reviewer may supersede that verdict before its stage closes only while the current evidence remains unchanged.
Never forward raw model reasoning or another role's private transcript.

## Revisions and runtime enforcement

Use an explicit `revise` with a reason to reset a round after its subject or current supporting evidence changes; keep old events and snapshots.
A plan reset requires both pre-reviews and new authorization and clears the current implementation and acceptance.
An implementation reset retains valid plan authorization but requires both fresh post-reviews and new reconciliation.
Do not carry one reviewer's approval across a correction or record a delayed report against a revision it did not inspect.
Version source artifact files and retain current originals: authorization, reconciliation, and runtime checks rehash them.
Use a plan revision to migrate legacy v1 records; their old approvals do not count for a new round.

The `ship` task launch hook requires current plan authorization, including relaunches; planning and review tasks with kind `scout` and coordinator launches with kind `secondmate` are exempt from `ship` approval records.
Both PR and local merge hooks require current acceptance and recheck before the merge action.
Use the normal hooked entrypoints and retain all existing merge and user-authority checks.
`status` reports evidence and may return `revision-required`; use `require` when a blocking verdict is needed.

Processes with a non-empty `FM_TASK_ID` cannot call `review`, `authorize`, `reconcile`, or `revise`, even for another task.
Have marked workers and reviewers return artifacts to the coordinator for recording; do not ask them to remove the marker to bypass a refusal.
The marker allows implementation evidence submission and read-only gate queries.
These are cooperative same-user checks, not authenticated roles or operating-system containment.
The same user can unset the marker, edit state, or invoke unguarded commands, and a race remains between a successful check and the subsequent action.

The gate checks supplied artifacts, not the actual Git tree, branch/PR head, reviewer identity, session independence, or the truth of a report.
The coordinator must verify that reports concern the current work and keep reviewer sessions separate.
Do not present a passing gate as proof of these properties or as new owner authority.

## Evidence and recovery

Preserve owner observations, source facts, inferences, disagreements, and unresolved questions as distinct evidence types.
An owner-reported operational fact is evidence within its stated scope; static analysis may narrow what it proves but must not silently erase the observation.

External research uses the configured DeepAPI discovery and research workflow.
Preserve source URLs, request IDs, completeness, and uncertainty.

When a response ends partial, preserve the completed scope and start a fresh continuation for only the missing scope.
Do not duplicate completed paid work or replace a request that remains in flight merely because it is slow.
Keep complete packets intact and split them only after an actual receiver limit prevents delivery.

## Private boundary

Keep credentials, provider routes, runtime identifiers, prompt captures, raw transcripts, and unrelated account data outside model-visible packets and public artifacts.
The gate stores private event history and copies submitted artifact bytes into owner-only snapshots, including superseded revisions.
Do not submit secrets or raw transcripts as artifacts, and do not commit the state or snapshots.
