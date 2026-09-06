from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-port"
WORKFLOWS = {"migrate", "backup", "restore", "verify", "doctor", "rollback"}


def _load_tool(name: str) -> ModuleType:
    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dual_plugin_manifests_and_marketplaces_share_identity() -> None:
    codex = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    codex_market = json.loads(
        (ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    claude_market = json.loads(
        (ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )

    assert codex["name"] == claude["name"] == "agent-port"
    assert codex["version"] == claude["version"] == "0.5.5"
    assert codex_market["plugins"][0]["source"]["path"] == "./plugins/agent-port"
    assert claude_market["plugins"][0]["source"] == "./plugins/agent-port"
    assert claude_market["plugins"][0]["strict"] is True


def test_plugin_exposes_six_guarded_workflow_skills() -> None:
    found = set()
    for skill in (PLUGIN / "skills").glob("*/SKILL.md"):
        found.add(skill.parent.name)
        _, frontmatter, body = skill.read_text(encoding="utf-8").split("---", maxsplit=2)
        parsed = yaml.safe_load(frontmatter)
        assert parsed["name"] == skill.parent.name
        assert parsed["description"]
        assert "agent-port --version" in body
        assert "0.5.x" in body
    assert found == WORKFLOWS


def test_migration_skill_runs_destination_receiver_for_the_user() -> None:
    migrate = (PLUGIN / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    destination = (PLUGIN / "references" / "destination-workflow.md").read_text(encoding="utf-8")

    assert "transfer prepare --format json" in migrate
    assert "transfer receive --workspace MIGRATION_WORKSPACE --format json" in migrate
    assert "--pairing-code-file" not in migrate
    assert "--pairing-code PAIRING_CODE" not in migrate
    assert "free-form reply" in migrate
    assert "Never require a destination Terminal" in migrate
    assert "exact absolute `pairing_code_file` path" in destination


def test_migration_skill_uses_only_canonical_non_probe_command_blocks() -> None:
    migrate = (PLUGIN / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    command_blocks = "\n".join(re.findall(r"```text\n(.*?)```", migrate, flags=re.DOTALL))

    assert "transfer prepare" in command_blocks
    assert "transfer receive" in command_blocks
    assert "restore plan-info" in command_blocks
    assert "--help" not in command_blocks
    assert "transfer inspect" not in command_blocks
    assert "skills inspect" not in command_blocks
    assert not any(token in command_blocks for token in (" | ", "cat ", "grep ", "head "))


def test_migration_skill_arms_restore_and_verifies_after_restart() -> None:
    migrate = (PLUGIN / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    restore = (PLUGIN / "skills" / "restore" / "SKILL.md").read_text(encoding="utf-8")
    verify = (PLUGIN / "skills" / "verify" / "SKILL.md").read_text(encoding="utf-8")

    for content in (migrate, restore):
        assert "restore handoff" in content
        assert "--confirm-quit-to-apply" in content
        assert "Never hand" in content or "never send" in content
        assert "restore verify" in content
        assert "restore plan-info" in content
    assert "restore_completed_safely" in verify
    assert "current_data_intact" in verify
    assert "destination_valid" in verify


def test_plugin_has_no_automatic_execution_components() -> None:
    assert not (PLUGIN / ".mcp.json").exists()
    assert not (PLUGIN / ".app.json").exists()
    assert not (PLUGIN / "hooks").exists()
    assert not (PLUGIN / "bin").exists()
    for manifest_path in (
        PLUGIN / ".codex-plugin" / "plugin.json",
        PLUGIN / ".claude-plugin" / "plugin.json",
    ):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "hooks" not in manifest
        assert "mcpServers" not in manifest
        assert "apps" not in manifest


def test_repository_plugin_validator_passes() -> None:
    validator = _load_tool("validate_plugins")
    assert validator.validate() == "0.5.5"


def test_plugin_builder_is_reproducible_and_self_contained(tmp_path: Path) -> None:
    builder = _load_tool("build_plugin")
    first, first_checksum = builder.build(tmp_path / "first")
    second, second_checksum = builder.build(tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    assert first_checksum.read_text(encoding="utf-8") == f"{digest}  {first.name}\n"
    assert second_checksum.read_text(encoding="utf-8") == f"{digest}  {second.name}\n"

    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
    assert "agent-port/.codex-plugin/plugin.json" in names
    assert "agent-port/.claude-plugin/plugin.json" in names
    assert {f"agent-port/skills/{name}/SKILL.md" for name in WORKFLOWS} <= names
    assert all(".." not in Path(name).parts for name in names)
    assert not any(name.endswith((".agentpack", ".db", ".sqlite")) for name in names)


@pytest.mark.parametrize("name", ["restore", "rollback"])
def test_mutating_workflows_require_fresh_closed_confirmation(name: str) -> None:
    content = (PLUGIN / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    assert "fresh explicit confirmation" in content
    assert "--confirm-harness-closed" in content
