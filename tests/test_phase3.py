from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from agent_port import __version__
from agent_port.domain.models import ArchiveManifest, InspectionReport
from agent_port.presentation.cli import app

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "agent-port" / "SKILL.md"


def _skill_parts() -> tuple[dict[str, Any], str]:
    content = SKILL.read_text(encoding="utf-8")
    marker, frontmatter, body = content.split("---", maxsplit=2)
    assert marker == ""
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed, body


def test_portable_skill_has_only_the_common_contract() -> None:
    frontmatter, body = _skill_parts()

    assert frontmatter == {
        "name": "agent-port",
        "description": (
            "Orchestrates Agent Port to inspect, transfer, back up, plan, safely apply, verify, "
            "or roll back native Codex and Claude Code migrations. Use when moving coding-agent "
            "sessions or user-owned skills between machines, examining .agentpack archives, "
            "resolving restore mappings or conflicts, validating destinations, or recovering a "
            "restore."
        ),
    }
    assert SKILL.parent.name == frontmatter["name"]
    assert len(body.splitlines()) < 500
    files = [path.relative_to(SKILL.parent) for path in SKILL.parent.rglob("*") if path.is_file()]
    assert files == [Path("SKILL.md")]


def test_skill_documents_real_cli_and_mutation_guardrails() -> None:
    content = SKILL.read_text(encoding="utf-8")
    runner = CliRunner()

    for command in (
        ["inspect", "--help"],
        ["skills", "inspect", "--help"],
        ["backup", "--help"],
        ["transfer", "send", "--help"],
        ["transfer", "receive", "--help"],
        ["restore", "plan", "--help"],
        ["restore", "handoff", "--help"],
        ["restore", "apply", "--help"],
        ["restore", "verify", "--help"],
        ["restore", "rollback", "--help"],
        ["doctor", "--help"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output

    for required in (
        "never apply an archive directly",
        "never pass `--confirm-harness-closed` without that confirmation",
        "never hand-edit its\nJSON",
        "Do not weaken validation to continue",
        "Never overwrite newer user data",
        "Only use `agent-port transfer` after the user explicitly requests LAN transfer",
    ):
        assert required in content


def test_package_and_module_versions_match() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == __version__ == "0.5.5"
    assert "Development Status :: 3 - Alpha" in project["classifiers"]
    assert ArchiveManifest.model_fields["adapter_version"].default == __version__
    assert InspectionReport.model_fields["adapter_version"].default == __version__


def test_skill_evaluation_suite_covers_safety_boundaries() -> None:
    path = ROOT / "tests" / "fixtures" / "agent_port_skill_evals.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    identifiers = {case["id"] for case in payload["cases"]}

    assert identifiers == {
        "codex-backup",
        "claude-restore-plan-first",
        "mapping-and-skill-decisions",
        "fresh-closed-confirmation",
        "automatic-post-restart-verification",
        "early-restart-pending",
        "unsupported-destination",
        "successful-apply",
        "safe-rollback-refusal",
        "automatic-lan-transfer",
        "exact-transfer-send-no-format",
        "pairing-file-retry",
        "visible-pairing-code-handoff",
        "changed-transcript-no-success",
        "incomplete-counts-no-success",
        "archive-secrecy",
        "archived-skill-execution-refusal",
        "cross-harness-refusal",
        "doctor-read-only",
        "incompatible-cli",
        "missing-cli",
        "project-registration-approval",
        "stale-plan",
        "blocked-plan-is-valid",
        "append-only-session-collision",
        "safe-count-summary",
        "no-command-discovery",
    }
    assert all(case["prompt"] and case["expected"] for case in payload["cases"])


def test_release_automation_uses_current_dynamic_cli_contract() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'grep -Fx "0.4.0"' not in ci
    assert "tomllib" in ci
    for command in ("inspect", "transfer", "backup", "restore", "doctor", "skills"):
        assert command in ci
    assert "AGENT_PORT_STRESS_REPORT" in ci
    assert "AGENT_PORT_STRESS_REPORT" in release
    assert "Verify release artifact contents" in release
    assert "Smoke-test release wheel" in release
    assert "agent_port/application/transfer.py" in release
