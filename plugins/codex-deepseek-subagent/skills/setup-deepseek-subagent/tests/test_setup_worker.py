from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "manage_agent.py"
TARGET_NAME = "deepseek-evidence-worker.toml"
SAFE_ENVIRONMENT_NAMES = (
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)


class SetupWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temporary.name) / "codex-home"
        self.target = self.codex_home / "agents" / TARGET_NAME

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_command(self, command: str, *, key: bool = False) -> tuple[subprocess.CompletedProcess[str], dict]:
        environment = {
            name: os.environ[name]
            for name in SAFE_ENVIRONMENT_NAMES
            if name in os.environ
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if key:
            environment["DEEPSEEK_API_KEY"] = "must-never-appear-in-output"
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                command,
                "--codex-home",
                str(self.codex_home),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        self.assertNotIn("must-never-appear-in-output", completed.stdout)
        self.assertNotIn("must-never-appear-in-output", completed.stderr)
        return completed, json.loads(completed.stdout)

    def test_plan_is_read_only(self) -> None:
        completed, result = self.run_command("plan")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["action"], "create")
        self.assertEqual(result["key_status"], "missing")
        self.assertFalse(self.codex_home.exists())

    def test_install_doctor_and_uninstall_touch_only_the_agent(self) -> None:
        installed, install_result = self.run_command("install", key=True)

        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(install_result["status"], "installed")
        self.assertEqual(install_result["key_status"], "present")
        self.assertTrue(self.target.is_file())
        all_files = [path for path in self.codex_home.rglob("*") if path.is_file()]
        self.assertEqual(all_files, [self.target])
        content = self.target.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# codex-deepseek-subagent: managed-agent/v1\n"))
        self.assertIn("payload_sha256", content.splitlines()[1])

        doctor, doctor_result = self.run_command("doctor", key=True)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(doctor_result["status"], "healthy")
        self.assertEqual(doctor_result["inspection"]["state"], "current")

        removed, remove_result = self.run_command("uninstall")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(remove_result["status"], "removed")
        self.assertFalse(self.target.exists())

    def test_install_refuses_an_unmanaged_target(self) -> None:
        self.target.parent.mkdir(parents=True)
        original = "name = \"someone_elses_agent\"\n"
        self.target.write_text(original, encoding="utf-8")

        completed, result = self.run_command("install")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["inspection"]["state"], "conflict")
        self.assertEqual(self.target.read_text(encoding="utf-8"), original)

    def test_install_and_uninstall_refuse_a_modified_managed_target(self) -> None:
        installed, _ = self.run_command("install")
        self.assertEqual(installed.returncode, 0)
        with self.target.open("a", encoding="utf-8") as stream:
            stream.write("# user change\n")

        reinstall, reinstall_result = self.run_command("install")
        self.assertEqual(reinstall.returncode, 2)
        self.assertEqual(reinstall_result["status"], "refused")
        self.assertEqual(reinstall_result["inspection"]["state"], "modified")

        completed, result = self.run_command("uninstall")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["inspection"]["state"], "modified")
        self.assertTrue(self.target.exists())

    def test_install_updates_an_unmodified_owned_older_payload(self) -> None:
        installed, _ = self.run_command("install")
        self.assertEqual(installed.returncode, 0)
        content = self.target.read_text(encoding="utf-8")
        header, manifest_line, blank, payload = content.split("\n", 3)
        older_payload = payload + "# previous managed release\n"
        manifest = json.loads(manifest_line.split(": ", 1)[1])
        import hashlib

        manifest["payload_sha256"] = hashlib.sha256(older_payload.encode("utf-8")).hexdigest()
        older_managed_file = (
            header
            + "\n# codex-deepseek-subagent-ownership: "
            + json.dumps(manifest, separators=(",", ":"), sort_keys=True)
            + "\n"
            + blank
            + "\n"
            + older_payload
        )
        # The manager writes canonical LF bytes on every platform. Construct an
        # older managed release the same way instead of letting Windows text IO
        # translate its ownership header to CRLF.
        self.target.write_bytes(older_managed_file.encode("utf-8"))

        planned, plan_result = self.run_command("plan")
        self.assertEqual(planned.returncode, 0)
        self.assertEqual(plan_result["action"], "update")
        updated, update_result = self.run_command("install")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertEqual(update_result["status"], "updated")

    def test_doctor_reports_missing_key_without_reading_it(self) -> None:
        installed, _ = self.run_command("install")
        self.assertEqual(installed.returncode, 0)

        completed, result = self.run_command("doctor")

        self.assertEqual(completed.returncode, 1)
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["key_status"], "missing")
        self.assertEqual(result["inspection"]["state"], "current")


if __name__ == "__main__":
    unittest.main()
