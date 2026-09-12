#!/usr/bin/env python3
"""Deterministic state for the optional principal-coordination review profile.

The semantic policy is owned by
.agents/skills/principal-coordination/SKILL.md.
This script records evidence hashes and enforces transition order.
It never launches an agent, selects a model, or contacts the captain.

Revision mechanics: `revise TASK --stage plan|implementation --artifact FILE
--reason TEXT` starts a fresh review round, preserving prior events and private
artifact snapshots. Plan revisions reset authorization and implementation;
implementation revisions reset post-reviews and acceptance only. Use versioned
source artifacts: active originals are rehashed, and changing one requires an
explicit revision. Old v1 state needs a plan revision before it can pass a gate.

`require TASK --phase authorized|accepted [--kind ship|scout|secondmate]` is
read-only, including on failure. It honors FM_CONFIG_OVERRIDE or FM_HOME/config:
absent/standard is inactive, principal-review gates ships, unknown values refuse.
The hooks check artifact evidence, not the actual Git tree or a reviewer session.
FM_TASK_ID refuses worker review/authorization/reconciliation/revision calls.
This is cooperative same-user workflow enforcement, not authentication or OS
containment: a same-user process can unset the marker or alter private state.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


SCHEMA = "firstmate.principal-coordination.v2"
LEGACY_SCHEMA = "firstmate.principal-coordination.v1"
TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
TEXT_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
REVIEWER_ROLES = ("reviewer-a", "reviewer-b")


class GateError(ValueError):
    """The requested transition or local evidence is invalid."""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z")


def repository_root():
    return Path(__file__).resolve().parent.parent


def effective_home():
    return Path(os.environ.get("FM_HOME") or repository_root())


def effective_state_dir():
    return Path(os.environ.get("FM_STATE_OVERRIDE") or effective_home() / "state")


def require_task_id(value):
    if not TASK_ID.fullmatch(value):
        raise GateError("task id must use 1-128 letters, digits, dots, dashes, or underscores")
    return value


def require_alias(value):
    if (type(value) is not str or not value or value != value.strip()
            or len(value) > 128 or TEXT_CONTROL.search(value)):
        raise GateError("reviewer aliases must be trimmed 1-128 character strings without controls")
    return value


def ensure_private_root(create=True):
    state_dir = effective_state_dir()
    if create:
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_metadata = os.lstat(state_dir)
    if (not stat.S_ISDIR(state_metadata.st_mode) or stat.S_ISLNK(state_metadata.st_mode)
            or state_metadata.st_uid != os.getuid()
            or stat.S_IMODE(state_metadata.st_mode) & 0o022):
        raise GateError("principal coordination parent state directory has unsafe metadata")
    root = state_dir / "principal-coordination"
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    metadata = os.lstat(root)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise GateError("principal coordination state root is not a real directory")
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise GateError("principal coordination state root must be owner-only mode 0700")
    return root


@contextmanager
def locked(root, readonly=False):
    flags = (os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT)
    flags |= getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
    descriptor = os.open(root / ".lock", flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise GateError("principal coordination lock has unsafe metadata")
        fcntl.flock(descriptor, fcntl.LOCK_SH if readonly else fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def state_path(root, task_id):
    return root / (require_task_id(task_id) + ".json")


def read_state(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as error:
        raise GateError("principal coordination task does not exist") from error
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > MAX_STATE_BYTES):
            raise GateError("principal coordination task has unsafe metadata")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            data = json.load(handle)
    except (OSError, UnicodeError, ValueError) as error:
        raise GateError("principal coordination task is invalid") from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if (not isinstance(data, dict) or data.get("schema") not in (SCHEMA, LEGACY_SCHEMA)
            or not isinstance(data.get("events"), list)
            or not isinstance(data.get("reviewers"), dict)
            or set(data.get("reviewers", {})) != set(REVIEWER_ROLES)
            or any(type(value) is not str for value in data["reviewers"].values())
            or len(set(data["reviewers"].values())) != 2
            or any(require_alias(value) != value for value in data["reviewers"].values())
            or type(data.get("task_id")) is not str
            or data.get("task_id") != path.stem
            or not TASK_ID.fullmatch(data.get("task_id", ""))
            or any(not isinstance(event, dict)
                   or event.get("sequence") != index
                   or type(event.get("actor")) is not str
                   for index, event in enumerate(data["events"], 1))):
        raise GateError("principal coordination task has an unsupported schema")
    if data["schema"] == SCHEMA:
        if (type(data.get("plan_revision")) is not int or data["plan_revision"] < 1
                or type(data.get("implementation_revision")) is not int
                or data["implementation_revision"] < 0
                or not valid_artifact(data.get("intent"))
                or (data["implementation_revision"] == 0
                    and data.get("implementation") is not None)
                or (data["implementation_revision"] > 0
                    and not valid_artifact(data.get("implementation")))):
            raise GateError("principal coordination task has invalid revision identity")
        for event in data["events"]:
            # Legacy events are retained for audit, never counted as approvals.
            if "plan_revision" not in event:
                continue
            if (type(event["plan_revision"]) is not int
                    or not 1 <= event["plan_revision"] <= data["plan_revision"]
                    or type(event.get("implementation_revision")) is not int
                    or event["implementation_revision"] < 0
                    or event.get("kind") not in (
                        "initialized", "plan-revised", "implementation-revised",
                        "review", "authorized", "implemented", "accepted")):
                raise GateError("principal coordination event has invalid revision identity")
            if event["kind"] == "review" and (
                    event.get("stage") not in ("pre", "post")
                    or event.get("reviewer_role") not in REVIEWER_ROLES
                    or event.get("reviewer_alias") != data["reviewers"][event["reviewer_role"]]
                    or event.get("verdict") not in ("approved", "changes-required")):
                raise GateError("principal coordination review is invalid")
            if event["kind"] in ("review", "authorized", "implemented", "accepted",
                                  "plan-revised", "implementation-revised"):
                if not valid_artifact(event.get("artifact")):
                    raise GateError("principal coordination event evidence is invalid")
    return data


def write_state(path, data):
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise GateError("principal coordination task exceeds its state limit")
    descriptor, temporary = tempfile.mkstemp(prefix=".principal-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
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


def valid_artifact(value):
    return (isinstance(value, dict) and type(value.get("path")) is str
            and Path(value["path"]).is_absolute()
            and type(value.get("sha256")) is str
            and re.fullmatch(r"[a-f0-9]{64}", value["sha256"]) is not None
            and type(value.get("bytes")) is int
            and 0 <= value["bytes"] <= MAX_ARTIFACT_BYTES)


def evidence(path_text, archive=False):
    path = Path(path_text).absolute()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_ARTIFACT_BYTES:
                raise GateError("evidence artifact must be a regular file no larger than 64 MiB")
            content = handle.read(MAX_ARTIFACT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except OSError as error:
        raise GateError("evidence artifact cannot be read") from error
    if (len(content) != metadata.st_size or len(content) > MAX_ARTIFACT_BYTES
            or metadata.st_mtime_ns != after.st_mtime_ns
            or metadata.st_ctime_ns != after.st_ctime_ns):
        raise GateError("evidence artifact changed while being read")
    record = {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
              "bytes": len(content)}
    if archive:
        root = effective_state_dir() / "principal-coordination" / "artifacts"
        root.mkdir(mode=0o700, exist_ok=True)
        metadata = os.lstat(root)
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise GateError("artifact archive must be an owner-only real directory")
        target = root / record["sha256"]
        if target.exists() or target.is_symlink():
            metadata = os.lstat(target)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or evidence(target)["sha256"] != record["sha256"]):
                raise GateError("archived evidence has unsafe metadata or changed content")
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=root)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                directory = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        record["snapshot"] = str(target)
    return record


def verify_artifact(record):
    if evidence(record["path"]) != {key: record[key] for key in ("path", "sha256", "bytes")}:
        raise GateError("evidence changed; record an explicit revision: {}".format(record["path"]))


def require_revision_state(state):
    if state["schema"] != SCHEMA:
        raise GateError("legacy evidence has no revision identity; use revise --stage plan")


def refuse_worker():
    if os.environ.get("FM_TASK_ID"):
        raise GateError("worker-marked FM_TASK_ID process cannot review, authorize, reconcile, or revise; "
                        "this is cooperative workflow enforcement, not authentication")


def append_event(state, kind, actor, artifact=None, **details):
    event = {
        "sequence": len(state["events"]) + 1,
        "at": utc_now(),
        "kind": kind,
        "actor": actor,
        "plan_revision": state["plan_revision"],
        "implementation_revision": state["implementation_revision"],
    }
    if artifact is not None:
        event["artifact"] = artifact
    event.update(details)
    state["events"].append(event)


def current_events(state):
    for event in state["events"]:
        if event.get("plan_revision") != state["plan_revision"]:
            continue
        if (event.get("kind") in ("implemented", "implementation-revised", "accepted")
                or event.get("stage") == "post"):
            if event.get("implementation_revision") != state["implementation_revision"]:
                continue
        yield event


def has_event(state, kind):
    return any(event.get("kind") == kind for event in current_events(state))


def latest_reviews(state, stage):
    reviews = {role: None for role in REVIEWER_ROLES}
    for event in current_events(state):
        if event.get("kind") == "review" and event.get("stage") == stage:
            role = event.get("reviewer_role")
            if role in reviews:
                reviews[role] = event
    return reviews


def reviews_approved(state, stage):
    reviews = latest_reviews(state, stage)
    return all(review is not None and review.get("verdict") == "approved"
               for review in reviews.values())


def verify_current_evidence(state, stage):
    require_revision_state(state)
    verify_artifact(state["intent"])
    for review in latest_reviews(state, "pre").values():
        if review is not None:
            verify_artifact(review["artifact"])
    for event in current_events(state):
        if event["kind"] == "authorized":
            verify_artifact(event["artifact"])
    if stage == "post":
        if state["implementation"] is not None:
            verify_artifact(state["implementation"])
        for review in latest_reviews(state, "post").values():
            if review is not None:
                verify_artifact(review["artifact"])
        for event in current_events(state):
            if event["kind"] == "accepted":
                verify_artifact(event["artifact"])


def phase(state):
    if has_event(state, "accepted"):
        return "accepted"
    if state["implementation"] is not None:
        return "post-reviewed" if reviews_approved(state, "post") else "post-review"
    if has_event(state, "authorized"):
        return "authorized"
    return "pre-reviewed" if reviews_approved(state, "pre") else "pre-review"


def next_action(state):
    current = phase(state)
    return {
        "pre-review": "first mate collects independent pre-implementation reviews",
        "pre-reviewed": "coordinator records authorization before implementation",
        "authorized": "coding worker may implement the accepted scope",
        "post-review": "first mate collects independent post-implementation reviews",
        "post-reviewed": "coordinator reconciles the reviews and records acceptance",
        "accepted": "first mate returns the verified outcome to the captain",
    }[current]


def snapshot(state):
    if state["schema"] != SCHEMA:
        return {"schema": state["schema"], "task_id": state["task_id"],
                "phase": "revision-required", "event_count": len(state["events"]),
                "next_action": "use revise --stage plan to bind legacy evidence to a revision"}
    stale = {}
    for stage in ("pre", "post"):
        try:
            verify_current_evidence(state, stage)
        except (GateError, OSError) as error:
            stale[stage] = str(error)
    review_summary = {}
    for stage in ("pre", "post"):
        review_summary[stage] = {
            role: (None if event is None or stage in stale else {
                "alias": event["reviewer_alias"],
                "verdict": event["verdict"],
                "artifact": event["artifact"],
                "sequence": event["sequence"],
            })
            for role, event in latest_reviews(state, stage).items()
        }
    return {
        "schema": state["schema"],
        "task_id": state["task_id"],
        "captain_contact": "firstmate-only",
        "phase": "revision-required" if stale else phase(state),
        "next_action": "record an explicit revision" if stale else next_action(state),
        "plan_revision": state["plan_revision"],
        "implementation_revision": state["implementation_revision"],
        "stale_evidence": stale,
        "reviewers": state["reviewers"],
        "reviews": review_summary,
        "event_count": len(state["events"]),
    }


def emit(state):
    print(json.dumps(snapshot(state), indent=2, ensure_ascii=False))


def command_init(arguments):
    reviewer_a = require_alias(arguments.reviewer_a)
    reviewer_b = require_alias(arguments.reviewer_b)
    if reviewer_a == reviewer_b:
        raise GateError("reviewer aliases must be distinct")
    root = ensure_private_root()
    path = state_path(root, arguments.task_id)
    with locked(root):
        intent = evidence(arguments.intent_file, archive=True)
        if path.exists():
            state = read_state(path)
            require_revision_state(state)
            if (state.get("intent") != intent or state.get("reviewers") != {
                    "reviewer-a": arguments.reviewer_a,
                    "reviewer-b": arguments.reviewer_b}):
                raise GateError("principal coordination task already exists with different inputs")
            emit(state)
            return
        state = {
            "schema": SCHEMA,
            "task_id": require_task_id(arguments.task_id),
            "captain_contact": "firstmate-only",
            "reviewers": {
                "reviewer-a": reviewer_a,
                "reviewer-b": reviewer_b,
            },
            "intent": intent,
            "plan_revision": 1,
            "implementation_revision": 0,
            "implementation": None,
            "events": [],
        }
        append_event(state, "initialized", "firstmate", intent=intent)
        write_state(path, state)
        emit(state)


def mutate(task_id, change):
    root = ensure_private_root()
    path = state_path(root, task_id)
    with locked(root):
        state = read_state(path)
        change(state)
        write_state(path, state)
        emit(state)


def command_review(arguments):
    refuse_worker()
    def change(state):
        verify_current_evidence(state, arguments.stage)
        if has_event(state, "accepted"):
            raise GateError("accepted work cannot receive another review")
        if arguments.stage == "pre" and has_event(state, "authorized"):
            raise GateError("pre-implementation review is frozen after authorization")
        if arguments.stage == "post" and state["implementation"] is None:
            raise GateError("post-implementation review requires implementation evidence")
        matching = [role for role, alias in state["reviewers"].items()
                    if alias == arguments.reviewer]
        if len(matching) != 1:
            raise GateError("reviewer must match exactly one configured reviewer alias")
        append_event(
            state,
            "review",
            arguments.reviewer,
            evidence(arguments.artifact, archive=True),
            stage=arguments.stage,
            reviewer_role=matching[0],
            reviewer_alias=arguments.reviewer,
            verdict=arguments.verdict,
        )
    mutate(arguments.task_id, change)


def command_authorize(arguments):
    refuse_worker()
    def change(state):
        verify_current_evidence(state, "pre")
        if has_event(state, "authorized"):
            raise GateError("implementation is already authorized")
        if not reviews_approved(state, "pre"):
            raise GateError("both current pre-implementation reviews must approve")
        append_event(state, "authorized", "coordinator", evidence(arguments.artifact, archive=True))
    mutate(arguments.task_id, change)


def command_implemented(arguments):
    def change(state):
        verify_current_evidence(state, "pre")
        if not has_event(state, "authorized"):
            raise GateError("implementation requires coordinator authorization")
        if state["implementation"] is not None:
            raise GateError("implementation evidence is already recorded; use revise --stage implementation")
        state["implementation"] = evidence(arguments.artifact, archive=True)
        state["implementation_revision"] = 1
        append_event(state, "implemented", "coding-worker", state["implementation"])
    mutate(arguments.task_id, change)


def command_reconcile(arguments):
    refuse_worker()
    def change(state):
        verify_current_evidence(state, "post")
        if has_event(state, "accepted"):
            raise GateError("work is already accepted")
        if not reviews_approved(state, "post"):
            raise GateError("both current post-implementation reviews must approve")
        if not has_event(state, "authorized") or state["implementation"] is None:
            raise GateError("acceptance requires authorized implementation evidence")
        append_event(state, "accepted", "coordinator", evidence(arguments.artifact, archive=True))
    mutate(arguments.task_id, change)


def command_revise(arguments):
    refuse_worker()
    if not arguments.reason.strip() or len(arguments.reason) > 1024 or TEXT_CONTROL.search(arguments.reason):
        raise GateError("revision reason must be 1-1024 characters without controls")
    def change(state):
        if arguments.stage == "plan":
            state["plan_revision"] = (state.get("plan_revision", 0) + 1
                                      if state["schema"] == SCHEMA else 1)
            state["schema"] = SCHEMA
            state["intent"] = evidence(arguments.artifact, archive=True)
            state["implementation_revision"] = 0
            state["implementation"] = None
            artifact = state["intent"]
        else:
            verify_current_evidence(state, "pre")
            if not has_event(state, "authorized") or state["implementation"] is None:
                raise GateError("implementation revision requires an authorized, recorded implementation")
            state["implementation"] = evidence(arguments.artifact, archive=True)
            state["implementation_revision"] += 1
            artifact = state["implementation"]
        append_event(state, arguments.stage + "-revised", "coordinator", artifact,
                     reason=arguments.reason)
    mutate(arguments.task_id, change)


def profile():
    root = Path(os.environ.get("FM_CONFIG_OVERRIDE") or effective_home() / "config")
    path = root / "coordination-profile"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        if path.is_symlink():
            raise GateError("coordination profile must not be a symbolic link")
        return "standard"
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128:
            raise GateError("coordination profile must be a regular file of at most 128 bytes")
        value = handle.read(129).decode("utf-8").strip()
    if value not in ("standard", "principal-review"):
        raise GateError("unknown coordination profile; expected standard or principal-review")
    return value


def command_require(arguments):
    selected = profile()
    if selected == "standard" or arguments.kind != "ship":
        print(json.dumps({"profile": selected, "active": False, "kind": arguments.kind}))
        return
    root = ensure_private_root(create=False)
    with locked(root, readonly=True):
        state = read_state(state_path(root, arguments.task_id))
        verify_current_evidence(state, "post" if arguments.phase == "accepted" else "pre")
        if not has_event(state, "authorized") or not reviews_approved(state, "pre"):
            raise GateError("principal-review requires authorized implementation for task " + arguments.task_id)
        if arguments.phase == "accepted" and (
                not has_event(state, "accepted") or not reviews_approved(state, "post")
                or state["implementation"] is None):
            raise GateError("principal-review requires accepted implementation for task " + arguments.task_id)
        print(json.dumps({"profile": selected, "active": True, "task_id": arguments.task_id,
                          "required": arguments.phase, "plan_revision": state["plan_revision"],
                          "implementation_revision": state["implementation_revision"]}))


def command_status(arguments):
    root = ensure_private_root(create=False)
    with locked(root, readonly=True):
        emit(read_state(state_path(root, arguments.task_id)))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("task_id")
    init.add_argument("--intent-file", required=True)
    init.add_argument("--reviewer-a", required=True)
    init.add_argument("--reviewer-b", required=True)
    init.set_defaults(handler=command_init)

    review = commands.add_parser("review")
    review.add_argument("task_id")
    review.add_argument("--stage", choices=("pre", "post"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--verdict", choices=("approved", "changes-required"), required=True)
    review.add_argument("--artifact", required=True)
    review.set_defaults(handler=command_review)

    authorize = commands.add_parser("authorize")
    authorize.add_argument("task_id")
    authorize.add_argument("--artifact", required=True)
    authorize.set_defaults(handler=command_authorize)

    implemented = commands.add_parser("implemented")
    implemented.add_argument("task_id")
    implemented.add_argument("--artifact", required=True)
    implemented.set_defaults(handler=command_implemented)

    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("task_id")
    reconcile.add_argument("--artifact", required=True)
    reconcile.set_defaults(handler=command_reconcile)

    revise = commands.add_parser("revise", help="reset a review round while preserving prior evidence")
    revise.add_argument("task_id")
    revise.add_argument("--stage", choices=("plan", "implementation"), required=True)
    revise.add_argument("--artifact", required=True)
    revise.add_argument("--reason", required=True)
    revise.set_defaults(handler=command_revise)

    require = commands.add_parser("require", help="read-only runtime gate; inactive for standard profile")
    require.add_argument("task_id")
    require.add_argument("--phase", choices=("authorized", "accepted"), required=True)
    require.add_argument("--kind", choices=("ship", "scout", "secondmate"), default="ship")
    require.set_defaults(handler=command_require)

    status_parser = commands.add_parser("status")
    status_parser.add_argument("task_id")
    status_parser.set_defaults(handler=command_status)
    return result


def main():
    try:
        arguments = parser().parse_args()
        arguments.handler(arguments)
        return 0
    except GateError as error:
        print("error: {}".format(error), file=sys.stderr)
        return 2
    except (OSError, UnicodeError) as error:
        print("error: principal coordination evidence/configuration unavailable: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
