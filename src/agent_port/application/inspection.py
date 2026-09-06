from __future__ import annotations

from pathlib import Path

from agent_port.application.registry import AdapterRegistry
from agent_port.domain.models import ArchiveInspectionReport, InspectionReport
from agent_port.infrastructure.archive import AgentPackReader


class InspectService:
    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        archive_reader: AgentPackReader | None = None,
    ) -> None:
        self._registry = registry or AdapterRegistry()
        self._archive_reader = archive_reader or AgentPackReader()

    def execute(
        self,
        source: Path,
        requested_harness: str = "auto",
        user_home: Path | None = None,
    ) -> InspectionReport | ArchiveInspectionReport:
        source = source.expanduser().resolve()
        if source.suffix == ".agentpack":
            return self._archive_reader.inspect(source)
        adapter = self._registry.select(source, requested_harness)
        return adapter.inspect(source, user_home.expanduser().resolve() if user_home else None)
