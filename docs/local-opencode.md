# Local OpenCode setup

This distribution uses local OpenCode for the coordinator and workers.
The coordinator uses GPT-6 Astra with `xhigh` reasoning.
Planned worker slots 1 and 2 use Fable 5.1, and later slots use Astra with `xhigh` reasoning when the coordinator needs more workers.
These are roles within a task's plan, not a requirement to start idle workers.
The user talks only to the coordinator.

## Configure an instance

Install the dependencies in the main [setup guide](../README.md#quick-start), including OpenCode and a supported session backend.
Provider definitions and credentials must already be configured in OpenCode.
This repository contains neither credentials nor personal provider configuration.

Run `python3 bin/fm-local-runtime.py setup` from the checkout.
Setup checks the installed model catalog, saves exact routes in private `config/runtime-policy.json`, and selects the [principal-review profile](principal-coordination.md).
It never starts a model session or overwrites an existing conflicting choice.
If multiple providers offer the same model, use `setup --lead-model PROVIDER/MODEL --pair-model PROVIDER/MODEL` with the exact identifiers shown by `opencode models`.
Do not guess provider names from this document.
The helper's help text owns the command syntax.

Use `python3 bin/fm-local-runtime.py launch --verify-only` to check the resolved model, reasoning effort, approval-hook registration, and empty isolated session store without generating a response.
Use `python3 bin/fm-local-runtime.py launch` to start the ordinary coordinator session when ready.
Do not invoke OpenCode's plan or build agent directly for this workflow.

## Model selection

`config/runtime-policy.json` has three entries named `lead`, `worker_pair`, and `worker_extra`.
Each entry contains `runtime`, `model`, and `effort`.
The selected runtime is `opencode` unless the user explicitly chooses another runtime.
The local runtime helper never substitutes another model or a Codex subscription when a route is missing, ambiguous, or fails.
An explicit Codex selection is recorded but must be started deliberately through its own supported launch path.
Existing upstream adapters remain available for explicit user-requested switches.

The coordinator assigns stable one-based worker slots and uses `fm-spawn.sh --worker-slot N`.
Single-worker dispatch resolves and validates that slot's model before creating an endpoint.
A slot cannot be combined with a separate runtime, model, effort, secondary-coordinator, or batch selection.
Use explicit runtime and model flags only to carry out a user-requested switch.

## Session and approval boundaries

Each launch gets fresh data, cache, and state directories under the selected `FM_HOME/state/local-runtimes/`.
The helper verifies those paths and confirms there are no previous sessions before starting work.
It retains them for output recovery and leaves OpenCode's shared history untouched.
Global provider configuration remains in use, with `XDG_CONFIG_HOME` unset.
The action hook is registered in the per-launch configuration and uses the same selected `FM_HOME` as the coordinator.
It does not rewrite a project's OpenCode plugin files.

[Hands-free approvals](handsfree-approvals.md) owns the prompt behavior and its limits.
Public main defaults to `prompt`.
The development copy has identical implementation and tests, with only `.firstmate-defaults.json` changed to `no_prompt`.
A local approval override can take precedence over either default, so inspect the effective mode before use.
No-prompt mode removes this project's extra confirmation step; native permissions, user authorization, and platform controls still apply.

## Private GitHub development copy

GitHub visibility applies to a repository, not to individual branches or local worktrees.
A private development copy of a public fork therefore needs a separate private repository outside the public fork network.
Never push the private development branch to the public remote.
Keep provider settings, runtime state, transcripts, and research evidence in ignored local directories in both copies.
Private repository visibility does not make committed secrets safe.

## Verification

Run `tests/fm-local-runtime.test.sh`, `tests/fm-handsfree-tool.test.sh`, `tests/fm-handsfree-answer.test.sh`, and `tests/fm-principal-gate.test.sh` for deterministic checks.
Run the answer tests with `FM_TEST_EXPECT_DEFAULT=no_prompt` in the development copy.
The helper's `launch --verify-only` checks the installed OpenCode configuration without generation.
These checks do not prove a complete live multi-agent task or voice session.
