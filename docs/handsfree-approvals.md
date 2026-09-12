# Hands-free approvals

HandsFreeBridge connects OpenCode tool actions to FirstMate's approval conversation and also accepts captured answers for tasks awaiting a user decision.
FirstMate asks the user in its own conversation; the user never needs to open a worker or reviewer chat.
The action helper and the captured-answer intake share the `policy_mode()` implementation in [`bin/fm-handsfree-answer.py`](../bin/fm-handsfree-answer.py).

## Default and mode persistence

[`.firstmate-defaults.json`](../.firstmate-defaults.json) selects the default when no local override exists.
Public `main` uses `{"approval_mode":"prompt"}`.
The private GitHub companion's `dev` branch shares the same implementation, with only that file changed to `{"approval_mode":"no_prompt"}`.
There is no separate development hook or permission implementation.

In `prompt` mode, a non-exempt tool action waits for an explicit user decision through FirstMate.
In `no_prompt` mode, the custom action check allows a valid invocation without asking an extra question or creating a pending request.
For the separate captured-answer intake, `prompt` requires `"confirmed":true` after FirstMate obtains confirmation; `no_prompt` omits that extra confirmation but still requires the user's captured answer.

Show the current effective mode:

```sh
bin/fm-handsfree-answer.sh mode
```

Persist either choice:

```sh
bin/fm-handsfree-answer.sh set-mode no_prompt
bin/fm-handsfree-answer.sh set-mode prompt
```

The command writes `config/handsfree-approval.json` under the selected `FM_HOME`, or under `FM_CONFIG_OVERRIDE` when supplied.
An explicit local override takes precedence on either branch.
The file must be a regular owner-owned mode-`0600` file with one link and uses exactly `{"version":1,"mode":"no_prompt"}` or `{"version":1,"mode":"prompt"}`.
Malformed or unsafe configuration stops with an error rather than silently choosing a mode.

The tracked example at [`docs/examples/handsfree-approval.json`](examples/handsfree-approval.json) demonstrates an explicit `prompt` override on either branch.

## OpenCode action decisions

The native [`fm-handsfree-tool.js`](../.opencode/plugins/fm-handsfree-tool.js) plugin uses OpenCode's `tool.execute.before` hook.
It takes the tool and session ID from `input`, the actual invocation arguments from `output.args`, and the worker task ID from the launch environment's `FM_TASK_ID`.
It calls [`bin/fm-handsfree-tool.py`](../bin/fm-handsfree-tool.py) locally with JSON on standard input and throws to block a pending, refused, or invalid invocation.
It does not rewrite the action arguments or launch a model.

The local runtime includes the plugin's absolute file URI once in the per-launch OpenCode configuration's `plugin` array.
It sets `FM_ROOT_OVERRIDE` to FirstMate's code root and `FM_HOME` to the owning FirstMate home, while `FM_TASK_ID` identifies a worker when applicable.
The plugin is referenced directly, without copying it into the worker's project.
The plugin captures these values at initialization, so changing the surrounding environment later does not rebind an existing plugin instance.
The Python helper imports the policy owner from its own `bin` directory through `importlib`.

`python3 bin/fm-local-runtime.py launch --verify-only` checks that OpenCode's resolved configuration contains that hook URI exactly once, selects the requested provider/model, and preserves any explicitly requested reasoning effort.
It also verifies the isolated runtime paths and an empty session store, then reports `generation_started:false` without submitting a model prompt.
Configuration verification establishes the configured entry, not that the hook has executed successfully in a live tool call.
See [Local OpenCode setup](local-opencode.md) for instance setup and per-launch model routing.

For a non-exempt action in `prompt` mode:

1. The helper creates a random request ID and privately records the exact tool arguments, session ID, and worker task ID.
   Concurrent checks of the same pending action reuse the same request under a per-home lock.
2. For a worker, it appends a `needs-decision [key=handsfree-<request-id>]` event to `FM_HOME/state/<task-id>.status` for FirstMate's existing decision reader.
   The event contains the request reference and handling instructions, without the action arguments.
   The hook also reports the request ID and directs the worker to report through FirstMate.
3. FirstMate reads the referenced private request and explains the action and consequence to the user in its own conversation.
   It asks for an explicit allow or deny decision for that request and preserves the user's exact words.
4. FirstMate submits the resolution using the primary resolver described below.
   The resolver rejects a process with a nonempty `FM_TASK_ID`, records the answer privately, and appends a matching `resolved` event for a worker request.
5. An allow permits one identical retry by that worker.
   FirstMate communicates the result through its normal worker coordination; the resolver itself does not execute the action, steer the worker, or close a backlog task.

A primary action has an empty task ID and presents its request in the primary hook error instead of appending a worker status event.
The user-facing decision still stays in FirstMate's conversation.

## Action helper CLI

The script header owns the complete command and state contract.
These commands run from the FirstMate code root with the intended `FM_HOME` in the environment:

| Command | Input and result |
| --- | --- |
| `python3 bin/fm-handsfree-tool.py mode` | Prints the effective mode using the shared policy loader. |
| `python3 bin/fm-handsfree-tool.py check` | Reads one action JSON object from stdin; returns safe decision metadata and blocks pending or refused actions. |
| `python3 bin/fm-handsfree-tool.py pending` | Primary-only listing of pending request IDs, private file references, and resolver templates; does not print action arguments. |
| `python3 bin/fm-handsfree-tool.py resolve` | Primary-only resolution; reads one JSON object from stdin and records allow or deny with exact user words. |
| `python3 bin/fm-handsfree-tool.py resolver-command` | Prints the exact shell template for primary resolution through a hooked `bash` tool. |

`check` accepts `session_id`, `task_id`, `tool`, and an `args` object, plus an optional `request_id` for explicitly bound CLI retries.
`task_id` must equal the invoking process's `FM_TASK_ID`, or be empty when that environment value is absent or empty.
The native hook supplies this binding automatically and finds an approved retry by the unchanged action; it does not require an extra argument in the actual tool call.
Calling `check` can create a request or consume a grant, so it is not a preview command.

`resolve` accepts exactly this shape, using an actual pending ID and the user's actual words:

```json
{
  "request_id":"<pending-request-id>",
  "decision":"allow",
  "user_words":"Yes, run that exact action."
}
```

`decision` is `allow` or `deny`; `user_words` must be nonempty and is stored without trimming or paraphrasing.
The `confirmed` flag used by captured-answer intake is not a tool-action resolution.
The CLI returns exit code `0` for permission to proceed or command success, `3` for a pending decision, `4` for a denied, expired, or already used request, and `2` for invalid input, unknown IDs, mismatched bindings, worker resolution attempts, or unsafe state.

Inside a hooked primary session, use the exact `resolver_command` supplied in the hook error or safe request summary.
Replace only `{payload}` with the JSON resolution, preserving the quoted here-document delimiter and the fixed absolute helper command.
Do not add shell commands, prefixes, redirects, a different interpreter, or a `workdir` argument.
This narrowly matched primary resolver command is exempt from the custom prompt check; its own request validation still runs.
There is no substring whitelist or dedicated SDK tool dependency.

The other CLI commands above are terminal diagnostics and are not shell exemptions in a hooked primary session.
For an active decision, use the provided request reference and a bounded native `read` instead of issuing a shell command to discover the request or resolver template and creating another approval request.

## Bounded read exceptions

In `prompt` mode, the action helper also exempts only these native read tool shapes:

| Native tool | Accepted arguments |
| --- | --- |
| `read` | A nonempty `filePath` of at most 4096 characters and an explicit integer `limit` from 1 through 2000; optional integer `offset` must be nonnegative. |
| `glob` | Required `pattern` and optional `path`; supplied values must be nonempty strings of at most 4096 characters. |
| `grep` | Required `pattern`, optional `path` and `include`, with the same string bounds. |

Additional arguments remove the exemption.
The `glob` and `grep` result caps come from OpenCode's native tools, while `read` must supply its explicit limit.
A shell command such as `cat`, an MCP tool with a similar name, a network tool, or a tool that starts another task receives no read exemption.
These exceptions skip only the custom question and still pass through native permission checks.

## Retry and refusal behavior

An allow is consumed atomically in the before-hook check for the same session, task, tool, and complete arguments, including fields such as description and timeout.
JSON object key order does not affect the binding.
Changing any bound value cannot inherit the grant: an explicit request-ID mismatch is rejected, while a different action without an ID needs its own decision in `prompt` mode.
Concurrent retries can consume a grant only once.

Pending requests and unused grants expire 24 hours after request creation; approval does not restart that clock.
Denied, consumed, and expired records are retained, and retries of the same action remain refused in `prompt` mode.
Resolving a terminal record again does not reopen it, and unknown request IDs are rejected.
Do not keep resubmitting a terminal request, change its identity to evade a refusal, or edit approval files to revive it; report the condition through FirstMate.

Consumption happens before the native action executes.
A later native permission refusal or tool failure does not refund the grant, and there is no reset or reissue CLI for that same action binding.
The custom grant therefore confirms permission for one attempt, not proof that the action ran or succeeded.

## Private action data

Action state lives under `FM_HOME/state/handsfree-tools`, regardless of `FM_STATE_OVERRIDE`.
The helper creates its store directories with mode `0700` and request, index, and lock files with mode `0600`.
Request files retain the exact arguments, session and task identities, timestamps, decision state, and any captured resolution words.
These are private plaintext records, not encrypted storage.

The action helper's stdout and the hook's error messages expose decision metadata, request references, and resolver templates, without captured arguments or user words.
Worker status events likewise contain references rather than the action payload.
Keep full records and answers out of public files, issue bodies, logs, and copied command arguments; send action and resolution JSON through stdin.
FirstMate must read the private record to assess the action and show the user the relevant consequence without repeating credentials.
The bridge does not suppress OpenCode's own tool-input or conversation records, which may contain the original action arguments or the submitted resolution.

## Submit a captured answer

A supported speech or accessibility input writes one JSON object to standard input:

```json
{
  "schema":"firstmate.handsfree-answer.v1",
  "task_id":"task-123",
  "answer":"approve the reviewed change",
  "label":"Approve reviewed change",
  "close_mode":"release",
  "confirmed":true
}
```

In `prompt` mode, set `confirmed:true` only after FirstMate obtains the user's confirmation.
Feed a private capture file to the built-in intake:

```sh
bin/fm-handsfree-answer.sh < /path/to/private/captured-answer.json
```

The parser validates the task id, text bounds, close mode, policy, and reserved values.
It then emits one keyed row to `bin/fm-captain-hold.sh answers`.
That existing owner verifies the task is awaiting a user decision, records the exact answer and provenance, and handles replay.

The input channel never maps a key to a task, closes a task itself, or grants authority.
If more than one decision could match, FirstMate clarifies which task the user means in the same conversation before submitting an answer.
The user never has to answer in a worker or reviewer session.
This older intake intentionally passes an answer row to its existing owner; the action helper's metadata-only stdout guarantee does not apply to that answer row.

## Authority boundary

The approval mode is separate from the principal-review profile, Firstmate delivery modes, and merge authority.
`no_prompt` does not authorize unrequested work, project expansion, merges, destructive or irreversible actions, credential use, or security-sensitive choices.
It does not override an operating-system, application, harness, service, or platform permission.
The native action plugin neither changes OpenCode permission settings nor answers native permission prompts.

The `FM_TASK_ID` distinction, request bindings, and user-word recording are cooperative checks between processes owned by the same operating-system user.
They cannot cryptographically authenticate that the words came from the human, prove a process is the primary, or prevent deliberate same-user changes to the environment or private files.
The resolver validates the supplied decision and words but cannot establish that their meanings agree; FirstMate must faithfully convey the user's decision.
Request IDs and recorded words are not a spoof-proof identity or authorization system.

The existing voice relay does not yet produce the keyed JSON object by itself.
It can still read bounded status and queue work, while another supported capture source may call this intake.
Live microphone behavior and speech recognition remain subject to the limits in [`docs/voice-relay.md`](voice-relay.md).

## Verification

Run [`tests/fm-handsfree-tool.test.sh`](../tests/fm-handsfree-tool.test.sh) for the CLI, native before-hook, FirstMate decision reader, concurrency, private-file, and replay checks.
It invokes the companion JavaScript tests in bounded temporary homes without model or external calls.
Run [`tests/fm-handsfree-answer.test.sh`](../tests/fm-handsfree-answer.test.sh) for the separate captured-answer intake.
These local tests do not establish live microphone behavior or that a particular worker launch has loaded the plugin.
