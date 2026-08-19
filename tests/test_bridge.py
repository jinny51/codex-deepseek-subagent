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
SCRIPT = ROOT / "plugins" / "codex-deepseek-bridge" / "hooks" / "bridge.py"
HOOKS = ROOT / "plugins" / "codex-deepseek-bridge" / "hooks" / "hooks.json"
ROLE = "deepseek_evidence_worker"
PROTOCOL = "codex-deepseek-bridge/v1"

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
            "CODEX_DEEPSEEK_BRIDGE_TTL_SECONDS": "120",
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
        pre = self.invoke("hook", self.pre_input(assignment))
        decision = self.pre_decision(pre)
        self.assertEqual(decision["permissionDecision"], "allow")
        self.assertNotIn(assignment, pre.stdout)
        handoff_id = self.extract_handoff_id(decision)

        started = self.invoke("hook", self.start_input())
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

    def test_pretool_validation_is_fail_closed(self):
        cases = (
            self.pre_input("valid body", fork_turns="all"),
            self.pre_input("valid body", permission_mode="bypassPermissions"),
            self.pre_input("   \n"),
        )
        for hook_input in cases:
            with self.subTest(hook_input=hook_input):
                decision = self.pre_decision(self.invoke("hook", hook_input))
                self.assertEqual(decision["permissionDecision"], "deny")
        self.assertFalse(list(self.state_root.rglob("*.json")))

    def test_assignment_size_is_measured_in_utf8_and_never_truncated(self):
        accepted = "a" * bridge.MAX_ASSIGNMENT_BYTES
        decision = self.pre_decision(self.invoke("hook", self.pre_input(accepted)))
        self.assertEqual(decision["permissionDecision"], "allow")
        handoff_id = self.extract_handoff_id(decision)
        cancelled = self.invoke("cancel", None, "--handoff-id", handoff_id)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertTrue(json.loads(cancelled.stdout)["cancelled"])

        denied = self.pre_decision(
            self.invoke(
                "hook",
                self.pre_input("界" * (bridge.MAX_ASSIGNMENT_BYTES // 3 + 1), turn="turn-b"),
            )
        )
        self.assertEqual(denied["permissionDecision"], "deny")
        self.assertIn(str(bridge.MAX_ASSIGNMENT_BYTES), denied["permissionDecisionReason"])

    def test_session_and_cwd_prevent_cross_delivery_without_parent_turn_reuse(self):
        assignment = "SESSION_A_ONLY_MARKER"
        allowed = self.pre_decision(self.invoke("hook", self.pre_input(assignment)))
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
            self.invoke("hook", self.pre_input("first", session="session-1", turn="turn-1"))
        )
        second = self.pre_decision(
            self.invoke("hook", self.pre_input("second", session="session-2", turn="turn-2"))
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
        self.environment["CODEX_DEEPSEEK_BRIDGE_TTL_SECONDS"] = "1"
        assignment = "EXPIRED_BODY_MARKER"
        decision = self.pre_decision(self.invoke("hook", self.pre_input(assignment)))
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
        decision = self.pre_decision(self.invoke("hook", self.pre_input(assignment)))
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
        decision = self.pre_decision(self.invoke("hook", self.pre_input(assignment)))
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
        pre = config["hooks"]["PreToolUse"][0]
        start = config["hooks"]["SubagentStart"][0]
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


if __name__ == "__main__":
    unittest.main()
