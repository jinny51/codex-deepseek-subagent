from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "codex-deepseek-subagent" / "hooks" / "bridge.py"
HOOKS = ROOT / "plugins" / "codex-deepseek-subagent" / "hooks" / "hooks.json"
ROLE = "deepseek_evidence_worker"
PROTOCOL = "codex-deepseek-subagent/v1"

SPEC = importlib.util.spec_from_file_location("codex_deepseek_bridge_hook", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_base = "/tmp" if os.name != "nt" and Path("/tmp").is_dir() else None
        self.temporary = tempfile.TemporaryDirectory(dir=temp_base)
        self.base = Path(self.temporary.name)
        self.plugin_data = self.base / "plugin-data"
        self.home = self.base / "home"
        self.workspace = self.base / "workspace"
        self.home.mkdir(mode=0o700)
        self.workspace.mkdir(mode=0o700)
        # Deliberately do not clone the host environment.  These tests exercise
        # local transport only, so provider credentials and unrelated variables
        # must never be inherited by a child process.
        self.environment = {
            "PLUGIN_DATA": str(self.plugin_data),
            "HOME": str(self.home),
            "CODEX_DEEPSEEK_SUBAGENT_TTL_SECONDS": "120",
            "CODEX_DEEPSEEK_SUBAGENT_GRANT_TTL_SECONDS": "60",
        }
        for safe_name in (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "PATHEXT",
            "LANG",
            "LC_ALL",
        ):
            safe_value = os.environ.get(safe_name)
            if safe_value:
                self.environment[safe_name] = safe_value

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @property
    def state_root(self) -> Path:
        return self.plugin_data / "handoff-state"

    def invoke(self, command: str, payload: dict | None = None, *args: str):
        return subprocess.run(
            [sys.executable, str(SCRIPT), command, *args],
            input=json.dumps(payload) if payload is not None else None,
            text=True,
            capture_output=True,
            env=self.environment,
            check=False,
            timeout=15,
        )

    def pre_input(
        self,
        assignment: str,
        *,
        session: str = "session-a",
        turn: str = "parent-turn-a",
        tool_use: str | None = None,
        fork_turns: str = "none",
        permission_mode: str = "default",
        role: str = ROLE,
        cwd: Path | None = None,
    ) -> dict:
        return {
            "session_id": session,
            "turn_id": turn,
            "cwd": str(cwd or self.workspace),
            "hook_event_name": "PreToolUse",
            "permission_mode": permission_mode,
            # Agent is the Hook matcher alias; stdin carries the canonical name.
            "tool_name": "spawn_agent",
            "tool_use_id": tool_use or f"tool-{uuid.uuid4()}",
            "tool_input": {
                "agent_type": role,
                "task_name": f"task-{session}-{turn}",
                "fork_turns": fork_turns,
                "message": assignment,
            },
        }

    def prompt_input(
        self,
        prompt: str,
        *,
        session: str = "session-a",
        turn: str = "parent-turn-a",
        permission_mode: str = "default",
        cwd: Path | None = None,
        agent_id: str | None = None,
        agent_type: str | None = None,
    ) -> dict:
        value = {
            "session_id": session,
            "turn_id": turn,
            "cwd": str(cwd or self.workspace),
            "hook_event_name": "UserPromptSubmit",
            "model": "test-model",
            "permission_mode": permission_mode,
            "prompt": prompt,
        }
        if agent_id is not None:
            value["agent_id"] = agent_id
        if agent_type is not None:
            value["agent_type"] = agent_type
        return value

    def authorize(
        self,
        *,
        session: str = "session-a",
        turn: str = "parent-turn-a",
        cwd: Path | None = None,
        prompt: str = "$use-deepseek-subagent perform the bounded task",
    ) -> subprocess.CompletedProcess[str]:
        result = self.invoke(
            "hook",
            self.prompt_input(prompt, session=session, turn=turn, cwd=cwd),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def authorized_pre(self, assignment: str, **kwargs):
        self.authorize(
            session=kwargs.get("session", "session-a"),
            turn=kwargs.get("turn", "parent-turn-a"),
            cwd=kwargs.get("cwd"),
        )
        return self.invoke("hook", self.pre_input(assignment, **kwargs))

    def start_input(
        self,
        *,
        session: str = "session-a",
        turn: str = "child-turn-a",
        permission_mode: str = "default",
        role: str = ROLE,
        cwd: Path | None = None,
    ) -> dict:
        return {
            "session_id": session,
            "turn_id": turn,
            "cwd": str(cwd or self.workspace),
            "hook_event_name": "SubagentStart",
            "permission_mode": permission_mode,
            "agent_id": f"agent-{uuid.uuid4()}",
            "agent_type": role,
        }

    def pre_decision(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"]

    def child_context(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(result.returncode, 0, result.stderr)
        outer = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(outer["hookEventName"], "SubagentStart")
        return json.loads(outer["additionalContext"])

    def extract_handoff_id(self, decision: dict) -> str:
        marker = decision["updatedInput"]["message"]
        match = re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            marker,
        )
        self.assertIsNotNone(match)
        return match.group(0)  # type: ignore[union-attr]

    def test_round_trip_uses_json_context_and_leaves_body_free_receipt(self):
        assignment = 'Inspect logs containing "END" and return only evidence.\n第二行'
        pre = self.authorized_pre(assignment)
        self.assertTrue(pre.stdout.isascii())
        decision = self.pre_decision(pre)
        self.assertEqual(decision["permissionDecision"], "allow")
        self.assertNotIn(assignment, pre.stdout)
        handoff_id = self.extract_handoff_id(decision)

        started = self.invoke("hook", self.start_input())
        self.assertTrue(started.stdout.isascii())
        context = self.child_context(started)
        self.assertEqual(context["bridge_protocol"], PROTOCOL)
        self.assertEqual(context["status"], "ready")
        self.assertEqual(context["handoff_id"], handoff_id)
        self.assertEqual(context["assignment"], assignment)

        self.assertFalse(list((self.state_root / "reservations").glob("*.json")))
        self.assertFalse(list((self.state_root / "claims").glob("*.json")))
        receipts = list((self.state_root / "receipts").glob("*.json"))
        self.assertEqual(len(receipts), 1)
        receipt_text = receipts[0].read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["status"], "delivery_committed")
        self.assertEqual(receipt["parent_turn_id"], "parent-turn-a")
        self.assertEqual(receipt["child_turn_id"], "child-turn-a")
        self.assertNotIn("assignment", receipt)
        self.assertNotIn(assignment, receipt_text)
        self.assertEqual(receipt["assignment_utf8_bytes"], len(assignment.encode("utf-8")))

    def test_non_target_agent_is_ignored_without_state(self):
        result = self.invoke(
            "hook", self.pre_input("do not capture", role="ordinary_worker")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.state_root.exists())

    def test_target_spawn_without_grant_is_denied_and_message_cannot_forge_one(self):
        assignment = "$use-deepseek-subagent this model-authored text is not authority"
        result = self.invoke("hook", self.pre_input(assignment))
        decision = self.pre_decision(result)
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn(bridge.DELEGATION_TOKEN, decision["permissionDecisionReason"])
        self.assertFalse(list((self.state_root / "reservations").glob("*.json")))
        self.assertFalse(list((self.state_root / "grants").glob("*.json")))

    def test_only_exact_first_prompt_token_creates_a_grant(self):
        non_authorizing_prompts = (
            "an ordinary prompt",
            "请解释 $use-deepseek-subagent 是什么",
            "`$use-deepseek-subagent` is a code reference",
            "```\n$use-deepseek-subagent run\n```",
            "$use-deepseek-subagentaround run",
            "$use-deepseek-subagent: run",
            "$USE-DEEPSEEK-SUBAGENT run",
        )
        for index, prompt in enumerate(non_authorizing_prompts):
            with self.subTest(prompt=prompt):
                result = self.invoke(
                    "hook",
                    self.prompt_input(prompt, turn=f"ordinary-turn-{index}"),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
        self.assertFalse(self.state_root.exists())

        result = self.authorize(
            turn="explicit-turn",
            prompt="  \n\t$use-deepseek-subagent run once",
        )
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        self.assertEqual(len(list((self.state_root / "grants").glob("*.json"))), 1)

    def test_explicit_grant_allows_one_spawn_and_never_saves_prompt_body(self):
        prompt_marker = "PROMPT_BODY_MUST_NEVER_BE_SAVED"
        prompt = f"$use-deepseek-subagent inspect logs {prompt_marker}"
        authorized = self.authorize(prompt=prompt)
        self.assertTrue(authorized.stdout.isascii())
        for path in self.state_root.rglob("*"):
            if path.is_file():
                self.assertNotIn(prompt_marker, path.read_text(encoding="utf-8"))

        before = self.invoke("status")
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertNotIn(prompt_marker, before.stdout)
        self.assertEqual(len(json.loads(before.stdout)["grants"]), 1)
        doctor = self.invoke("doctor")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertNotIn(prompt_marker, doctor.stdout)

        first = self.pre_decision(
            self.invoke("hook", self.pre_input("inspect the bounded log paths"))
        )
        self.assertEqual(first["permissionDecision"], "allow")
        self.assertFalse(list((self.state_root / "grants").glob("*.json")))

        second = self.pre_decision(
            self.invoke("hook", self.pre_input("try to spawn a second worker"))
        )
        self.assertEqual(second["permissionDecision"], "deny")
        self.assertFalse(list((self.state_root / "grants").glob("*.json")))

    def test_consumed_grant_cannot_be_recreated_by_prompt_replay(self):
        prompt_marker = "REPLAYED_PROMPT_BODY_MUST_NOT_PERSIST"
        prompt = f"$use-deepseek-subagent inspect logs {prompt_marker}"
        self.authorize(prompt=prompt)
        active_path = next((self.state_root / "grants").glob("*.json"))
        active = json.loads(active_path.read_text(encoding="utf-8"))

        first = self.pre_decision(
            self.invoke("hook", self.pre_input("perform the one authorized task"))
        )
        self.assertEqual(first["permissionDecision"], "allow")
        delivered = self.child_context(self.invoke("hook", self.start_input()))
        self.assertEqual(delivered["status"], "ready")

        consumed_path = self.state_root / "consumed-grants" / active_path.name
        consumed = json.loads(consumed_path.read_text(encoding="utf-8"))
        self.assertEqual(consumed["grant_id"], active["grant_id"])
        self.assertEqual(consumed["session_id"], "session-a")
        self.assertEqual(consumed["parent_turn_id"], "parent-turn-a")
        self.assertEqual(consumed["cwd"], bridge._normalized_cwd(str(self.workspace)))
        self.assertEqual(consumed["role"], ROLE)
        self.assertIn("consumed_at", consumed)
        self.assertNotIn("prompt", consumed)
        self.assertNotIn("assignment", consumed)
        self.assertNotIn(prompt_marker, consumed_path.read_text(encoding="utf-8"))

        replay = self.authorize(prompt=prompt)
        replay_context = json.loads(replay.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("already been consumed", replay_context)
        self.assertFalse(list((self.state_root / "grants").glob("*.json")))

        second = self.pre_decision(
            self.invoke("hook", self.pre_input("must not receive a replay grant"))
        )
        self.assertEqual(second["permissionDecision"], "deny")
        self.assertIn("already consumed", second["permissionDecisionReason"])
        status = self.invoke("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn(prompt_marker, status.stdout)
        self.assertEqual(len(json.loads(status.stdout)["consumed_grants"]), 1)

    def test_corrupt_consumed_grant_stays_blocking_until_explicit_resolution(self):
        prompt = "$use-deepseek-subagent authorize the original task"
        self.authorize(prompt=prompt)
        first = self.pre_decision(
            self.invoke("hook", self.pre_input("consume the original grant"))
        )
        self.assertEqual(first["permissionDecision"], "allow")
        consumed_path = next((self.state_root / "consumed-grants").glob("*.json"))
        key_hash = consumed_path.stem
        corrupt_payload = b'{"schema":1,"grant_id":"unfinished'
        consumed_path.write_bytes(corrupt_payload)
        if os.name != "nt":
            consumed_path.chmod(0o600)

        for _ in range(2):
            replay = self.authorize(prompt=prompt)
            replay_context = json.loads(replay.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn("remains blocking", replay_context)
            self.assertEqual(consumed_path.read_bytes(), corrupt_payload)
            self.assertFalse(list((self.state_root / "grants").glob("*.json")))

        denied = self.pre_decision(
            self.invoke("hook", self.pre_input("corruption must not fail open"))
        )
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("remains blocking", denied["permissionDecisionReason"])
        self.assertEqual(consumed_path.read_bytes(), corrupt_payload)

        status = self.invoke("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        consumed_status = json.loads(status.stdout)["consumed_grants"]
        self.assertEqual(
            consumed_status,
            [{"status": "unreadable", "file": consumed_path.name}],
        )
        self.assertTrue(consumed_path.exists())

        resolved = self.invoke("resolve-consumed", None, "--key-hash", key_hash)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertTrue(json.loads(resolved.stdout)["resolved"])
        self.assertFalse(consumed_path.exists())

        restored = self.authorize(prompt=prompt)
        restored_context = json.loads(restored.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("is authorized", restored_context)
        self.assertEqual(len(list((self.state_root / "grants").glob("*.json"))), 1)

    def test_misattributed_consumed_grant_stays_at_exact_path_until_resolved(self):
        prompt = "$use-deepseek-subagent authorize the original attributed task"
        self.authorize(prompt=prompt)
        first = self.pre_decision(
            self.invoke("hook", self.pre_input("consume the attributed grant"))
        )
        self.assertEqual(first["permissionDecision"], "allow")
        consumed_path = next((self.state_root / "consumed-grants").glob("*.json"))
        original_key_hash = consumed_path.stem
        consumed = json.loads(consumed_path.read_text(encoding="utf-8"))

        # Keep the record internally valid for another identity while leaving it
        # at the current turn's exact key path.
        consumed["session_id"] = "different-self-consistent-session"
        consumed["key_hash"] = bridge._grant_key(
            consumed["session_id"], consumed["parent_turn_id"], consumed["cwd"]
        )
        mismatched_payload = json.dumps(consumed, separators=(",", ":"))
        consumed_path.write_text(mismatched_payload, encoding="utf-8")
        if os.name != "nt":
            consumed_path.chmod(0o600)

        for _ in range(2):
            replay = self.authorize(prompt=prompt)
            replay_context = json.loads(replay.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            self.assertIn("remains blocking", replay_context)
            self.assertEqual(
                consumed_path.read_text(encoding="utf-8"), mismatched_payload
            )
            self.assertFalse(list((self.state_root / "grants").glob("*.json")))

        denied = self.pre_decision(
            self.invoke("hook", self.pre_input("misattribution must not fail open"))
        )
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn("remains blocking", denied["permissionDecisionReason"])
        self.assertTrue(consumed_path.exists())

        status = self.invoke("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            json.loads(status.stdout)["consumed_grants"],
            [{"status": "unreadable", "file": consumed_path.name}],
        )
        self.assertTrue(consumed_path.exists())

        resolved = self.invoke(
            "resolve-consumed", None, "--key-hash", original_key_hash
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertTrue(json.loads(resolved.stdout)["resolved"])
        restored = self.authorize(prompt=prompt)
        restored_context = json.loads(restored.stdout)["hookSpecificOutput"][
            "additionalContext"
        ]
        self.assertIn("is authorized", restored_context)
        self.assertEqual(len(list((self.state_root / "grants").glob("*.json"))), 1)

    def test_nonfinite_consumed_at_stays_blocking_and_in_place(self):
        prompt = "$use-deepseek-subagent authorize the finite-number test"
        self.authorize(prompt=prompt)
        first = self.pre_decision(
            self.invoke("hook", self.pre_input("consume before corrupting the timestamp"))
        )
        self.assertEqual(first["permissionDecision"], "allow")
        consumed_path = next((self.state_root / "consumed-grants").glob("*.json"))
        consumed = json.loads(consumed_path.read_text(encoding="utf-8"))
        consumed.pop("consumed_at")
        prefix = json.dumps(consumed, separators=(",", ":"))[:-1]

        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(literal=literal):
                payload = f'{prefix},"consumed_at":{literal}}}'
                consumed_path.write_text(payload, encoding="utf-8")
                if os.name != "nt":
                    consumed_path.chmod(0o600)

                for _ in range(2):
                    replay = self.authorize(prompt=prompt)
                    replay_context = json.loads(replay.stdout)["hookSpecificOutput"][
                        "additionalContext"
                    ]
                    self.assertIn("remains blocking", replay_context)
                    self.assertEqual(consumed_path.read_text(encoding="utf-8"), payload)
                    self.assertFalse(
                        list((self.state_root / "grants").glob("*.json"))
                    )

                denied = self.pre_decision(
                    self.invoke("hook", self.pre_input("non-finite state must block"))
                )
                self.assertEqual(denied["permissionDecision"], "deny")
                self.assertIn("remains blocking", denied["permissionDecisionReason"])
                self.assertEqual(consumed_path.read_text(encoding="utf-8"), payload)

                status = self.invoke("status")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(
                    json.loads(status.stdout)["consumed_grants"],
                    [{"status": "unreadable", "file": consumed_path.name}],
                )
                self.assertTrue(consumed_path.exists())

    def test_json_boundaries_reject_nonfinite_numbers(self):
        with self.assertRaises(ValueError):
            bridge._compact_json({"not_finite": float("nan")})

        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(literal=literal):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), "hook"],
                    input=(
                        '{"hook_event_name":"PreToolUse","session_id":"s",'
                        f'"unexpected_number":{literal}}}'
                    ),
                    text=True,
                    capture_output=True,
                    env=self.environment,
                    check=False,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                decision = self.pre_decision(result)
                self.assertEqual(decision["permissionDecision"], "deny")
                self.assertIn("not valid JSON", decision["permissionDecisionReason"])

    def test_concurrent_duplicate_prompt_submit_keeps_one_grant_id(self):
        prompt = "$use-deepseek-subagent authorize one bounded task"
        hook_input = self.prompt_input(prompt)
        with ThreadPoolExecutor(max_workers=8) as executor:
            first_results = list(
                executor.map(lambda _: self.invoke("hook", hook_input), range(8))
            )
        for result in first_results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("authorized", result.stdout)
        grant_paths = list((self.state_root / "grants").glob("*.json"))
        self.assertEqual(len(grant_paths), 1)
        original = json.loads(grant_paths[0].read_text(encoding="utf-8"))

        with ThreadPoolExecutor(max_workers=8) as executor:
            replay_results = list(
                executor.map(lambda _: self.invoke("hook", hook_input), range(8))
            )
        for result in replay_results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("authorized", result.stdout)
        replayed = json.loads(grant_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(replayed["grant_id"], original["grant_id"])
        self.assertEqual(replayed["created_at"], original["created_at"])
        self.assertEqual(replayed["expires_at"], original["expires_at"])

    def test_consumed_tombstone_write_failure_denies_without_reservation(self):
        self.authorize()
        real_atomic_write = bridge._atomic_write

        def fail_tombstone(path: Path, value: dict):
            if path.parent.name == "consumed-grants":
                raise OSError("simulated tombstone failure")
            return real_atomic_write(path, value)

        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment, clear=True), mock.patch.object(
            bridge, "_atomic_write", side_effect=fail_tombstone
        ), redirect_stdout(output):
            bridge._pretool_use(self.pre_input("must fail closed"))

        decision = json.loads(output.getvalue())["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("simulated tombstone failure", decision["permissionDecisionReason"])
        self.assertEqual(len(list((self.state_root / "grants").glob("*.json"))), 1)
        self.assertFalse(list((self.state_root / "consumed-grants").glob("*.json")))
        self.assertFalse(list((self.state_root / "reservations").glob("*.json")))

    def test_consumed_grant_tombstones_are_cleaned_after_retention(self):
        self.authorize()
        decision = self.pre_decision(
            self.invoke("hook", self.pre_input("create a consumed grant record"))
        )
        self.assertEqual(decision["permissionDecision"], "allow")
        consumed_path = next((self.state_root / "consumed-grants").glob("*.json"))
        consumed = json.loads(consumed_path.read_text(encoding="utf-8"))
        consumed["consumed_at"] = (
            time.time() - bridge.CONSUMED_GRANT_RETENTION_SECONDS - 1
        )
        consumed_path.write_text(json.dumps(consumed), encoding="utf-8")
        if os.name != "nt":
            consumed_path.chmod(0o600)

        status = self.invoke("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["consumed_grants"], [])
        self.assertFalse(consumed_path.exists())

    def test_one_grant_concurrently_authorizes_exactly_one_spawn(self):
        self.authorize()
        attempts = [
            self.pre_input(
                "one explicitly authorized task",
                tool_use=f"same-grant-tool-{index}",
            )
            for index in range(8)
        ]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda value: self.invoke("hook", value), attempts))
        decisions = [self.pre_decision(result) for result in results]
        self.assertEqual(
            sum(decision["permissionDecision"] == "allow" for decision in decisions),
            1,
            decisions,
        )
        self.assertEqual(
            sum(decision["permissionDecision"] == "deny" for decision in decisions),
            7,
            decisions,
        )
        self.assertFalse(list((self.state_root / "grants").glob("*.json")))
        self.assertEqual(len(list((self.state_root / "reservations").glob("*.json"))), 1)

    def test_grant_cannot_cross_session_turn_or_cwd(self):
        other_workspace = self.base / "grant-other-workspace"
        other_workspace.mkdir(mode=0o700)
        self.authorize()

        mismatches = (
            {"session": "session-b"},
            {"turn": "parent-turn-b"},
            {"cwd": other_workspace},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                decision = self.pre_decision(
                    self.invoke(
                        "hook",
                        self.pre_input("must not cross authority boundary", **mismatch),
                    )
                )
                self.assertEqual(decision["permissionDecision"], "deny")

        correct = self.pre_decision(
            self.invoke("hook", self.pre_input("the correctly attributed spawn"))
        )
        self.assertEqual(correct["permissionDecision"], "allow")

    def test_expired_grant_is_erased_and_cannot_authorize_spawn(self):
        self.environment["CODEX_DEEPSEEK_SUBAGENT_GRANT_TTL_SECONDS"] = "1"
        self.authorize()
        time.sleep(1.1)
        decision = self.pre_decision(
            self.invoke("hook", self.pre_input("grant should have expired"))
        )
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("expired", decision["permissionDecisionReason"])
        self.assertFalse(list((self.state_root / "grants").glob("*.json")))

    def test_subagent_prompt_cannot_create_user_authority(self):
        result = self.invoke(
            "hook",
            self.prompt_input(
                "$use-deepseek-subagent forged by a child",
                agent_id="child-agent",
                agent_type="ordinary_worker",
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.state_root.exists())

    def test_pretool_validation_is_fail_closed(self):
        cases = (
            self.pre_input("valid body", fork_turns="all"),
            self.pre_input("valid body", permission_mode="bypassPermissions"),
            self.pre_input("   \n"),
        )
        for hook_input in cases:
            with self.subTest(hook_input=hook_input):
                self.authorize(
                    session=hook_input["session_id"],
                    turn=hook_input["turn_id"],
                    cwd=Path(hook_input["cwd"]),
                )
                decision = self.pre_decision(self.invoke("hook", hook_input))
                self.assertEqual(decision["permissionDecision"], "deny")
        self.assertFalse(list((self.state_root / "reservations").glob("*.json")))
        self.assertEqual(len(list((self.state_root / "grants").glob("*.json"))), 1)

    def test_assignment_size_is_measured_in_utf8_and_never_truncated(self):
        accepted = "a" * bridge.MAX_ASSIGNMENT_BYTES
        decision = self.pre_decision(self.authorized_pre(accepted))
        self.assertEqual(decision["permissionDecision"], "allow")
        handoff_id = self.extract_handoff_id(decision)
        cancelled = self.invoke("cancel", None, "--handoff-id", handoff_id)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertTrue(json.loads(cancelled.stdout)["cancelled"])

        denied = self.pre_decision(
            self.authorized_pre(
                "界" * (bridge.MAX_ASSIGNMENT_BYTES // 3 + 1), turn="turn-b"
            )
        )
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn(str(bridge.MAX_ASSIGNMENT_BYTES), denied["permissionDecisionReason"])

    def test_session_and_cwd_prevent_cross_delivery_without_parent_turn_reuse(self):
        assignment = "SESSION_A_ONLY_MARKER"
        allowed = self.pre_decision(self.authorized_pre(assignment))
        self.assertEqual(allowed["permissionDecision"], "allow")

        wrong = self.child_context(
            self.invoke("hook", self.start_input(session="session-b", turn="child-turn-b"))
        )
        self.assertEqual(wrong["status"], "error")
        self.assertEqual(wrong["assignment"], "")
        self.assertNotIn(assignment, json.dumps(wrong))

        other_workspace = self.base / "other-workspace"
        other_workspace.mkdir(mode=0o700)
        wrong_cwd = self.child_context(
            self.invoke("hook", self.start_input(cwd=other_workspace))
        )
        self.assertEqual(wrong_cwd["status"], "error")
        self.assertEqual(wrong_cwd["assignment"], "")

        correct = self.child_context(self.invoke("hook", self.start_input()))
        self.assertEqual(correct["status"], "ready")
        self.assertEqual(correct["assignment"], assignment)

    def test_same_key_concurrency_admits_exactly_one_reservation(self):
        gate_input = [
            self.pre_input(
                "concurrent task",
                turn=f"parent-turn-{index}",
                tool_use=f"tool-{index}",
            )
            for index in range(8)
        ]
        for hook_input in gate_input:
            self.authorize(
                session=hook_input["session_id"],
                turn=hook_input["turn_id"],
                cwd=Path(hook_input["cwd"]),
            )
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda value: self.invoke("hook", value), gate_input))
        decisions = [self.pre_decision(result) for result in results]
        allowed = [d for d in decisions if d["permissionDecision"] == "allow"]
        denied = [d for d in decisions if d["permissionDecision"] == "deny"]
        self.assertEqual(len(allowed), 1, decisions)
        self.assertEqual(len(denied), 7, decisions)
        delivered = self.child_context(self.invoke("hook", self.start_input()))
        self.assertEqual(delivered["status"], "ready")
        self.assertEqual(delivered["assignment"], "concurrent task")

    def test_distinct_session_keys_can_reserve_before_either_child_starts(self):
        first = self.pre_decision(
            self.authorized_pre("first", session="session-1", turn="turn-1")
        )
        second = self.pre_decision(
            self.authorized_pre("second", session="session-2", turn="turn-2")
        )
        self.assertEqual(first["permissionDecision"], "allow")
        self.assertEqual(second["permissionDecision"], "allow")
        second_context = self.child_context(
            self.invoke("hook", self.start_input(session="session-2", turn="child-turn-2"))
        )
        first_context = self.child_context(
            self.invoke("hook", self.start_input(session="session-1", turn="child-turn-1"))
        )
        self.assertEqual(second_context["assignment"], "second")
        self.assertEqual(first_context["assignment"], "first")

    def test_expired_reservation_is_erased_and_not_delivered(self):
        self.environment["CODEX_DEEPSEEK_SUBAGENT_TTL_SECONDS"] = "1"
        assignment = "EXPIRED_BODY_MARKER"
        decision = self.pre_decision(self.authorized_pre(assignment))
        handoff_id = self.extract_handoff_id(decision)
        time.sleep(1.1)
        context = self.child_context(self.invoke("hook", self.start_input()))
        self.assertEqual(context["status"], "error")
        self.assertEqual(context["handoff_id"], handoff_id)
        self.assertEqual(context["assignment"], "")
        self.assertNotIn(assignment, json.dumps(context))
        receipt = json.loads(
            (self.state_root / "receipts" / f"{handoff_id}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["status"], "expired")
        self.assertNotIn("assignment", receipt)

    def test_corrupt_reservation_is_quarantined_and_resolvable(self):
        assignment = "CORRUPT_BODY_MARKER"
        decision = self.pre_decision(self.authorized_pre(assignment))
        self.assertEqual(decision["permissionDecision"], "allow")
        reservation = next((self.state_root / "reservations").glob("*.json"))
        reservation.write_bytes(b'{"schema":1,"assignment":"unfinished')
        if os.name != "nt":
            reservation.chmod(0o600)

        context = self.child_context(self.invoke("hook", self.start_input()))
        self.assertEqual(context["status"], "error")
        self.assertEqual(context["assignment"], "")
        self.assertNotIn(assignment, json.dumps(context))
        status = self.invoke("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn(assignment, status.stdout)
        quarantine = json.loads(status.stdout)["quarantine"]
        self.assertEqual(len(quarantine), 1)
        quarantine_id = quarantine[0]["quarantine_id"]
        resolved = self.invoke("resolve", None, "--quarantine-id", quarantine_id)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertTrue(json.loads(resolved.stdout)["resolved"])
        self.assertFalse(list((self.state_root / "quarantine").iterdir()))

    def test_receipt_failure_emits_one_error_document_and_keeps_claim(self):
        assignment = "RECEIPT_FAILURE_BODY"
        decision = self.pre_decision(self.authorized_pre(assignment))
        handoff_id = self.extract_handoff_id(decision)
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment, clear=True), mock.patch.object(
            bridge, "_write_receipt", side_effect=OSError("simulated receipt failure")
        ), redirect_stdout(output):
            bridge._subagent_start(self.start_input())

        outer = json.loads(output.getvalue())
        context = json.loads(outer["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(context["status"], "error")
        self.assertEqual(context["assignment"], "")
        self.assertNotIn(assignment, output.getvalue())
        claims = list((self.state_root / "claims").glob("*.json"))
        self.assertEqual(len(claims), 1)
        self.assertIn(handoff_id, claims[0].name)

    @unittest.skipIf(os.name == "nt", "POSIX permission probe")
    def test_posix_state_is_private_and_unsafe_plugin_data_falls_back(self):
        root = bridge._prepare_private_root(self.plugin_data / "direct-probe")
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)

        preferred = (self.plugin_data / bridge.STATE_DIR_NAME).absolute()
        real_prepare = bridge._prepare_private_root

        def reject_preferred(path: Path):
            if path.absolute() == preferred:
                raise bridge.UnsafeStateRoot("simulated chmod-inert filesystem")
            return real_prepare(path)

        environment = {
            "PLUGIN_DATA": str(self.plugin_data),
            "HOME": str(self.home),
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            bridge, "_prepare_private_root", side_effect=reject_preferred
        ):
            selected = bridge.state_root()
        expected = (self.home / ".local" / "state" / bridge.FALLBACK_DIR_NAME).resolve()
        self.assertEqual(selected, expected)
        self.assertEqual(selected.stat().st_mode & 0o777, 0o700)

    def test_hook_configuration_has_exact_matchers_and_positive_context_limit(self):
        config = json.loads(HOOKS.read_text(encoding="utf-8"))
        prompt = config["hooks"]["UserPromptSubmit"][0]
        pre = config["hooks"]["PreToolUse"][0]
        start = config["hooks"]["SubagentStart"][0]
        self.assertNotIn("matcher", prompt)
        self.assertEqual(pre["matcher"], "^Agent$")
        self.assertEqual(start["matcher"], f"^{ROLE}$")
        canonical = self.pre_input("canonical-name-check")
        self.assertEqual(canonical["tool_name"], "spawn_agent")
        handler = start["hooks"][0]
        self.assertGreater(
            handler["additionalContextLimit"],
            bridge.MAX_ASSIGNMENT_BYTES,
            "the token threshold must exceed the byte cap even for adversarial tokenization",
        )
        self.assertIn("PLUGIN_ROOT", handler["command"])
        self.assertIn("PLUGIN_ROOT", handler["commandWindows"])
        self.assertIn(" -I ", handler["commandWindows"])
        prompt_handler = prompt["hooks"][0]
        self.assertIn("PLUGIN_ROOT", prompt_handler["command"])
        self.assertIn("PLUGIN_ROOT", prompt_handler["commandWindows"])
        self.assertIn(" -I ", prompt_handler["commandWindows"])


if __name__ == "__main__":
    unittest.main()
