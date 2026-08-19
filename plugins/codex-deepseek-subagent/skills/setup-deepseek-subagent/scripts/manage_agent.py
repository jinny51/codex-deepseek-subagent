#!/usr/bin/env python3
"""Manage the single Codex Agent file owned by Codex DeepSeek Subagent."""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    print("manage_agent.py requires Python 3.10 or newer.", file=sys.stderr)
    raise SystemExit(2)

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


OWNER = "codex-deepseek-subagent"
ROLE_NAME = "deepseek_evidence_worker"
TARGET_NAME = "deepseek-evidence-worker.toml"
MANAGED_MARKER = "# codex-deepseek-subagent: managed-agent/v1"
MANIFEST_PREFIX = "# codex-deepseek-subagent-ownership: "
TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "assets" / TARGET_NAME


class SetupFailure(RuntimeError):
    """An expected, safely reportable setup failure."""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_template() -> str:
    try:
        value = TEMPLATE_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SetupFailure(f"cannot read Agent template: {error}") from error
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not value.endswith("\n"):
        value += "\n"
    validate_agent_payload(value)
    return value


def validate_agent_payload(payload: str) -> None:
    table_marker = "[model_providers.deepseek]"
    if payload.count(table_marker) != 1:
        raise SetupFailure("Agent template must contain one local DeepSeek provider table")
    agent_section, provider_section = payload.split(table_marker, 1)

    expected_agent_fields = {
        "name": ROLE_NAME,
        "model_provider": "deepseek",
        "model": "deepseek-v4-flash",
        "sandbox_mode": "read-only",
    }
    for field, value in expected_agent_fields.items():
        require_string_field(agent_section, field, value)

    expected_provider_fields = {
        "base_url": "https://api.deepseek.com",
        "wire_api": "responses",
        "env_key": "DEEPSEEK_API_KEY",
    }
    for field, value in expected_provider_fields.items():
        require_string_field(provider_section, field, value)

    if "[" in provider_section:
        raise SetupFailure("Agent template has an unexpected trailing TOML table")
    if "auth" in provider_section or "experimental_bearer_token" in provider_section:
        raise SetupFailure("Agent template contains an unsupported credential source")
    if 'developer_instructions = """' not in agent_section:
        raise SetupFailure("Agent template is missing developer instructions")
    for required_text in ("codex-deepseek-subagent/v1", "transport_error", "do not call a tool"):
        if required_text not in agent_section:
            raise SetupFailure("Agent template lacks its fail-closed bridge contract")


def require_string_field(section: str, field: str, expected: str) -> None:
    pattern = re.compile(rf'^\s*{re.escape(field)}\s*=\s*"([^"\n]*)"\s*$', re.MULTILINE)
    matches = pattern.findall(section)
    if matches != [expected]:
        raise SetupFailure(f"Agent template has an invalid {field}")


def ownership_manifest(payload: str) -> dict[str, Any]:
    return {
        "artifact": TARGET_NAME,
        "owner": OWNER,
        "payload_sha256": sha256_text(payload),
        "role": ROLE_NAME,
        "schema": 1,
    }


def render_managed_file(payload: str) -> str:
    manifest = json.dumps(
        ownership_manifest(payload), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return f"{MANAGED_MARKER}\n{MANIFEST_PREFIX}{manifest}\n\n{payload}"


def inspect_target(target: Path, expected_payload: str) -> dict[str, Any]:
    expected_hash = sha256_text(expected_payload)
    if target.is_symlink():
        return {"state": "conflict", "detail": "target is a symbolic link"}
    if not target.exists():
        return {"state": "missing"}
    if not target.is_file():
        return {"state": "conflict", "detail": "target is not a regular file"}

    try:
        content = target.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        return {"state": "conflict", "detail": f"target cannot be read: {error}"}

    parts = content.split("\n", 3)
    if not parts or parts[0] != MANAGED_MARKER:
        return {"state": "conflict", "detail": "target is not managed by this plugin"}
    if len(parts) != 4 or not parts[1].startswith(MANIFEST_PREFIX) or parts[2] != "":
        return {"state": "conflict", "detail": "managed header is malformed"}

    try:
        manifest = json.loads(parts[1][len(MANIFEST_PREFIX) :])
    except json.JSONDecodeError:
        return {"state": "conflict", "detail": "ownership manifest is invalid JSON"}
    if not isinstance(manifest, dict):
        return {"state": "conflict", "detail": "ownership manifest is not an object"}

    required = {
        "artifact": TARGET_NAME,
        "owner": OWNER,
        "role": ROLE_NAME,
        "schema": 1,
    }
    if any(manifest.get(field) != value for field, value in required.items()):
        return {"state": "conflict", "detail": "ownership manifest identity does not match"}
    recorded_hash = manifest.get("payload_sha256")
    if not isinstance(recorded_hash, str) or re.fullmatch(r"[0-9a-f]{64}", recorded_hash) is None:
        return {"state": "conflict", "detail": "ownership manifest hash is invalid"}

    payload = parts[3]
    actual_hash = sha256_text(payload)
    if actual_hash != recorded_hash:
        return {
            "state": "modified",
            "detail": "managed payload differs from its recorded SHA-256",
            "installed_sha256": actual_hash,
            "recorded_sha256": recorded_hash,
        }
    if actual_hash == expected_hash:
        return {"state": "current", "installed_sha256": actual_hash}
    return {"state": "outdated", "installed_sha256": actual_hash}


def resolve_codex_home(explicit: str | None) -> Path:
    if explicit:
        home = Path(explicit).expanduser()
    elif "CODEX_HOME" in os.environ:
        configured_home = os.environ["CODEX_HOME"]
        if not configured_home.strip():
            raise SetupFailure("CODEX_HOME is set but empty")
        home = Path(configured_home).expanduser()
    else:
        home = Path.home() / ".codex"
    return home.resolve(strict=False)


def secret_status() -> str:
    return "present" if "DEEPSEEK_API_KEY" in os.environ else "missing"


def atomic_write(target: Path, content: str) -> None:
    parent = target.parent
    if parent.is_symlink():
        raise SetupFailure("agents directory is a symbolic link")
    if parent.exists() and not parent.is_dir():
        raise SetupFailure("agents path is not a directory")
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{TARGET_NAME}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        fsync_directory(parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def emit(command: str, target: Path, status: str, **extra: Any) -> None:
    result: dict[str, Any] = {
        "command": command,
        "key_status": secret_status(),
        "status": status,
        "target": str(target),
    }
    result.update(extra)
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def run_plan(target: Path, payload: str) -> int:
    inspection = inspect_target(target, payload)
    action = {
        "missing": "create",
        "current": "none",
        "outdated": "update",
        "modified": "refuse",
        "conflict": "refuse",
    }[inspection["state"]]
    emit("plan", target, "ok", action=action, inspection=inspection)
    return 0


def run_install(target: Path, payload: str) -> int:
    inspection = inspect_target(target, payload)
    state = inspection["state"]
    if state in {"modified", "conflict"}:
        emit("install", target, "refused", inspection=inspection)
        return 2
    if state == "current":
        emit("install", target, "unchanged", inspection=inspection)
        return 0

    atomic_write(target, render_managed_file(payload))
    final = inspect_target(target, payload)
    if final["state"] != "current":
        raise SetupFailure("post-install verification failed")
    emit(
        "install",
        target,
        "installed" if state == "missing" else "updated",
        inspection=final,
    )
    return 0


def run_doctor(target: Path, payload: str) -> int:
    inspection = inspect_target(target, payload)
    ready = inspection["state"] == "current" and secret_status() == "present"
    emit("doctor", target, "healthy" if ready else "attention", inspection=inspection)
    return 0 if ready else 1


def run_uninstall(target: Path, payload: str) -> int:
    inspection = inspect_target(target, payload)
    state = inspection["state"]
    if state == "missing":
        emit("uninstall", target, "not-installed", inspection=inspection)
        return 0
    if state in {"modified", "conflict"}:
        emit("uninstall", target, "refused", inspection=inspection)
        return 2

    try:
        target.unlink()
    except OSError as error:
        raise SetupFailure(f"cannot remove managed Agent file: {error}") from error
    fsync_directory(target.parent)
    emit("uninstall", target, "removed", previous_state=state)
    return 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the Codex DeepSeek Subagent evidence worker Agent file."
    )
    parser.add_argument("command", choices=("plan", "install", "doctor", "uninstall"))
    parser.add_argument(
        "--codex-home",
        help="Override CODEX_HOME for isolated testing or an explicit Codex installation.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    codex_home = resolve_codex_home(arguments.codex_home)
    target = codex_home / "agents" / TARGET_NAME
    try:
        payload = canonical_template()
        if arguments.command == "plan":
            return run_plan(target, payload)
        if arguments.command == "install":
            return run_install(target, payload)
        if arguments.command == "doctor":
            return run_doctor(target, payload)
        return run_uninstall(target, payload)
    except SetupFailure as error:
        emit(arguments.command, target, "error", detail=str(error))
        return 2
    except OSError as error:
        emit(arguments.command, target, "error", detail=f"filesystem operation failed: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
