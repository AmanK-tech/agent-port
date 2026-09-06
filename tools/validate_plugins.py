from __future__ import annotations

import json
import re
import struct
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-port"
WORKFLOWS = {"migrate", "backup", "restore", "verify", "doctor", "rollback"}
MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


class ValidationError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"invalid JSON at {path.relative_to(ROOT)}: {error}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"expected an object at {path.relative_to(ROOT)}")
    return value


def _load_skill(path: Path) -> tuple[dict[str, Any], str]:
    content = path.read_text(encoding="utf-8")
    parts = content.split("---", maxsplit=2)
    if len(parts) != 3 or parts[0] != "":
        raise ValidationError(f"invalid frontmatter at {path.relative_to(ROOT)}")
    frontmatter = yaml.safe_load(parts[1])
    if not isinstance(frontmatter, dict):
        raise ValidationError(f"invalid frontmatter object at {path.relative_to(ROOT)}")
    return frontmatter, parts[2]


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    if len(data) != 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValidationError(f"not a PNG: {path.relative_to(ROOT)}")
    return struct.unpack(">II", data[16:24])


def _validate_version_contract() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    package_version = str(project["version"])
    module = (ROOT / "src" / "agent_port" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', module, re.MULTILINE)
    if match is None or match.group(1) != package_version:
        raise ValidationError("Python package and module versions differ")

    for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        manifest = _load_json(PLUGIN / relative)
        if manifest.get("name") != "agent-port" or manifest.get("version") != package_version:
            raise ValidationError(f"version or name mismatch in plugins/agent-port/{relative}")
    return package_version


def _validate_marketplaces() -> None:
    codex = _load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    claude = _load_json(ROOT / ".claude-plugin" / "marketplace.json")
    if codex.get("name") != "agent-port" or claude.get("name") != "agent-port":
        raise ValidationError("marketplace names must be agent-port")

    codex_plugins = codex.get("plugins")
    claude_plugins = claude.get("plugins")
    if not isinstance(codex_plugins, list) or len(codex_plugins) != 1:
        raise ValidationError("Codex marketplace must contain exactly one plugin")
    if not isinstance(claude_plugins, list) or len(claude_plugins) != 1:
        raise ValidationError("Claude marketplace must contain exactly one plugin")

    codex_entry = codex_plugins[0]
    claude_entry = claude_plugins[0]
    if codex_entry.get("source") != {"source": "local", "path": "./plugins/agent-port"}:
        raise ValidationError("unexpected Codex marketplace source")
    if codex_entry.get("policy") != {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }:
        raise ValidationError("Codex marketplace policy is incomplete")
    if (
        claude_entry.get("source") != "./plugins/agent-port"
        or claude_entry.get("strict") is not True
    ):
        raise ValidationError("unexpected Claude marketplace source or strict policy")


def _validate_skills() -> None:
    skill_roots = {path.parent.name: path for path in (PLUGIN / "skills").glob("*/SKILL.md")}
    if set(skill_roots) != WORKFLOWS:
        raise ValidationError(
            f"expected workflow skills {sorted(WORKFLOWS)}, got {sorted(skill_roots)}"
        )

    for name, path in skill_roots.items():
        frontmatter, body = _load_skill(path)
        if frontmatter.get("name") != name or not frontmatter.get("description"):
            raise ValidationError(f"invalid name or description in {path.relative_to(ROOT)}")
        if "agent-port --version" not in body or "0.5.x" not in body:
            raise ValidationError(f"missing CLI compatibility gate in {path.relative_to(ROOT)}")
        for target in MARKDOWN_LINK.findall(body):
            if "://" in target or target.startswith("#"):
                continue
            if not (path.parent / target).resolve().is_file():
                raise ValidationError(f"broken reference {target} in {path.relative_to(ROOT)}")

    safety = (PLUGIN / "references" / "safety-contract.md").read_text(encoding="utf-8")
    for phrase in (
        "saved, current, blocker-free plan",
        "fresh, explicit authorization",
        "Upload an archive or transfer it beyond the local network",
        "Convert Codex sessions into Claude Code sessions",
        "Execute archived skill scripts",
    ):
        if phrase not in safety:
            raise ValidationError(f"safety contract is missing: {phrase}")

    restore = (PLUGIN / "skills" / "restore" / "SKILL.md").read_text(encoding="utf-8")
    rollback = (PLUGIN / "skills" / "rollback" / "SKILL.md").read_text(encoding="utf-8")
    migrate = (PLUGIN / "skills" / "migrate" / "SKILL.md").read_text(encoding="utf-8")
    verify = (PLUGIN / "skills" / "verify" / "SKILL.md").read_text(encoding="utf-8")
    if (
        "fresh explicit confirmation" not in restore
        or "fresh explicit confirmation" not in rollback
    ):
        raise ValidationError("restore and rollback must require fresh harness-closed confirmation")
    for phrase in (
        "transfer prepare --format json",
        "transfer receive --workspace MIGRATION_WORKSPACE --format json",
        "free-form reply",
        "Never require a destination Terminal",
    ):
        if phrase not in migrate:
            raise ValidationError(f"migration workflow is missing harness-driven receipt: {phrase}")
    if "--pairing-code PAIRING_CODE" in migrate:
        raise ValidationError("migration workflow exposes the pairing code in a command")
    if "--pairing-code-file" in migrate:
        raise ValidationError("migration workflow should resolve the authorized inbox from state")
    for phrase in (
        "restore handoff",
        "--confirm-quit-to-apply",
        "restore verify",
        "When the user returns",
    ):
        if phrase not in migrate:
            raise ValidationError(f"migration workflow is missing automatic restore: {phrase}")
    for phrase in ("restore_completed_safely", "current_data_intact", "destination_valid"):
        if phrase not in verify:
            raise ValidationError(f"verification workflow is missing result field: {phrase}")


def _validate_assets_and_contents() -> None:
    expected = {
        "assets/icon.png": (256, 256),
        "assets/logo.png": (1024, 1024),
        "assets/logo-dark.png": (1024, 1024),
    }
    for relative, dimensions in expected.items():
        path = PLUGIN / relative
        if _png_dimensions(path) != dimensions:
            raise ValidationError(f"unexpected dimensions for {path.relative_to(ROOT)}")

    forbidden_suffixes = {".agentpack", ".db", ".sqlite", ".pem", ".key", ".env"}
    for path in PLUGIN.rglob("*"):
        if path.is_symlink():
            raise ValidationError(f"plugin must not contain symlinks: {path.relative_to(ROOT)}")
        if path.is_file() and path.suffix.lower() in forbidden_suffixes:
            raise ValidationError(f"forbidden plugin payload: {path.relative_to(ROOT)}")


def validate() -> str:
    version = _validate_version_contract()
    _validate_marketplaces()
    _validate_skills()
    _validate_assets_and_contents()
    return version


def main() -> None:
    version = validate()
    print(f"Agent Port plugin validation passed for {version}")


if __name__ == "__main__":
    main()
