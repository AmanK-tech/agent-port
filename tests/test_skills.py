from __future__ import annotations

import os
from pathlib import Path

from agent_port.domain.models import Severity, SkillOwner, SkillScope
from agent_port.infrastructure.filesystem.skills import inspect_skill


def test_escaping_symlink_makes_skill_ineligible(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: unsafe\ndescription: Unsafe test.\n---\n", encoding="utf-8"
    )
    try:
        os.symlink(outside, skill / "outside-link")
    except OSError:
        import pytest

        pytest.skip("symlink creation is unavailable on this platform")
    record = inspect_skill(skill, SkillOwner.USER, SkillScope.USER)
    assert record.eligible is False
    assert any(
        warning.code == "escaping-symlink" and warning.severity is Severity.ERROR
        for warning in record.warnings
    )


def test_managed_skill_is_never_eligible(tmp_path: Path) -> None:
    skill = tmp_path / "managed"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: managed\ndescription: Managed test.\n---\n", encoding="utf-8"
    )
    record = inspect_skill(skill, SkillOwner.PLUGIN, SkillScope.PLUGIN)
    assert record.eligible is False


def test_plugin_cache_does_not_emit_irrelevant_portability_warnings(tmp_path: Path) -> None:
    skill = tmp_path / "cached-plugin"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: cached-plugin\ndescription: Plugin cache.\n---\n"
        "\nExample path: /Users/someone/project\napi_key: placeholder\n",
        encoding="utf-8",
    )
    record = inspect_skill(skill, SkillOwner.PLUGIN, SkillScope.PLUGIN)

    assert record.eligible is False
    assert not {"absolute-path", "credential-like-field"}.intersection(
        warning.code for warning in record.warnings
    )
