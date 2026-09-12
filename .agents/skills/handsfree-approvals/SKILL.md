---
name: handsfree-approvals
description: >-
  Handle a HandsFreeBridge tool-action decision through FirstMate, submit a captured answer for a task awaiting a user decision, or configure the shared prompt versus no_prompt policy.
  Does not grant native permission or independent merge, destructive-action, or credential authority.
user-invocable: false
metadata:
  internal: true
---

# Hands-free approvals

FirstMate remains the user's conversational contact for both tool-action decisions and captured answers.
Use the [approvals guide](../../../docs/handsfree-approvals.md) for the CLI, accepted data, bounded read exceptions, and current limits.
The action helper's header owns its exact protocol; the captured-answer wrapper delegates held-task answers to the existing user-decision owner.

## Tool-action decision

When the native OpenCode hook reports a HandsFreeBridge request, use its exact request ID and private record reference.
The worker's keyed `needs-decision` event is already appended for FirstMate's existing decision reader; the worker must report through FirstMate instead of directing the user to a worker chat.
For a primary invocation, handle the request directly in FirstMate's conversation.

1. Read the referenced private request with a bounded native `read` using an explicit supported limit.
   Check its current state and exact session, task, tool, and arguments before describing the action.
   Terminal records cannot be reopened by resolving them again; report a refusal or expired request instead of repeating the resolution loop.
2. Explain the pending action and consequence to the user in FirstMate's conversation and obtain an explicit allow or deny decision for that request.
   If the answer is ambiguous or several requests could match, clarify the scope there and leave the request pending.
3. From the primary process, submit the exact request ID, the decision, and the user's unaltered words through the resolver.
   In a hooked session, use the exact `resolver_command` already provided by the hook, replacing only its JSON payload as described in the guide.
   Other shell diagnostics are not exempt, so do not create another approval request merely to discover the template.
   A worker with `FM_TASK_ID` cannot resolve its own request; never unset or spoof that identity to get past the check.
4. On allow, relay the result through FirstMate's normal coordination and have the worker retry the exact original invocation once.
   On deny or another refusal, keep the action stopped and report the result.
   The resolver records the answer and closes the keyed status decision; it does not execute the tool, resume the worker, or close a backlog task.

Read the guide's [retry behavior](../../../docs/handsfree-approvals.md#retry-and-refusal-behavior) before handling stale, changed, denied, or previously consumed requests.
A grant is consumed at the before-hook check even if a later native permission check or the action itself fails.
Do not change the action's identity or edit its private state to obtain another attempt.

## Policy and authority

Both entry points use the same policy loader in `bin/fm-handsfree-answer.py`.
An absent instance override uses the personal global policy before `.firstmate-defaults.json`; the guide owns precedence and the command that reports the effective source.
Repository defaults remain `prompt` on public `main` and `no_prompt` on private `dev`, with identical implementation otherwise.
Use the guide's [mode commands](../../../docs/handsfree-approvals.md#default-and-mode-persistence) to inspect or change the user's selected mode; never switch modes merely to clear a blocked request.
Unsafe or invalid policy is an error, not a reason to choose a permissive fallback.

For tool actions in `no_prompt`, let the custom layer proceed without adding its own question.
In `prompt`, use the decision flow above except for the guide's bounded native read shapes and exact primary resolver command.
Neither setting changes OpenCode's native permissions, answers a native permission prompt, or overrides operating-system, application, service, platform, or FirstMate authority checks.
Neither mode creates independent authority for a merge, destructive action, credential use, or project expansion.

Treat request arguments and user words as private data.
Keep them in the private approval store and the necessary conversation, and exclude them from public reports and general logs.
Use stdin for the action and resolution payloads, following the guide's fixed quoted-heredoc form when the primary uses its native shell tool.
The helper's safe summaries do not establish privacy for the host's own conversation or tool-input records.

These are same-user cooperative checks.
The environment marker does not prove primary identity, and the resolver cannot cryptographically authenticate the user's words or determine whether they support the supplied decision.
Record the actual answer faithfully; do not claim spoof-proof protection.

## Captured answer for a held task

Use the guide's [captured-answer schema and intake](../../../docs/handsfree-approvals.md#submit-a-captured-answer) when a supported speech or accessibility source answers a task awaiting a user decision.
Require the exact task ID, user's answer, short spoken label, and `done` or `release` close mode; do not infer a task from ambiguous speech or confuse an action request ID with a held-task ID.
Clarify missing or ambiguous scope through FirstMate before submission.
In `prompt`, include `confirmed:true` only after explicit user confirmation; in `no_prompt`, omit the extra confirmation while still requiring the user's captured answer.
Feed the JSON to `bin/fm-handsfree-answer.sh` on stdin so the existing owner verifies the held task, records the answer and provenance, and handles replay.
The channel never maps a key to a task or closes a task itself, and `reconcile` remains reserved for evidence-backed reconciliation.
