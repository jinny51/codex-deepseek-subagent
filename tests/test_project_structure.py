from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any, Iterator

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 can still run the bridge-only tests.
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "codex-deepseek-subagent"
CANONICAL_ROLE = "deepseek_evidence_worker"
CANONICAL_PROTOCOL = "codex-deepseek-subagent/v1"
EXPECTED_SKILLS = {"use-deepseek-subagent", "setup-deepseek-subagent"}
IGNORED_DIRS = {".git", ".mypy_cache", ".pytest_cache", ".venv", "__pycache__"}


def repository_files() -> Iterator[Path]:
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.relative_to(ROOT).parts):
            continue
        yield path


def repository_text() -> Iterator[tuple[Path, str]]:
    for path in repository_files():
        raw = path.read_bytes()
        if b"\x00" in raw:
            continue
        try:
            yield path, raw.decode("utf-8")
        except UnicodeDecodeError:
            continue


def nested_values(value: Any, key: str) -> Iterator[Any]:
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if item_key == key:
                yield item_value
            yield from nested_values(item_value, key)
    elif isinstance(value, list):
        for item in value:
            yield from nested_values(item, key)


def parse_skill_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise AssertionError(f"{path.relative_to(ROOT)} has no YAML frontmatter")
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise AssertionError(f"{path.relative_to(ROOT)} has unclosed YAML frontmatter") from exc

    result: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise AssertionError(f"{path.relative_to(ROOT)} has unsupported frontmatter: {line!r}")
        key, raw_value = line.split(":", 1)
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key.strip()] = value
    return result


class ProjectStructureTests(unittest.TestCase):
    maxDiff = None

    def test_all_json_and_toml_files_parse(self) -> None:
        json_files = sorted(path for path in repository_files() if path.suffix == ".json")
        toml_files = sorted(path for path in repository_files() if path.suffix == ".toml")
        self.assertTrue(json_files, "expected at least one JSON configuration")
        self.assertTrue(toml_files, "expected at least one TOML configuration")

        for path in json_files:
            with self.subTest(path=path.relative_to(ROOT)):
                json.loads(path.read_text(encoding="utf-8"))
        if tomllib is None:
            self.skipTest("complete TOML validation requires Python 3.11 or newer")
        for path in toml_files:
            with self.subTest(path=path.relative_to(ROOT)):
                tomllib.loads(path.read_text(encoding="utf-8"))

    def test_plugin_manifest_is_complete_and_namespaced(self) -> None:
        manifests = sorted(ROOT.glob("plugins/*/.codex-plugin/plugin.json"))
        self.assertEqual(len(manifests), 1, "the repository must publish exactly one plugin")

        manifest_path = manifests[0]
        plugin_root = manifest_path.parents[1]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(plugin_root.name, PLUGIN_NAME)
        self.assertEqual(manifest.get("name"), PLUGIN_NAME)
        self.assertRegex(str(manifest.get("version", "")), r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
        self.assertTrue(str(manifest.get("description", "")).strip())
        self.assertTrue(str(manifest.get("author", {}).get("name", "")).strip())
        self.assertEqual(manifest.get("license"), "MIT")
        self.assertEqual(manifest.get("skills"), "./skills/")
        self.assertNotIn("hooks", manifest, "Codex discovers plugin hooks without a manifest hooks field")

        interface = manifest.get("interface")
        self.assertIsInstance(interface, dict)
        for key in ("displayName", "shortDescription", "longDescription", "developerName", "category"):
            self.assertTrue(str(interface.get(key, "")).strip(), f"interface.{key} is required")

        prompts = interface.get("defaultPrompt", [])
        self.assertIsInstance(prompts, list)
        self.assertLessEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertIsInstance(prompt, str)
            self.assertLessEqual(len(prompt), 128)
        self.assertEqual(
            {prompt.split(maxsplit=1)[0] for prompt in prompts},
            {"$use-deepseek-subagent", "$setup-deepseek-subagent"},
        )
        self.assertEqual(interface.get("capabilities"), ["Read", "Write"])

        for key in ("homepage", "repository"):
            self.assertRegex(str(manifest.get(key, "")), r"^https://")
        for key in ("websiteURL", "privacyPolicyURL", "termsOfServiceURL"):
            if key in interface:
                self.assertRegex(str(interface[key]), r"^https://")

        self.assertTrue((plugin_root / "skills").is_dir())
        for field in ("apps", "mcpServers"):
            companion = manifest.get(field)
            if isinstance(companion, str):
                self.assertTrue((plugin_root / companion).resolve().is_file(), f"missing {field} companion")

    def test_marketplace_matches_the_plugin_manifest(self) -> None:
        marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        entries = marketplace.get("plugins")
        self.assertIsInstance(entries, list)
        self.assertEqual(len(entries), 1)

        names = [entry.get("name") for entry in entries]
        self.assertEqual(len(names), len(set(names)), "marketplace plugin names must be unique")
        entry = entries[0]
        self.assertEqual(entry.get("name"), PLUGIN_NAME)
        self.assertEqual(entry.get("source"), {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"})
        self.assertEqual(entry.get("category"), "Developer Tools")
        self.assertIn(entry.get("policy", {}).get("installation"), {"NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT"})
        self.assertIn(entry.get("policy", {}).get("authentication"), {"ON_INSTALL", "ON_USE"})

        manifest = json.loads(
            (ROOT / "plugins" / PLUGIN_NAME / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(entry["category"], manifest["interface"]["category"])

    def test_skills_have_unique_valid_metadata(self) -> None:
        skills_root = ROOT / "plugins" / PLUGIN_NAME / "skills"
        skill_files = sorted(skills_root.glob("*/SKILL.md"))
        self.assertEqual({path.parent.name for path in skill_files}, EXPECTED_SKILLS)

        names: list[str] = []
        for path in skill_files:
            with self.subTest(skill=path.parent.name):
                metadata = parse_skill_frontmatter(path)
                self.assertEqual(metadata.get("name"), path.parent.name)
                self.assertTrue(metadata.get("description", "").strip())
                self.assertNotIn("TODO", path.read_text(encoding="utf-8").upper())
                names.append(metadata["name"])

                interface_path = path.parent / "agents" / "openai.yaml"
                self.assertTrue(interface_path.is_file())
                interface_text = interface_path.read_text(encoding="utf-8")
                self.assertIn("interface:", interface_text)
                self.assertIn("display_name:", interface_text)
                self.assertIn("short_description:", interface_text)
                self.assertIn("default_prompt:", interface_text)
                self.assertIn(f"${metadata['name']}", interface_text)
                policy_blocks = re.findall(
                    r"(?ms)^policy:\s*\n((?:^[ \t]+.*(?:\n|$))*)",
                    interface_text,
                )
                self.assertEqual(len(policy_blocks), 1, "Skill must declare one invocation policy")
                self.assertRegex(
                    policy_blocks[0],
                    r"(?m)^\s+allow_implicit_invocation:\s*false\s*$",
                )
                self.assertNotIn("TODO", interface_text.upper())

        self.assertEqual(len(names), len(set(names)), "skill names must be unique")

    def test_agent_role_and_protocol_namespace_are_unique(self) -> None:
        if tomllib is None:
            self.skipTest("Agent TOML validation requires Python 3.11 or newer")
        agent_files = sorted((ROOT / "plugins" / PLUGIN_NAME / "assets").glob("*.toml"))
        self.assertEqual(len(agent_files), 1, "publish one canonical worker template")
        agents = [tomllib.loads(path.read_text(encoding="utf-8")) for path in agent_files]
        self.assertEqual({agent.get("name") for agent in agents}, {CANONICAL_ROLE})

        agent = agents[0]
        self.assertEqual(agent.get("model_provider"), "deepseek")
        self.assertEqual(agent.get("sandbox_mode"), "read-only")
        self.assertIsInstance(agent.get("model"), str)
        self.assertTrue(agent["model"].strip())

        corpus = "\n".join(text for _, text in repository_text())
        role_names = set(re.findall(r"\bdeepseek_[a-z0-9_]*worker\b", corpus))
        self.assertEqual(role_names, {CANONICAL_ROLE})
        protocol_names = set(re.findall(r"\bcodex-deepseek-subagent/v[0-9]+\b", corpus))
        self.assertEqual(protocol_names, {CANONICAL_PROTOCOL})

        forbidden_upstream_role = "v4" + "_flash_worker"
        for path, text in repository_text():
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotIn(forbidden_upstream_role, text)

    def test_hook_context_limits_are_positive_and_role_scoped(self) -> None:
        hook_files = sorted((ROOT / "plugins" / PLUGIN_NAME / "hooks").glob("*.json"))
        self.assertEqual(len(hook_files), 1, "publish one hook definition")
        hooks_document = json.loads(hook_files[0].read_text(encoding="utf-8"))
        hooks = hooks_document.get("hooks")
        self.assertIsInstance(hooks, dict)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("PreToolUse", hooks)
        self.assertIn("SubagentStart", hooks)

        prompt_groups = hooks["UserPromptSubmit"]
        self.assertIsInstance(prompt_groups, list)
        self.assertTrue(prompt_groups)
        self.assertEqual(
            list(nested_values(prompt_groups, "matcher")),
            [],
            "UserPromptSubmit must inspect every raw user prompt without a matcher",
        )

        limits = list(nested_values(hooks_document, "additionalContextLimit"))
        self.assertTrue(limits, "Hook definitions must declare bounded context output")
        for limit in limits:
            self.assertIs(type(limit), int)
            self.assertGreater(limit, 0)
            self.assertGreaterEqual(limit, 65536)

        matchers = list(nested_values(hooks_document, "matcher"))
        self.assertIn(f"^{CANONICAL_ROLE}$", matchers)

    def test_repository_contains_no_secret_material(self) -> None:
        forbidden_secret_files: list[Path] = []
        for path in repository_files():
            if path.name.startswith(".env") and path.name not in {".env.example", ".env.template"}:
                forbidden_secret_files.append(path.relative_to(ROOT))
        self.assertEqual(forbidden_secret_files, [])

        token_patterns = {
            "provider token": re.compile(r"\b" + "sk" + r"-[A-Za-z0-9_-]{20,}\b"),
            "GitHub token": re.compile(r"\b" + "gh" + r"[pousr]_[A-Za-z0-9]{20,}\b"),
            "AWS access key": re.compile(r"\b" + "AK" + r"IA[0-9A-Z]{16}\b"),
            "private key": re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?" + "PRIVATE KEY-----"),
        }
        secret_names = (
            "DEEPSEEK_API_" + "KEY",
            "OPENAI_API_" + "KEY",
            "ANTHROPIC_API_" + "KEY",
        )
        secret_name_pattern = "|".join(re.escape(name) for name in secret_names)
        assignment_patterns = (
            re.compile(
                r"(?im)^\s*(?:export\s+|set\s+|setx\s+|\$env:)?(?:"
                + secret_name_pattern
                + r")\s*=\s*[\"']?([^\s\"'#]+)"
            ),
            re.compile(
                r"(?im)^\s*[A-Za-z_][A-Za-z0-9_]*\[\s*[\"'](?:"
                + secret_name_pattern
                + r")[\"']\s*\]\s*=\s*[\"']?([^\s\"'#]+)"
            ),
        )
        safe_prefixes = (
            "$",
            "%",
            "<",
            "YOUR_",
            "REPLACE",
            "EXAMPLE",
            "DUMMY",
            "TEST",
            "REDACTED",
            "CHANGEME",
            "MUST-NEVER",
        )
        wholesale_environment_copy = (
            "os.environ." + "copy()",
            "dict(" + "os.environ)",
        )

        findings: list[str] = []
        for path, text in repository_text():
            relative = path.relative_to(ROOT)
            for label, pattern in token_patterns.items():
                if pattern.search(text):
                    findings.append(f"{relative}: {label}")
            for marker in wholesale_environment_copy:
                if marker in text:
                    findings.append(f"{relative}: wholesale ambient environment copy")
            for assignment_pattern in assignment_patterns:
                for match in assignment_pattern.finditer(text):
                    value = match.group(1).strip()
                    if not value.upper().startswith(safe_prefixes):
                        findings.append(f"{relative}: concrete secret assignment")
        self.assertEqual(findings, [])

    def test_ci_covers_supported_operating_systems_and_python_versions(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for operating_system in ("ubuntu-latest", "windows-latest", "macos-latest"):
            self.assertIn(operating_system, workflow)
        for version in ('"3.11"', '"3.12"'):
            self.assertIn(version, workflow)
        self.assertIn("unittest discover", workflow)


if __name__ == "__main__":
    unittest.main()
