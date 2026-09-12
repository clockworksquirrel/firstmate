#!/usr/bin/env python3
"""HandsFreeBridge's cooperative action gate; never changes native permissions.

Commands: mode, check, pending, resolve, resolver-command.
check reads {session_id, task_id, tool, args, request_id?} as JSON on stdin.
resolve reads {request_id, decision: allow|deny, user_words} on stdin and refuses
FM_TASK_ID workers. FirstMate reads the private request, asks in its own chat,
then records the user's exact words. These same-user checks cannot authenticate
those words cryptographically or protect against deliberate same-user tampering.

stdout contains only mode/decision metadata, IDs, or the fixed command template,
never action arguments or user words. Requests and answers are 0600 files under
FM_HOME/state/handsfree-tools. A per-home flock serializes deduplication,
resolution and one-use consumption. Pending requests/grants expire after 24h;
terminal records are retained to reject replays, including an implicit retry.
FM_STATE_OVERRIDE deliberately does not relocate this approval store.

The only shell exemption is the exact resolver-command template, with one JSON
object in its quoted heredoc, invoked by a primary. No shell substring matching,
SDK, shell execution, model calls, or native permission changes occur here.
Exit codes: 0 allowed/success, 3 pending, 4 denied/stale/replayed, 2 invalid/error.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time

# Import the single policy owner beside this script without creating __pycache__.
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location(
    "fm_handsfree_answer", Path(__file__).resolve().with_name("fm-handsfree-answer.py"))
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)

SCHEMA = "firstmate.handsfree-tool.v1"
MAX_INPUT = 262144
TTL = 24 * 60 * 60
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
DELIMITER = "FM_HANDSFREE_RESOLUTION"


class GateError(ValueError):
    """A safe diagnostic that must not contain captured action data."""


def parse_json(raw):
    def reject_constant(_):
        raise GateError("non-finite JSON numbers are unsupported")
    try:
        return json.loads(raw, object_pairs_hook=POLICY.object_without_duplicates,
                          parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise GateError("invalid JSON input or record") from error


def encode(data):
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, RecursionError) as error:
        raise GateError("unsupported JSON value") from error


def fields(data, required, optional=()):
    if (type(data) is not dict or not set(required) <= set(data)
            or set(data) - set(required) - set(optional)):
        raise GateError("unsupported or missing fields")


def identifier(value, pattern=NAME):
    if type(value) is not str or not pattern.fullmatch(value):
        raise GateError("invalid identifier")
    return value


def action_input(data):
    fields(data, ("session_id", "task_id", "tool", "args"), ("request_id",))
    identifier(data["session_id"])
    identifier(data["tool"])
    if data["task_id"] != "":
        identifier(data["task_id"], POLICY.TASK_ID)
        if data["task_id"] in (".", ".."):
            raise GateError("invalid task identifier")
    if type(data["args"]) is not dict:
        raise GateError("tool args must be an object")
    if data["task_id"] != os.environ.get("FM_TASK_ID", ""):
        raise GateError("task identity does not match the invoking process")
    if "request_id" in data:
        identifier(data["request_id"], REQUEST_ID)
    return {key: data[key] for key in ("session_id", "task_id", "tool", "args")}


def resolution_input(data):
    fields(data, ("request_id", "decision", "user_words"))
    identifier(data["request_id"], REQUEST_ID)
    if data["decision"] not in ("allow", "deny"):
        raise GateError("decision must be allow or deny")
    words = data["user_words"]
    if (type(words) is not str or not words.strip() or len(words) > 16384
            or "\x00" in words):
        raise GateError("resolution requires the user's exact nonempty words")
    return data


def resolver_template():
    quoted = "'" + str(Path(__file__).resolve()).replace("'", "'\"'\"'") + "'"
    return f"python3 {quoted} resolve <<'{DELIMITER}'\n{{payload}}\n{DELIMITER}"


def is_resolver(action):
    if action["tool"] != "bash" or os.environ.get("FM_TASK_ID"):
        return False
    args = action["args"]
    if set(args) - {"command", "description", "timeout"}:
        return False
    command = args.get("command")
    if not isinstance(command, str):
        return False
    prefix, suffix = resolver_template().split("{payload}")
    if not command.startswith(prefix) or not command.endswith(suffix):
        return False
    body = command[len(prefix):-len(suffix)]
    # The closing delimiter must not occur in the body; no extra shell syntax.
    if any(line == DELIMITER for line in body.splitlines()):
        return False
    try:
        resolution_input(parse_json(body))
    except GateError:
        return False
    return True


def bounded_read(action):
    tool, args = action["tool"], action["args"]
    if tool == "read":
        return (set(args) <= {"filePath", "offset", "limit"}
                and type(args.get("filePath")) is str and bool(args["filePath"])
                and len(args["filePath"]) <= 4096
                and type(args.get("limit")) is int and 1 <= args["limit"] <= 2000
                and type(args.get("offset", 0)) is int and args.get("offset", 0) >= 0)
    # OpenCode's native glob/grep results are capped by the native tools.
    if tool in ("glob", "grep"):
        allowed = {"pattern", "path"} | ({"include"} if tool == "grep" else set())
        return (set(args) <= allowed and bool(args.get("pattern"))
                and all(type(value) is str and 0 < len(value) <= 4096
                        for value in args.values()))
    return False


def safe_directory(path, private=False):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & forbidden):
        raise GateError("unsafe approval directory metadata")


def safe_open(path, flags, private=True):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    forbidden = 0o077 if private else 0o022
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & forbidden):
        os.close(fd)
        raise GateError("unsafe approval file metadata")
    return fd


def read_file(path):
    with os.fdopen(safe_open(path, os.O_RDONLY), "rb") as handle:
        raw = handle.read(MAX_INPUT * 2 + 1)
    if len(raw) > MAX_INPUT * 2:
        raise GateError("approval record exceeds the size limit")
    return parse_json(raw)


def atomic_write(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encode(data) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def store():
    home = POLICY.effective_home().absolute()
    safe_directory(home)
    state = home / "state"
    safe_directory(state)
    root = state / "handsfree-tools"
    safe_directory(root, private=True)
    for child in ("requests", "actions"):
        safe_directory(root / child, private=True)
    fd = safe_open(root / ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def action_key(action):
    return hashlib.sha256(encode(action)).hexdigest()


def load_request(root, request_id):
    identifier(request_id, REQUEST_ID)
    try:
        record = read_file(root / "requests" / f"{request_id}.json")
    except FileNotFoundError as error:
        raise GateError("unknown request ID") from error
    if (type(record) is not dict or record.get("schema") != SCHEMA
            or record.get("request_id") != request_id
            or record.get("status") not in ("pending", "allowed", "denied", "consumed", "expired")
            or type(record.get("action")) is not dict
            or record.get("action_key") != action_key(record["action"])
            or type(record.get("expires_at")) not in (int, float)):
        raise GateError("invalid approval record")
    return record


def save(root, record):
    atomic_write(root / "requests" / f"{record['request_id']}.json", record)


def notify_firstmate(root, record, resolved=False):
    task = record["action"]["task_id"]
    if not task:
        return
    identifier(task, POLICY.TASK_ID)
    request_id = record["request_id"]
    if resolved:
        line = f"resolved [key=handsfree-{request_id}]: HandsFreeBridge {record['status']}; request {request_id}\n"
    else:
        line = (f"needs-decision [key=handsfree-{request_id}]: HandsFreeBridge request {request_id}; "
                f"FirstMate must read state/handsfree-tools/requests/{request_id}.json, "
                "ask the user in its own conversation, then resolve allow/deny with exact user words.\n")
    # Existing status files need not be 0600: the event contains no action data.
    fd = safe_open(root.parent / f"{task}.status", os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                   private=False)
    with os.fdopen(fd, "ab") as handle:
        handle.write(line.encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())


def expire(root, record):
    if record["status"] in ("pending", "allowed") and time.time() >= record["expires_at"]:
        record["status"] = "expired"
        save(root, record)


def summary(record):
    return {"request_id": record["request_id"], "status": record["status"],
            "request_file": f"state/handsfree-tools/requests/{record['request_id']}.json",
            "resolver_command": resolver_template()}


def check(data):
    action = action_input(data)
    if len(encode(action)) > MAX_INPUT:
        raise GateError("encoded action exceeds the size limit")
    mode = POLICY.policy_mode()
    if "request_id" not in data:
        if is_resolver(action):
            return {"status": "allow", "reason": "primary-resolution"}, 0
        if mode == "no_prompt" or bounded_read(action):
            return {"status": "allow", "reason": mode if mode == "no_prompt" else "bounded-read"}, 0
    with store() as root:
        key = action_key(action)
        index = root / "actions" / f"{key}.json"
        if "request_id" in data:
            record = load_request(root, data["request_id"])
        elif index.exists() or index.is_symlink():
            pointer = read_file(index)
            fields(pointer, ("request_id",))
            record = load_request(root, pointer["request_id"])
        else:
            now = time.time()
            record = {"schema": SCHEMA, "request_id": secrets.token_hex(16),
                      "action": action, "action_key": key, "status": "pending",
                      "created_at": now, "expires_at": now + TTL, "notified": False}
            save(root, record)
            atomic_write(index, {"request_id": record["request_id"]})
        if encode(record["action"]) != encode(action):
            raise GateError("request does not match this session, task, tool and args")
        expire(root, record)
        if record["status"] == "allowed":
            record["status"] = "consumed"
            record["consumed_at"] = time.time()
            save(root, record)
            return {**summary(record), "status": "allow", "reason": "one-use-grant"}, 0
        if record["status"] == "pending":
            if not record["notified"]:
                notify_firstmate(root, record)
                record["notified"] = True
                save(root, record)
            return summary(record), 3
        return summary(record), 4


def resolve_request(data):
    if os.environ.get("FM_TASK_ID"):
        raise GateError("workers cannot resolve requests; report needs-decision through FirstMate")
    resolution_input(data)
    with store() as root:
        record = load_request(root, data["request_id"])
        expire(root, record)
        if record["status"] != "pending":
            return summary(record), 4
        record.update(status="allowed" if data["decision"] == "allow" else "denied",
                      resolution={"decision": data["decision"], "user_words": data["user_words"],
                                  "resolved_at": time.time()})
        save(root, record)
        notify_firstmate(root, record, resolved=True)
        return summary(record), 0


def main():
    try:
        if sys.argv[1:] == ["mode"]:
            print(POLICY.policy_mode())
            return 0
        if sys.argv[1:] == ["resolver-command"]:
            print(resolver_template())
            return 0
        if sys.argv[1:] == ["pending"]:
            if os.environ.get("FM_TASK_ID"):
                raise GateError("only FirstMate may list pending requests")
            with store() as root:
                result = []
                for path in sorted((root / "requests").glob("*.json")):
                    record = load_request(root, path.stem)
                    expire(root, record)
                    if record["status"] == "pending":
                        result.append(summary(record))
            print(json.dumps(result))
            return 0
        if sys.argv[1:] not in (["check"], ["resolve"]):
            raise GateError("usage: fm-handsfree-tool.py mode|check|pending|resolve|resolver-command")
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise GateError("input exceeds the size limit")
        data = parse_json(raw)
        result, code = check(data) if sys.argv[1] == "check" else resolve_request(data)
        print(json.dumps(result))
        return code
    except (GateError, POLICY.AnswerError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        # Never echo an exception that may embed action data, words, or paths.
        print("error: unable to safely process the HandsFreeBridge request", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
