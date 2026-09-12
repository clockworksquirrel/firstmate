#!/usr/bin/env python3
"""Validate one hands-free captain answer and emit Firstmate's keyed row.

Policy-loading behavior is adapted from HandsFreeBridge's broker_policy.py,
licensed under Apache-2.0. See THIRD_PARTY_NOTICES.md.

This program never closes a task or grants authority.
It writes one validated tab-separated row for fm-captain-hold.sh answers.
Policy precedence: instance config, personal global config, tracked defaults.
An installed accessibility broker's personal policy is reused globally;
otherwise global config is ~/.config/firstmate. FM_GLOBAL_CONFIG_OVERRIDE
selects an explicit isolated profile. All policies use one owner-only schema.
"""

import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


SCHEMA = "firstmate.handsfree-answer.v1"
TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_INPUT_BYTES = 16384
MAX_POLICY_BYTES = 65536


class AnswerError(ValueError):
    """The policy or captured answer is unsafe or unsupported."""


def object_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnswerError("JSON objects must not contain duplicate keys")
        result[key] = value
    return result


def repository_root():
    return Path(__file__).resolve().parent.parent


def effective_home():
    return Path(os.environ.get("FM_HOME") or repository_root())


def config_dir():
    return Path(os.environ.get("FM_CONFIG_OVERRIDE") or effective_home() / "config")


def global_policy_path():
    override = os.environ.get("FM_GLOBAL_CONFIG_OVERRIDE")
    if override:
        return Path(override) / "handsfree-approval.json"
    broker = Path.home() / ".codex" / "accessible-approvals" / "broker-policy.json"
    if broker.exists() or broker.is_symlink():
        return broker
    return Path.home() / ".config" / "firstmate" / "handsfree-approval.json"


def default_mode():
    try:
        data = json.loads((repository_root() / ".firstmate-defaults.json").read_text(encoding="utf-8"),
                          object_pairs_hook=object_without_duplicates)
    except (OSError, ValueError) as error:
        raise AnswerError("the tracked approval defaults are missing or invalid") from error
    if (not isinstance(data, dict) or set(data) != {"approval_mode"}
            or data["approval_mode"] not in ("prompt", "no_prompt")):
        raise AnswerError("the tracked approval defaults have an unsupported mode")
    return data["approval_mode"]


def read_policy(path):
    root = path.parent
    if root.exists() or root.is_symlink():
        metadata = os.lstat(root)
        if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022):
            raise AnswerError("hands-free config directory has unsafe metadata")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if path.is_symlink():
            raise AnswerError("hands-free approval policy must not be a symbolic link")
        return None
    except OSError as error:
        raise AnswerError("hands-free approval policy cannot be read safely") from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_POLICY_BYTES):
            raise AnswerError("hands-free approval policy has unsafe file metadata")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            data = json.load(handle, object_pairs_hook=object_without_duplicates)
    except (OSError, UnicodeError, ValueError) as error:
        raise AnswerError("hands-free approval policy is invalid") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if (not isinstance(data, dict) or set(data) != {"version", "mode"}
            or type(data.get("version")) is not int or data["version"] != 1
            or type(data.get("mode")) is not str
            or data.get("mode") not in {"prompt", "no_prompt"}):
        raise AnswerError("hands-free approval policy has an unsupported schema or mode")
    return data["mode"]


def policy_info():
    for source, path in (("instance", config_dir() / "handsfree-approval.json"), ("global", global_policy_path())):
        mode = read_policy(path)
        if mode is not None:
            return {"mode": mode, "source": source, "path": str(path)}
    return {"mode": default_mode(), "source": "repository",
            "path": str(repository_root() / ".firstmate-defaults.json")}


def policy_mode():
    return policy_info()["mode"]


def write_policy(mode, global_scope=False):
    if os.environ.get("FM_TASK_ID"):
        raise AnswerError("workers cannot change approval policy; report through the coordinator")
    if mode not in {"prompt", "no_prompt"}:
        raise AnswerError("hands-free approval mode must be prompt or no_prompt")
    path = global_policy_path() if global_scope else config_dir() / "handsfree-approval.json"
    root = path.parent
    try:
        root.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    metadata = os.lstat(root)
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022):
        raise AnswerError("hands-free approval config directory has unsafe metadata")
    payload = (json.dumps({"version": 1, "mode": mode}, separators=(",", ":"))
               + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=".handsfree-", dir=root)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def clean_text(value, field, maximum):
    if type(value) is not str or not value or value != value.strip():
        raise AnswerError("{} must be a non-empty trimmed string".format(field))
    if len(value) > maximum or CONTROL.search(value):
        raise AnswerError("{} contains unsupported control text or is too long".format(field))
    return value


def read_answer():
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise AnswerError("captured answer exceeds 16384 bytes")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=object_without_duplicates)
    except (UnicodeError, ValueError) as error:
        raise AnswerError("captured answer must be one UTF-8 JSON object") from error
    allowed = {"schema", "task_id", "answer", "label", "close_mode", "confirmed"}
    required = {"schema", "task_id", "answer", "label", "close_mode"}
    if not isinstance(data, dict) or set(data) - allowed or not required <= set(data):
        raise AnswerError("captured answer has unsupported or missing fields")
    if data.get("schema") != SCHEMA:
        raise AnswerError("captured answer has an unsupported schema")
    task_id = clean_text(data.get("task_id"), "task_id", 128)
    if not TASK_ID.fullmatch(task_id):
        raise AnswerError("task_id must use letters, digits, dots, dashes, or underscores")
    answer = clean_text(data.get("answer"), "answer", 512)
    if answer.casefold() == "reconcile":
        raise AnswerError("reconcile is reserved for evidence-backed reconciliation")
    label = clean_text(data.get("label"), "label", 128)
    close_mode = data.get("close_mode")
    if type(close_mode) is not str or close_mode not in {"done", "release"}:
        raise AnswerError("close_mode must be done or release")
    confirmed = data.get("confirmed", False)
    if type(confirmed) is not bool:
        raise AnswerError("confirmed must be a boolean")
    return task_id, answer, label, close_mode, confirmed


def main():
    try:
        arguments = sys.argv[1:]
        if arguments == ["mode"]:
            print(policy_mode())
            return 0
        if arguments == ["policy"]:
            print(json.dumps(policy_info()))
            return 0
        if len(arguments) == 2 and arguments[0] in ("set-mode", "set-global-mode"):
            write_policy(arguments[1], global_scope=arguments[0] == "set-global-mode")
            print(arguments[1])
            return 0
        if arguments not in ([], ["submit"]):
            raise AnswerError("usage: fm-handsfree-answer.py [submit|mode|policy|set-mode MODE|set-global-mode MODE]")
        task_id, answer, label, close_mode, confirmed = read_answer()
        mode = policy_mode()
        if mode == "prompt" and not confirmed:
            print(
                "confirmation required for hands-free answer to task {}".format(task_id),
                file=sys.stderr,
            )
            return 3
        print("{}\t{}\t{}\t{}".format(task_id, answer, label, close_mode))
        return 0
    except AnswerError as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
