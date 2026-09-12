#!/usr/bin/env python3
"""Select exact local runtimes without automatic model or subscription fallback.

Usage:
  fm-local-runtime.py status
  fm-local-runtime.py select lead|worker --slot N [--catalog FILE]
  fm-local-runtime.py check lead|worker --slot N
  fm-local-runtime.py resolve PROVIDER/MODEL
  fm-local-runtime.py setup [--lead-model PROVIDER/MODEL] [--pair-model PROVIDER/MODEL]
  fm-local-runtime.py launch --model PROVIDER/MODEL [--prompt TEXT]

config/runtime-policy.json may explicitly pin lead, worker_pair, worker_extra.
Each entry has runtime (opencode or codex), model, and effort.
Omission uses OpenCode and the requested model family; ambiguous catalog matches
require a saved exact route. This tool never edits global provider configuration.
The launch command starts an ordinary interactive OpenCode agent with isolated
data/cache/state and the existing global provider configuration.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

DEFAULTS = {
    "lead": {"runtime": "opencode", "model": "gpt-6-astra", "effort": "xhigh"},
    "worker_pair": {"runtime": "opencode", "model": "claude-fable-5-1", "effort": ""},
    "worker_extra": {"runtime": "opencode", "model": "gpt-6-astra", "effort": "xhigh"},
}
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")


class RouteError(ValueError):
    pass


def home_path():
    return Path(os.environ.get("FM_HOME") or Path(__file__).resolve().parent.parent)


def policy():
    path = Path(os.environ.get("FM_CONFIG_OVERRIDE") or home_path() / "config") / "runtime-policy.json"
    if not path.exists():
        return {key: dict(value) for key, value in DEFAULTS.items()}
    if path.is_symlink():
        raise RouteError("runtime-policy.json must be a regular local file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RouteError("runtime-policy.json is unreadable or invalid") from error
    if not isinstance(data, dict) or set(data) != set(DEFAULTS):
        raise RouteError("runtime policy requires lead, worker_pair, and worker_extra")
    for entry in data.values():
        if not isinstance(entry, dict) or set(entry) != {"runtime", "model", "effort"}:
            raise RouteError("each route requires exactly runtime, model, and effort")
        if entry["runtime"] not in ("opencode", "codex"):
            raise RouteError("unsupported runtime; no automatic fallback")
        if not isinstance(entry["model"], str) or not MODEL.fullmatch(entry["model"]):
            raise RouteError("invalid model identifier")
        if entry["effort"] not in ("", "low", "medium", "high", "xhigh", "max", "ultra"):
            raise RouteError("invalid reasoning effort")
    return data


def select(role, slot):
    if role == "lead":
        return dict(policy()["lead"])
    if type(slot) is not int or slot < 1:
        raise RouteError("worker selection requires a positive one-based --slot")
    return dict(policy()["worker_pair" if slot <= 2 else "worker_extra"])


def resolve_model(requested, catalog):
    entries = set(catalog.splitlines())
    if requested in entries:
        return requested
    matches = sorted(item for item in entries if item.endswith("/" + requested))
    if len(matches) != 1:
        raise RouteError("requested model is absent or ambiguous in OpenCode; save an exact route, no fallback")
    return matches[0]


def catalog_from_opencode():
    executable = shutil.which("opencode")
    if not executable:
        raise RouteError("local OpenCode is unavailable; no subscription fallback")
    result = subprocess.run([executable, "models"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RouteError("OpenCode model discovery failed; no fallback")
    return result.stdout


def launch(model, prompt, effort="", verify_only=False):
    exact = resolve_model(model, catalog_from_opencode())
    if "/" not in exact:
        raise RouteError("OpenCode requires an exact provider/model")
    # Every launch starts fresh. Shared history and XDG_CONFIG_HOME stay untouched.
    base = home_path() / "state" / "local-runtimes"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    attempt = Path(tempfile.mkdtemp(prefix="opencode-", dir=base))
    env = dict(os.environ)
    code_root = Path(__file__).resolve().parent.parent
    env["FM_HOME"] = str(home_path().resolve())
    env["FM_ROOT_OVERRIDE"] = str(code_root)
    for name, directory in (("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        env[name] = str(attempt / directory)
    env.pop("XDG_CONFIG_HOME", None)
    # Match the existing provider model-options contract; use a named ordinary
    # primary agent instead of selecting OpenCode's plan or build agent.
    hook = (code_root / ".opencode/plugins/fm-handsfree-tool.js").as_uri()
    config = {"default_agent": "firstmate", "plugin": [hook], "agent": {
        "firstmate": {"mode": "primary", "model": exact,
                      "prompt": "Follow the supplied task and repository instructions. Report to First Mate when assigned as a worker."}}}
    if effort:
        provider, model_id = exact.split("/", 1)
        config["provider"] = {provider: {"models": {model_id: {"options": {"reasoningEffort": effort}}}}}
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    binary = shutil.which("opencode")
    paths = subprocess.run([binary, "debug", "paths"], env=env, capture_output=True, text=True, check=False)
    if paths.returncode or any(str(attempt / item) not in paths.stdout for item in ("data", "cache", "state")):
        raise RouteError("OpenCode isolated runtime paths did not verify")
    sessions = subprocess.run([binary, "session", "list", "--format", "json"],
                              env=env, capture_output=True, text=True, check=False)
    try:
        empty = json.loads(sessions.stdout) == []
    except ValueError:
        empty = False
        if sessions.returncode == 0 and not sessions.stdout.strip():
            database = attempt / "data" / "opencode" / "opencode.db"
            try:
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                    empty = connection.execute('SELECT COUNT(*) FROM session').fetchone()[0] == 0
            except sqlite3.Error:
                empty = False
    if sessions.returncode or not empty:
        raise RouteError("fresh OpenCode runtime session list is not empty")
    if verify_only:
        resolved = subprocess.run([binary, "debug", "config"], env=env,
                                  capture_output=True, text=True, check=False)
        try:
            settings = json.loads(resolved.stdout)
        except ValueError as error:
            raise RouteError("OpenCode resolved configuration could not be verified") from error
        if resolved.returncode or settings.get("default_agent") != "firstmate":
            raise RouteError("OpenCode did not select the ordinary firstmate agent")
        actual = settings.get("agent", {}).get("firstmate", {})
        if actual.get("model") != exact:
            raise RouteError("OpenCode model selection differs from the requested route")
        if effort:
            provider, model_id = exact.split("/", 1)
            actual_effort = settings.get("provider", {}).get(provider, {}).get("models", {}).get(model_id, {}).get("options", {}).get("reasoningEffort")
            if actual_effort != effort:
                raise RouteError("OpenCode reasoning effort differs from the requested setting")
        if settings.get("plugin", []).count(hook) != 1:
            raise RouteError("OpenCode action-approval hook must be loaded exactly once")
        print(json.dumps({"runtime": "opencode", "model": exact, "effort": effort,
                          "action_hook_configured": True,
                          "isolated_paths_verified": True, "empty_sessions": True,
                          "generation_started": False}))
        return 0
    command = [binary, "--agent", "firstmate", "--model", exact]
    if prompt:
        command += ["--prompt", prompt]
    return subprocess.call(command, env=env)


def setup(lead_model, pair_model):
    """Create a private, explicit profile; never overwrite an existing choice."""
    if os.environ.get("FM_TASK_ID"):
        raise RouteError("only the primary may configure an instance")
    routes = policy()
    if any(route["runtime"] != "opencode" for route in routes.values()):
        raise RouteError("an explicit alternative is configured; setup will not switch it")
    if lead_model:
        routes["lead"]["model"] = lead_model
        routes["worker_extra"]["model"] = lead_model
    if pair_model:
        routes["worker_pair"]["model"] = pair_model
    catalog = catalog_from_opencode()
    for route in routes.values():
        route["model"] = resolve_model(route["model"], catalog)
    config = Path(os.environ.get("FM_CONFIG_OVERRIDE") or home_path() / "config")
    config.mkdir(parents=True, exist_ok=True, mode=0o700)
    if config.is_symlink() or config.stat().st_uid != os.getuid() or config.stat().st_mode & 0o022:
        raise RouteError("configuration directory must be owner-controlled")
    files = {"runtime-policy.json": json.dumps(routes, indent=2) + "\n",
             "coordination-profile": "principal-review\n"}
    for name, value in files.items():
        path = config / name
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_text() != value:
                raise RouteError("setup preserves existing settings; review " + name + " explicitly")
    for name, value in files.items():
        path = config / name
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
    print(json.dumps({"configured": True, "profile": "principal-review", "routes": routes,
                      "generation_started": False}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    cmd = commands.add_parser("resolve")
    cmd.add_argument("model")
    cmd = commands.add_parser("setup")
    cmd.add_argument("--lead-model")
    cmd.add_argument("--pair-model")
    for verb in ("select", "check"):
        cmd = commands.add_parser(verb)
        cmd.add_argument("role", choices=("lead", "worker"))
        cmd.add_argument("--slot", type=int)
        cmd.add_argument("--catalog")
    cmd = commands.add_parser("launch")
    cmd.add_argument("--model")
    cmd.add_argument("--effort", choices=("", "low", "medium", "high", "xhigh", "max", "ultra"), default=None)
    cmd.add_argument("--prompt", default="")
    cmd.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "status":
            print(json.dumps({"routes": policy(), "automatic_fallback": False}, indent=2))
            return 0
        if args.command == "resolve":
            print(resolve_model(args.model, catalog_from_opencode()))
            return 0
        if args.command == "setup":
            return setup(args.lead_model, args.pair_model)
        if args.command == "launch":
            lead = select("lead", None)
            if not args.model and lead["runtime"] != "opencode":
                raise RouteError("explicit alternative selected; start that runtime deliberately")
            effort = args.effort
            if effort is None:
                effort = lead["effort"] if not args.model or args.model.endswith("gpt-6-astra") else ""
            return launch(args.model or lead["model"], args.prompt, effort, args.verify_only)
        route = select(args.role, args.slot)
        if args.command == "check" or args.catalog:
            if route["runtime"] != "opencode":
                raise RouteError("Codex subscription is an explicit alternative; not probed or launched automatically")
            catalog = Path(args.catalog).read_text(encoding="utf-8") if args.catalog else catalog_from_opencode()
            route["model"] = resolve_model(route["model"], catalog)
        print(json.dumps(route))
        return 0
    except (RouteError, OSError, TypeError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
