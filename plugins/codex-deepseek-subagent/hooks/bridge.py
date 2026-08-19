#!/usr/bin/env python3
"""Explicitly authorized task bridge for one Codex custom subagent role.

The normal path is entirely lifecycle driven:

* UserPromptSubmit records a body-free, one-use grant for an explicit skill call.
* PreToolUse captures the plaintext ``spawn_agent`` message and reserves it.
* SubagentStart claims the matching reservation and injects the assignment.

No model or provider is called by this program.  State contains plaintext only
for the short interval between those two events.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Iterator
import uuid


ROLE = "deepseek_evidence_worker"
DELEGATION_TOKEN = "$use-deepseek-subagent"
PROTOCOL = "codex-deepseek-subagent/v1"
SCHEMA = 1
MAX_ASSIGNMENT_BYTES = 49_152
MAX_STATE_BYTES = 112_640
DEFAULT_TTL_SECONDS = 120
MAX_TTL_SECONDS = 3_600
# The grant is already bound to one parent turn and is consumed once.  Keep it
# long enough for high-reasoning parents to decide and formulate a bounded job,
# while still cleaning abandoned turns without retaining any prompt body.
DEFAULT_GRANT_TTL_SECONDS = 900
MAX_GRANT_TTL_SECONDS = 3_600
LOCK_TIMEOUT_SECONDS = 3.0
RECEIPT_RETENTION_SECONDS = 7 * 24 * 60 * 60
CONSUMED_GRANT_RETENTION_SECONDS = RECEIPT_RETENTION_SECONDS
STATE_DIR_NAME = "handoff-state"
FALLBACK_DIR_NAME = "codex-deepseek-subagent"
DELEGATION_TOKEN_RE = re.compile(
    rf"^\s*{re.escape(DELEGATION_TOKEN)}(?:\s|$)"
)

if os.name == "nt":
    import msvcrt  # type: ignore[import-not-found]
else:
    import fcntl  # type: ignore[import-not-found]


class BridgeError(RuntimeError):
    """Expected bridge failure safe to show without task contents."""


class UnsafeStateRoot(BridgeError):
    """The selected state root cannot provide the required local privacy."""


class InvalidReservation(BridgeError):
    """A reservation failed structural or attribution validation."""


def _compact_json(value: Any) -> str:
    # Hook stdout can inherit a legacy Windows console encoding such as cp1252.
    # ASCII-safe JSON preserves the original Unicode after parsing while making
    # every lifecycle response portable across Windows, WSL, macOS, and Linux.
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _emit(value: Any) -> None:
    sys.stdout.write(_compact_json(value))
    sys.stdout.flush()


def _deny(reason: str) -> None:
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def _ready_context(handoff_id: str, assignment: str) -> str:
    return _compact_json(
        {
            "bridge_protocol": PROTOCOL,
            "status": "ready",
            "handoff_id": handoff_id,
            "assignment": assignment,
        }
    )


def _error_context(code: str, message: str, handoff_id: str | None = None) -> str:
    return _compact_json(
        {
            "bridge_protocol": PROTOCOL,
            "status": "error",
            "handoff_id": handoff_id,
            "assignment": "",
            "error": {"code": code, "message": message},
        }
    )


def _emit_subagent_context(context: str) -> None:
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": context,
            }
        }
    )


def _canonical_id(value: Any, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise BridgeError(f"{field} is missing or invalid")
    if "\x00" in value:
        raise BridgeError(f"{field} contains a NUL character")
    return value


def _normalized_cwd(value: Any) -> str:
    raw = _canonical_id(value, "cwd", maximum=4096)
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(raw))))


def _reservation_key(session_id: str, cwd: str) -> str:
    """Key the one in-flight handoff by identities shared by both Hook events.

    SubagentStart receives the child's newly allocated turn id, not the parent
    turn id observed by PreToolUse.  The parent turn remains audit metadata but
    must never participate in delivery lookup.
    """

    material = _compact_json([session_id, cwd, ROLE]).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _grant_key(session_id: str, parent_turn_id: str, cwd: str) -> str:
    """Bind explicit authority to exactly one parent turn and workspace."""

    material = _compact_json([session_id, parent_turn_id, cwd, ROLE]).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _ttl_seconds() -> int:
    raw = os.environ.get("CODEX_DEEPSEEK_SUBAGENT_TTL_SECONDS")
    if raw is None:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError as error:
        raise BridgeError("CODEX_DEEPSEEK_SUBAGENT_TTL_SECONDS must be an integer") from error
    if not 1 <= value <= MAX_TTL_SECONDS:
        raise BridgeError(
            f"CODEX_DEEPSEEK_SUBAGENT_TTL_SECONDS must be between 1 and {MAX_TTL_SECONDS}"
        )
    return value


def _grant_ttl_seconds() -> int:
    raw = os.environ.get("CODEX_DEEPSEEK_SUBAGENT_GRANT_TTL_SECONDS")
    if raw is None:
        return DEFAULT_GRANT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError as error:
        raise BridgeError(
            "CODEX_DEEPSEEK_SUBAGENT_GRANT_TTL_SECONDS must be an integer"
        ) from error
    if not 1 <= value <= MAX_GRANT_TTL_SECONDS:
        raise BridgeError(
            "CODEX_DEEPSEEK_SUBAGENT_GRANT_TTL_SECONDS must be between "
            f"1 and {MAX_GRANT_TTL_SECONDS}"
        )
    return value


def _verify_private_posix_path(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeStateRoot(f"state path is a symbolic link: {path}")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(info.st_mode):
        raise UnsafeStateRoot(f"state path has an unexpected file type: {path}")
    if info.st_uid != os.geteuid():
        raise UnsafeStateRoot(f"state path is not owned by the current user: {path}")
    if info.st_mode & 0o077:
        raise UnsafeStateRoot(f"state path is accessible by group or other users: {path}")


def _prepare_private_root(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    if candidate.exists() and candidate.is_symlink():
        raise UnsafeStateRoot(f"state root is a symbolic link: {candidate}")
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)

    if os.name != "nt":
        os.chmod(candidate, 0o700)
        _verify_private_posix_path(candidate, directory=True)

    probe = candidate / f".privacy-probe-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(probe, flags, 0o600)
        os.write(descriptor, b"private")
        os.fsync(descriptor)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise UnsafeStateRoot(
                    f"state filesystem does not preserve private file permissions: {candidate}"
                )
    except OSError as error:
        raise UnsafeStateRoot(f"state root privacy probe failed: {candidate}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
    return candidate.resolve()


def state_root() -> Path:
    plugin_data = os.environ.get("PLUGIN_DATA")
    preferred_error: Exception | None = None
    if plugin_data:
        try:
            return _prepare_private_root(Path(plugin_data) / STATE_DIR_NAME)
        except (OSError, UnsafeStateRoot) as error:
            preferred_error = error

    home = os.environ.get("HOME")
    fallback_base = Path(home) if home else Path.home()
    fallback = fallback_base / ".local" / "state" / FALLBACK_DIR_NAME
    try:
        return _prepare_private_root(fallback)
    except (OSError, UnsafeStateRoot) as error:
        if preferred_error is not None:
            raise UnsafeStateRoot(
                f"PLUGIN_DATA and fallback state roots are unsafe: {preferred_error}; {error}"
            ) from error
        raise


def _ensure_subdirs(root: Path) -> None:
    for name in (
        "grants",
        "consumed-grants",
        "reservations",
        "claims",
        "receipts",
        "quarantine",
    ):
        _prepare_private_root(root / name)


@contextlib.contextmanager
def _state_lock(root: Path) -> Iterator[None]:
    _ensure_subdirs(root)
    lock_path = root / ".bridge.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.name != "nt":
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if info.st_mode & 0o077:
            os.close(descriptor)
            raise UnsafeStateRoot("the bridge lock is not private")
    stream = os.fdopen(descriptor, "r+b", buffering=0)
    if os.name == "nt":
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)

    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise BridgeError("bridge state is busy; retry the spawn after the current dispatch")
                time.sleep(0.02)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, value: Any) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    payload = _compact_json(value).encode("utf-8")
    descriptor: int | None = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.write(descriptor, payload)
        os.fsync(descriptor)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        if os.name != "nt":
            _verify_private_posix_path(path, directory=False)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidReservation(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise InvalidReservation(f"non-finite JSON number is forbidden: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise InvalidReservation(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _read_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
        raise InvalidReservation("state file is not a bounded regular file")
    if os.name != "nt" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
        raise InvalidReservation("state file permissions or ownership are unsafe")
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(
                stream,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_constant,
                parse_float=_parse_finite_float,
            )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidReservation("state file is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise InvalidReservation("state document must be a JSON object")
    return value


def _is_finite_json_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidReservation(f"{field} is not a string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise InvalidReservation(f"{field} is not a UUID") from error
    if str(parsed) != value:
        raise InvalidReservation(f"{field} is not a canonical UUID")
    return value


def _validate_reservation(value: dict[str, Any], *, now: float) -> dict[str, Any]:
    if value.get("schema") != SCHEMA or value.get("bridge_protocol") != PROTOCOL:
        raise InvalidReservation("reservation schema or protocol is invalid")
    _canonical_uuid(value.get("handoff_id"), "handoff_id")
    try:
        for field, maximum in (
            ("key_hash", 64),
            ("session_id", 512),
            ("parent_turn_id", 512),
            ("cwd", 4096),
            ("role", 128),
            ("task_name", 512),
            ("tool_use_id", 512),
            ("permission_mode", 64),
            ("assignment_sha256", 64),
        ):
            _canonical_id(value.get(field), field, maximum=maximum)
    except BridgeError as error:
        raise InvalidReservation(str(error)) from error
    if value["role"] != ROLE:
        raise InvalidReservation("reservation role is invalid")
    if value["permission_mode"] == "bypassPermissions":
        raise InvalidReservation("reservation permission mode is forbidden")
    if not re.fullmatch(r"[0-9a-f]{64}", value["key_hash"]):
        raise InvalidReservation("reservation key hash is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", value["assignment_sha256"]):
        raise InvalidReservation("assignment digest is invalid")
    assignment = value.get("assignment")
    if not isinstance(assignment, str) or not assignment.strip():
        raise InvalidReservation("assignment is empty or invalid")
    try:
        assignment_bytes = assignment.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidReservation("assignment is not valid UTF-8 text") from error
    if len(assignment_bytes) > MAX_ASSIGNMENT_BYTES:
        raise InvalidReservation("assignment exceeds the bridge size limit")
    if value.get("assignment_utf8_bytes") != len(assignment_bytes):
        raise InvalidReservation("assignment byte count does not match")
    if value["assignment_sha256"] != hashlib.sha256(assignment_bytes).hexdigest():
        raise InvalidReservation("assignment digest does not match")
    created_at = value.get("created_at")
    expires_at = value.get("expires_at")
    if not _is_finite_json_number(created_at):
        raise InvalidReservation("created_at is invalid")
    if not _is_finite_json_number(expires_at):
        raise InvalidReservation("expires_at is invalid")
    if expires_at <= created_at or expires_at - created_at > MAX_TTL_SECONDS + 1:
        raise InvalidReservation("reservation lifetime is invalid")
    if created_at > now + 30:
        raise InvalidReservation("reservation creation time is in the future")
    expected_key = _reservation_key(value["session_id"], value["cwd"])
    if value["key_hash"] != expected_key:
        raise InvalidReservation("reservation attribution hash is invalid")
    return value


def _validate_grant(value: dict[str, Any], *, now: float) -> dict[str, Any]:
    if value.get("schema") != SCHEMA or value.get("bridge_protocol") != PROTOCOL:
        raise InvalidReservation("grant schema or protocol is invalid")
    _canonical_uuid(value.get("grant_id"), "grant_id")
    try:
        for field, maximum in (
            ("key_hash", 64),
            ("session_id", 512),
            ("parent_turn_id", 512),
            ("cwd", 4096),
            ("role", 128),
        ):
            _canonical_id(value.get(field), field, maximum=maximum)
    except BridgeError as error:
        raise InvalidReservation(str(error)) from error
    if value["role"] != ROLE:
        raise InvalidReservation("grant role is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", value["key_hash"]):
        raise InvalidReservation("grant key hash is invalid")
    created_at = value.get("created_at")
    expires_at = value.get("expires_at")
    if not _is_finite_json_number(created_at):
        raise InvalidReservation("grant created_at is invalid")
    if not _is_finite_json_number(expires_at):
        raise InvalidReservation("grant expires_at is invalid")
    if expires_at <= created_at or expires_at - created_at > MAX_GRANT_TTL_SECONDS + 1:
        raise InvalidReservation("grant lifetime is invalid")
    if created_at > now + 30:
        raise InvalidReservation("grant creation time is in the future")
    expected_key = _grant_key(
        value["session_id"], value["parent_turn_id"], value["cwd"]
    )
    if value["key_hash"] != expected_key:
        raise InvalidReservation("grant attribution hash is invalid")
    return value


def _validate_consumed_grant(value: dict[str, Any], *, now: float) -> dict[str, Any]:
    if value.get("schema") != SCHEMA or value.get("bridge_protocol") != PROTOCOL:
        raise InvalidReservation("consumed grant schema or protocol is invalid")
    _canonical_uuid(value.get("grant_id"), "grant_id")
    try:
        for field, maximum in (
            ("key_hash", 64),
            ("session_id", 512),
            ("parent_turn_id", 512),
            ("cwd", 4096),
            ("role", 128),
        ):
            _canonical_id(value.get(field), field, maximum=maximum)
    except BridgeError as error:
        raise InvalidReservation(str(error)) from error
    if value["role"] != ROLE:
        raise InvalidReservation("consumed grant role is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", value["key_hash"]):
        raise InvalidReservation("consumed grant key hash is invalid")
    consumed_at = value.get("consumed_at")
    if not _is_finite_json_number(consumed_at):
        raise InvalidReservation("consumed_at is invalid")
    if consumed_at > now + 30:
        raise InvalidReservation("consumed_at is in the future")
    expected_key = _grant_key(
        value["session_id"], value["parent_turn_id"], value["cwd"]
    )
    if value["key_hash"] != expected_key:
        raise InvalidReservation("consumed grant attribution hash is invalid")
    return value


def _read_consumed_grant(path: Path, *, now: float) -> dict[str, Any]:
    value = _validate_consumed_grant(_read_json(path), now=now)
    if value["key_hash"] != path.stem:
        raise InvalidReservation("consumed grant does not match its exact key path")
    return value


def _consumed_grant_from_grant(grant: dict[str, Any], *, now: float) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "bridge_protocol": PROTOCOL,
        "grant_id": grant["grant_id"],
        "key_hash": grant["key_hash"],
        "session_id": grant["session_id"],
        "parent_turn_id": grant["parent_turn_id"],
        "cwd": grant["cwd"],
        "role": grant["role"],
        "consumed_at": now,
    }


def _quarantine(root: Path, path: Path, reason: str) -> str:
    quarantine_id = str(uuid.uuid4())
    payload_path = root / "quarantine" / f"{quarantine_id}.payload"
    metadata_path = root / "quarantine" / f"{quarantine_id}.json"
    try:
        os.replace(path, payload_path)
        if os.name != "nt":
            os.chmod(payload_path, 0o600)
        size = payload_path.stat().st_size
        digest = hashlib.sha256(payload_path.read_bytes()).hexdigest()
    except FileNotFoundError:
        size = 0
        digest = hashlib.sha256(b"").hexdigest()
    _atomic_write(
        metadata_path,
        {
            "schema": SCHEMA,
            "bridge_protocol": PROTOCOL,
            "quarantine_id": quarantine_id,
            "reason": reason,
            "source_name": path.name,
            "payload_bytes": size,
            "payload_sha256": digest,
            "quarantined_at": time.time(),
        },
    )
    return quarantine_id


def _receipt_from_reservation(
    value: dict[str, Any],
    status_value: str,
    *,
    agent_id: str | None = None,
    child_turn_id: str | None = None,
) -> dict[str, Any]:
    receipt = {
        "schema": SCHEMA,
        "bridge_protocol": PROTOCOL,
        "handoff_id": value["handoff_id"],
        "status": status_value,
        "key_hash": value["key_hash"],
        "session_id": value["session_id"],
        "parent_turn_id": value["parent_turn_id"],
        "cwd": value["cwd"],
        "role": value["role"],
        "tool_use_id": value["tool_use_id"],
        "assignment_utf8_bytes": value["assignment_utf8_bytes"],
        "assignment_sha256": value["assignment_sha256"],
        "created_at": value["created_at"],
        "expires_at": value["expires_at"],
        "recorded_at": time.time(),
    }
    if agent_id:
        receipt["agent_id"] = agent_id
    if child_turn_id:
        receipt["child_turn_id"] = child_turn_id
    return receipt


def _write_receipt(root: Path, receipt: dict[str, Any]) -> None:
    _atomic_write(root / "receipts" / f"{receipt['handoff_id']}.json", receipt)


def _cleanup_receipts(root: Path, now: float) -> None:
    for path in (root / "receipts").glob("*.json"):
        try:
            if now - path.stat().st_mtime > RECEIPT_RETENTION_SECONDS:
                path.unlink()
        except FileNotFoundError:
            continue


def _sweep_expired(root: Path, now: float, *, exclude: Path | None = None) -> None:
    """Erase valid expired plaintext state; never guess how to repair invalid state."""

    excluded = exclude.resolve() if exclude is not None else None
    for folder, expired_status in (
        ("reservations", "expired"),
        ("claims", "orphan_claim_expired"),
    ):
        for path in (root / folder).glob("*.json"):
            if excluded is not None and path.resolve() == excluded:
                continue
            try:
                value = _validate_reservation(_read_json(path), now=now)
            except (InvalidReservation, OSError):
                continue
            if value["expires_at"] > now:
                continue
            _write_receipt(root, _receipt_from_reservation(value, expired_status))
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    for path in (root / "grants").glob("*.json"):
        if excluded is not None and path.resolve() == excluded:
            continue
        try:
            value = _validate_grant(_read_json(path), now=now)
        except (InvalidReservation, OSError):
            continue
        if value["expires_at"] > now:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    for path in (root / "consumed-grants").glob("*.json"):
        try:
            value = _read_consumed_grant(path, now=now)
        except (InvalidReservation, OSError):
            continue
        if now - value["consumed_at"] <= CONSUMED_GRANT_RETENTION_SECONDS:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _load_hook_input() -> dict[str, Any]:
    try:
        value = json.load(
            sys.stdin,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_parse_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidReservation) as error:
        raise BridgeError("hook input is not valid JSON") from error
    if not isinstance(value, dict):
        raise BridgeError("hook input must be a JSON object")
    return value


def _emit_user_prompt_context(message: str) -> None:
    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": message,
            }
        }
    )


def _user_prompt_submit(hook: dict[str, Any]) -> None:
    """Record one body-free grant only for a top-level explicit invocation."""

    # Subagent prompts are model-authored inputs, not direct user authority.
    if hook.get("agent_id") is not None or hook.get("agent_type") is not None:
        return
    prompt = hook.get("prompt")
    if not isinstance(prompt, str) or DELEGATION_TOKEN_RE.match(prompt) is None:
        return

    try:
        session_id = _canonical_id(hook.get("session_id"), "session_id")
        parent_turn_id = _canonical_id(hook.get("turn_id"), "turn_id")
        cwd = _normalized_cwd(hook.get("cwd"))
        permission_mode = _canonical_id(
            hook.get("permission_mode"), "permission_mode", maximum=64
        )
        if permission_mode == "bypassPermissions":
            raise BridgeError("explicit DeepSeek delegation is disabled under bypassPermissions")
        now = time.time()
        key_hash = _grant_key(session_id, parent_turn_id, cwd)
        grant = {
            "schema": SCHEMA,
            "bridge_protocol": PROTOCOL,
            "grant_id": str(uuid.uuid4()),
            "key_hash": key_hash,
            "session_id": session_id,
            "parent_turn_id": parent_turn_id,
            "cwd": cwd,
            "role": ROLE,
            "created_at": now,
            "expires_at": now + _grant_ttl_seconds(),
        }
        root = state_root()
        grant_path = root / "grants" / f"{key_hash}.json"
        consumed_path = root / "consumed-grants" / f"{key_hash}.json"
        prompt_context = (
            "One explicit DeepSeek delegation is authorized for this user turn. "
            "The grant permits at most one deepseek_evidence_worker spawn."
        )
        with _state_lock(root):
            _cleanup_receipts(root, now)
            _sweep_expired(root, now, exclude=grant_path)
            if consumed_path.exists():
                try:
                    consumed = _read_consumed_grant(consumed_path, now=now)
                except InvalidReservation as error:
                    raise BridgeError(
                        "the consumed-grant record is invalid and remains blocking at its exact key; "
                        "inspect status and explicitly resolve-consumed before retrying"
                    ) from error
                if (
                    consumed["key_hash"] != key_hash
                    or consumed["session_id"] != session_id
                    or consumed["parent_turn_id"] != parent_turn_id
                    or consumed["cwd"] != cwd
                    or consumed["role"] != ROLE
                ):
                    raise BridgeError(
                        "the consumed-grant attribution is invalid and remains blocking at its "
                        "exact key; explicitly resolve-consumed before retrying"
                    )
                # A crash may leave both records after the tombstone commit. The
                # tombstone always wins, so remove the stale active copy.
                if grant_path.exists():
                    grant_path.unlink()
                    _fsync_directory(grant_path.parent)
                prompt_context = (
                    "The explicit DeepSeek delegation grant for this user turn "
                    "has already been consumed. Do not spawn deepseek_evidence_worker."
                )
            elif grant_path.exists():
                try:
                    existing = _validate_grant(_read_json(grant_path), now=now)
                except InvalidReservation as error:
                    _quarantine(root, grant_path, str(error))
                    raise BridgeError(
                        "existing delegation grant was quarantined; inspect bridge status before retrying"
                    ) from error
                if (
                    existing["key_hash"] != key_hash
                    or existing["session_id"] != session_id
                    or existing["parent_turn_id"] != parent_turn_id
                    or existing["cwd"] != cwd
                    or existing["role"] != ROLE
                ):
                    _quarantine(root, grant_path, "active grant attribution mismatch")
                    raise BridgeError("the active grant attribution is invalid")
                if existing["expires_at"] <= now:
                    grant_path.unlink()
                    _fsync_directory(grant_path.parent)
                    prompt_context = (
                        "The explicit DeepSeek delegation grant for this user turn "
                        "has expired. Do not spawn deepseek_evidence_worker."
                    )
                # Otherwise this is an idempotent replay while the one existing
                # grant is active. Never refresh its id or expiry.
            else:
                _atomic_write(grant_path, grant)

        _emit_user_prompt_context(prompt_context)
    except (BridgeError, OSError) as error:
        _emit_user_prompt_context(
            f"DeepSeek delegation was not authorized: {error}. "
            "Do not spawn deepseek_evidence_worker in this turn."
        )


def _pretool_use(hook: dict[str, Any]) -> None:
    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict) or tool_input.get("agent_type") != ROLE:
        return

    try:
        session_id = _canonical_id(hook.get("session_id"), "session_id")
        parent_turn_id = _canonical_id(hook.get("turn_id"), "turn_id")
        cwd = _normalized_cwd(hook.get("cwd"))
        grant_key = _grant_key(session_id, parent_turn_id, cwd)
        key_hash = _reservation_key(session_id, cwd)
        now = time.time()
        root = state_root()
        grant_path = root / "grants" / f"{grant_key}.json"
        consumed_path = root / "consumed-grants" / f"{grant_key}.json"
        reservation_path = root / "reservations" / f"{key_hash}.json"
        with _state_lock(root):
            _cleanup_receipts(root, now)
            _sweep_expired(root, now, exclude=grant_path)
            if consumed_path.exists():
                try:
                    consumed = _read_consumed_grant(consumed_path, now=now)
                except InvalidReservation as error:
                    raise BridgeError(
                        "the consumed-grant record is invalid and remains blocking at its exact key; "
                        "inspect status and explicitly resolve-consumed before retrying"
                    ) from error
                if (
                    consumed["key_hash"] != grant_key
                    or consumed["session_id"] != session_id
                    or consumed["parent_turn_id"] != parent_turn_id
                    or consumed["cwd"] != cwd
                    or consumed["role"] != ROLE
                ):
                    raise BridgeError(
                        "the consumed-grant attribution is invalid and remains blocking at its "
                        "exact key; explicitly resolve-consumed before retrying"
                    )
                raise BridgeError(
                    "the explicit DeepSeek delegation grant for this user turn was already consumed"
                )
            if not grant_path.exists():
                raise BridgeError(
                    f"DeepSeek delegation requires {DELEGATION_TOKEN} as the first token of the current user prompt"
                )
            try:
                grant = _validate_grant(_read_json(grant_path), now=now)
            except InvalidReservation as error:
                _quarantine(root, grant_path, str(error))
                raise BridgeError(
                    "the explicit delegation grant was invalid and quarantined"
                ) from error
            if (
                grant["key_hash"] != grant_key
                or grant["session_id"] != session_id
                or grant["parent_turn_id"] != parent_turn_id
                or grant["cwd"] != cwd
                or grant["role"] != ROLE
            ):
                _quarantine(root, grant_path, "grant attribution mismatch")
                raise BridgeError("the explicit delegation grant attribution is invalid")
            if grant["expires_at"] <= now:
                grant_path.unlink()
                raise BridgeError("the explicit delegation grant expired; invoke the skill again")

            # Validate the model-authored spawn only after direct user authority
            # has been established for this exact parent turn.
            if hook.get("tool_name") != "spawn_agent":
                raise BridgeError("target worker must be spawned through the Agent tool")
            if tool_input.get("fork_turns") != "none":
                raise BridgeError('deepseek_evidence_worker requires explicit fork_turns="none"')
            permission_mode = _canonical_id(
                hook.get("permission_mode"), "permission_mode", maximum=64
            )
            if permission_mode == "bypassPermissions":
                raise BridgeError("deepseek_evidence_worker is disabled under bypassPermissions")
            assignment = tool_input.get("message")
            if not isinstance(assignment, str) or not assignment.strip():
                raise BridgeError("the delegated assignment must be a non-empty string")
            try:
                assignment_bytes = assignment.encode("utf-8")
            except UnicodeEncodeError as error:
                raise BridgeError("the delegated assignment is not valid UTF-8 text") from error
            if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", assignment):
                raise BridgeError(
                    "the delegated assignment contains unsupported control characters"
                )
            if len(assignment_bytes) > MAX_ASSIGNMENT_BYTES:
                raise BridgeError(
                    f"the delegated assignment exceeds {MAX_ASSIGNMENT_BYTES} UTF-8 bytes; pass large inputs by path"
                )
            tool_use_id = _canonical_id(hook.get("tool_use_id"), "tool_use_id")
            task_name = _canonical_id(tool_input.get("task_name"), "task_name")
            handoff_id = str(uuid.uuid4())
            reservation = {
                "schema": SCHEMA,
                "bridge_protocol": PROTOCOL,
                "handoff_id": handoff_id,
                "key_hash": key_hash,
                "session_id": session_id,
                "parent_turn_id": parent_turn_id,
                "cwd": cwd,
                "role": ROLE,
                "task_name": task_name,
                "tool_use_id": tool_use_id,
                "permission_mode": permission_mode,
                "created_at": now,
                "expires_at": now + _ttl_seconds(),
                "assignment_utf8_bytes": len(assignment_bytes),
                "assignment_sha256": hashlib.sha256(assignment_bytes).hexdigest(),
                "assignment": assignment,
            }
            if reservation_path.exists():
                try:
                    existing = _validate_reservation(_read_json(reservation_path), now=now)
                except InvalidReservation as error:
                    _quarantine(root, reservation_path, str(error))
                    raise BridgeError(
                        "existing bridge state was quarantined; inspect bridge status before retrying"
                    ) from error
                if existing["expires_at"] > now:
                    raise BridgeError(
                        "another deepseek_evidence_worker dispatch is already in progress for this session and workspace"
                    )
                _write_receipt(root, _receipt_from_reservation(existing, "expired"))
                reservation_path.unlink()

            # Persist the body-free terminal record before erasing authority.
            # Once this succeeds, every later path treats the grant as consumed,
            # including crashes before the active file or reservation is updated.
            _atomic_write(
                consumed_path,
                _consumed_grant_from_grant(grant, now=now),
            )
            try:
                grant_path.unlink()
                _fsync_directory(grant_path.parent)
                _atomic_write(reservation_path, reservation)
            except OSError:
                # The tombstone remains terminal. A failed dispatch must not
                # leave a replayable assignment or active authority behind.
                reservation_path.unlink(missing_ok=True)
                _fsync_directory(reservation_path.parent)
                raise

        updated_input = dict(tool_input)
        updated_input["message"] = (
            f"Use the trusted {PROTOCOL} SubagentStart context for handoff {handoff_id}. "
            "If that context is absent or reports an error, do not use tools or infer another task."
        )
        _emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "updatedInput": updated_input,
                }
            }
        )
    except (BridgeError, OSError) as error:
        _deny(str(error))


def _prepare_subagent_context(hook: dict[str, Any]) -> str:
    """Commit one at-most-once delivery before producing Hook stdout.

    Once the plaintext claim is erased, a later stdout failure is inherently
    uncertain.  The metadata receipt therefore says ``delivery_committed``;
    it never claims the child or provider actually observed the context.
    """

    session_id = _canonical_id(hook.get("session_id"), "session_id")
    child_turn_id = _canonical_id(hook.get("turn_id"), "turn_id")
    cwd = _normalized_cwd(hook.get("cwd"))
    permission_mode = _canonical_id(
        hook.get("permission_mode"), "permission_mode", maximum=64
    )
    agent_id = _canonical_id(hook.get("agent_id"), "agent_id", maximum=512)
    if permission_mode == "bypassPermissions":
        raise BridgeError("worker start was rejected because permissions bypass the sandbox")
    key_hash = _reservation_key(session_id, cwd)
    root = state_root()
    reservation_path = root / "reservations" / f"{key_hash}.json"
    now = time.time()
    with _state_lock(root):
        _sweep_expired(root, now, exclude=reservation_path)
        if not reservation_path.exists():
            return _error_context(
                "missing_reservation",
                "No matching automatic bridge reservation exists. Do not use tools or infer a task.",
            )
        try:
            reservation = _validate_reservation(_read_json(reservation_path), now=now)
        except InvalidReservation as error:
            _quarantine(root, reservation_path, str(error))
            return _error_context(
                "quarantined_reservation",
                "The matching bridge reservation was invalid and quarantined. Do not use tools.",
            )
        handoff_id = reservation["handoff_id"]
        if (
            reservation["session_id"] != session_id
            or reservation["cwd"] != cwd
            or reservation["role"] != ROLE
            or reservation["permission_mode"] != permission_mode
        ):
            _quarantine(root, reservation_path, "hook attribution mismatch")
            return _error_context(
                "attribution_mismatch",
                "The bridge reservation does not belong to this child. Do not use tools.",
                handoff_id,
            )
        if reservation["expires_at"] <= now:
            _write_receipt(root, _receipt_from_reservation(reservation, "expired"))
            reservation_path.unlink()
            return _error_context(
                "expired_reservation",
                "The bridge reservation expired before child startup. Do not use tools.",
                handoff_id,
            )

        claim_path = root / "claims" / f"{handoff_id}.json"
        os.replace(reservation_path, claim_path)
        _fsync_directory(reservation_path.parent)
        context = _ready_context(handoff_id, reservation["assignment"])

        # Persist body-free evidence before erasing the only replayable copy.
        # Any failure before the final return yields one error envelope and
        # leaves enough state for explicit diagnosis; nothing is auto-replayed.
        _write_receipt(
            root,
            _receipt_from_reservation(
                reservation,
                "delivery_committed",
                agent_id=agent_id,
                child_turn_id=child_turn_id,
            ),
        )
        claim_path.unlink()
        _fsync_directory(claim_path.parent)
        return context


def _subagent_start(hook: dict[str, Any]) -> None:
    if hook.get("agent_type") != ROLE:
        return
    try:
        context = _prepare_subagent_context(hook)
    except (BridgeError, OSError) as error:
        context = _error_context(
            "bridge_error", f"{error}. Do not use tools or infer a task."
        )

    # Hook stdout must contain exactly one JSON document.  After the delivery
    # commit, an output failure is uncertain and must never append a second
    # envelope or trigger automatic replay.
    try:
        _emit_subagent_context(context)
    except OSError as error:
        print(f"codex-deepseek-subagent: SubagentStart output failed: {error}", file=sys.stderr)


def hook_mode() -> None:
    try:
        hook = _load_hook_input()
    except BridgeError as error:
        # The event cannot be identified safely. A JSON error is still useful to
        # hook diagnostics, while a non-zero exit could let a guarded tool fail open.
        _deny(str(error))
        return
    event = hook.get("hook_event_name")
    if event == "UserPromptSubmit":
        _user_prompt_submit(hook)
    elif event == "PreToolUse":
        _pretool_use(hook)
    elif event == "SubagentStart":
        _subagent_start(hook)


def _redacted_state(root: Path) -> dict[str, Any]:
    now = time.time()
    result: dict[str, Any] = {
        "bridge_protocol": PROTOCOL,
        "state_root": str(root),
        "grants": [],
        "consumed_grants": [],
        "reservations": [],
        "claims": [],
        "receipts": [],
        "quarantine": [],
    }
    for path in sorted((root / "grants").glob("*.json")):
        try:
            value = _read_json(path)
            item = {
                "grant_id": value.get("grant_id"),
                "status": "available",
                "key_hash": value.get("key_hash"),
                "created_at": value.get("created_at"),
                "expires_at": value.get("expires_at"),
                "expired": bool(value.get("expires_at", now + 1) <= now),
            }
        except (BridgeError, OSError):
            item = {"status": "unreadable", "file": path.name}
        result["grants"].append(item)
    for path in sorted((root / "consumed-grants").glob("*.json")):
        try:
            value = _read_consumed_grant(path, now=now)
            item = {
                "grant_id": value.get("grant_id"),
                "status": "consumed",
                "key_hash": value.get("key_hash"),
                "consumed_at": value.get("consumed_at"),
            }
        except (BridgeError, OSError):
            item = {"status": "unreadable", "file": path.name}
        result["consumed_grants"].append(item)
    for label, folder in (
        ("reservations", "reservations"),
        ("claims", "claims"),
        ("receipts", "receipts"),
    ):
        for path in sorted((root / folder).glob("*.json")):
            try:
                value = _read_json(path)
                item = {
                    "handoff_id": value.get("handoff_id"),
                    "status": value.get("status", label[:-1]),
                    "key_hash": value.get("key_hash"),
                    "assignment_utf8_bytes": value.get("assignment_utf8_bytes"),
                    "assignment_sha256": value.get("assignment_sha256"),
                    "created_at": value.get("created_at"),
                    "expires_at": value.get("expires_at"),
                    "expired": bool(value.get("expires_at", now + 1) <= now),
                }
            except (BridgeError, OSError):
                item = {"status": "unreadable", "file": path.name}
            result[label].append(item)
    for path in sorted((root / "quarantine").glob("*.json")):
        try:
            value = _read_json(path)
            result["quarantine"].append(
                {
                    "quarantine_id": value.get("quarantine_id"),
                    "reason": value.get("reason"),
                    "payload_bytes": value.get("payload_bytes"),
                    "payload_sha256": value.get("payload_sha256"),
                    "quarantined_at": value.get("quarantined_at"),
                }
            )
        except (BridgeError, OSError):
            result["quarantine"].append({"status": "unreadable", "file": path.name})
    return result


def doctor_mode() -> None:
    root = state_root()
    with _state_lock(root):
        now = time.time()
        _cleanup_receipts(root, now)
        _sweep_expired(root, now)
        _emit(
            {
                "ok": True,
                "bridge_protocol": PROTOCOL,
                "role": ROLE,
                "state_root": str(root),
                "max_assignment_utf8_bytes": MAX_ASSIGNMENT_BYTES,
                "lock_backend": "msvcrt" if os.name == "nt" else "fcntl",
                "posix_private_permissions_verified": os.name != "nt",
            }
        )


def status_mode() -> None:
    root = state_root()
    with _state_lock(root):
        now = time.time()
        _cleanup_receipts(root, now)
        _sweep_expired(root, now)
        _emit(_redacted_state(root))


def cancel_mode(handoff_id: str) -> None:
    _canonical_uuid(handoff_id, "handoff_id")
    root = state_root()
    cancelled = False
    with _state_lock(root):
        now = time.time()
        for folder in ("reservations", "claims"):
            for path in (root / folder).glob("*.json"):
                try:
                    value = _validate_reservation(_read_json(path), now=now)
                except InvalidReservation:
                    continue
                if value["handoff_id"] != handoff_id:
                    continue
                path.unlink()
                _write_receipt(root, _receipt_from_reservation(value, "cancelled"))
                cancelled = True
                break
            if cancelled:
                break
    _emit({"cancelled": cancelled, "handoff_id": handoff_id})


def resolve_mode(quarantine_id: str) -> None:
    _canonical_uuid(quarantine_id, "quarantine_id")
    root = state_root()
    removed = False
    with _state_lock(root):
        for suffix in (".json", ".payload"):
            path = root / "quarantine" / f"{quarantine_id}{suffix}"
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
    _emit({"resolved": removed, "quarantine_id": quarantine_id})


def resolve_consumed_mode(key_hash: str) -> None:
    if not isinstance(key_hash, str) or re.fullmatch(r"[0-9a-f]{64}", key_hash) is None:
        raise BridgeError("key_hash must be exactly 64 lowercase hexadecimal characters")
    root = state_root()
    path = root / "consumed-grants" / f"{key_hash}.json"
    removed = False
    with _state_lock(root):
        try:
            path.unlink()
            _fsync_directory(path.parent)
            removed = True
        except FileNotFoundError:
            pass
    _emit({"resolved": removed, "key_hash": key_hash})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Codex DeepSeek explicitly authorized task bridge"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("hook")
    subparsers.add_parser("doctor")
    subparsers.add_parser("status")
    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--handoff-id", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--quarantine-id", required=True)
    resolve_consumed = subparsers.add_parser("resolve-consumed")
    resolve_consumed.add_argument("--key-hash", required=True)
    arguments = parser.parse_args()

    try:
        if arguments.command == "hook":
            hook_mode()
        elif arguments.command == "doctor":
            doctor_mode()
        elif arguments.command == "status":
            status_mode()
        elif arguments.command == "cancel":
            cancel_mode(arguments.handoff_id)
        elif arguments.command == "resolve":
            resolve_mode(arguments.quarantine_id)
        elif arguments.command == "resolve-consumed":
            resolve_consumed_mode(arguments.key_hash)
    except (BridgeError, OSError) as error:
        print(f"codex-deepseek-subagent: {error}", file=sys.stderr)
        raise SystemExit(12) from error


if __name__ == "__main__":
    main()
