from __future__ import annotations

from pathlib import Path

from agent_port.adapters.claude_code import ClaudeCodeAdapter
from agent_port.adapters.codex import CodexAdapter
from agent_port.domain.errors import DetectionError
from agent_port.domain.models import HarnessName
from agent_port.domain.ports import HarnessAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        adapters: list[HarnessAdapter] = [CodexAdapter(), ClaudeCodeAdapter()]
        self._adapters = {adapter.name: adapter for adapter in adapters}

    def select(self, source: Path, requested: str = "auto") -> HarnessAdapter:
        if requested != "auto":
            try:
                harness = HarnessName(requested)
            except ValueError as error:
                choices = ", ".join(["auto", *(item.value for item in HarnessName)])
                raise DetectionError(
                    f"Unknown harness {requested!r}; choose one of: {choices}"
                ) from error
            adapter = self._adapters[harness]
            result = adapter.detect(source)
            if not result.score:
                raise DetectionError(
                    "The source does not contain evidence for the requested "
                    f"{harness.value} harness."
                )
            return adapter

        results = sorted(
            ((adapter.detect(source), adapter) for adapter in self._adapters.values()),
            key=lambda item: item[0].score,
            reverse=True,
        )
        best, adapter = results[0]
        if not best.score:
            raise DetectionError(
                "No supported harness was detected. Use --harness only when the source layout "
                "is known."
            )
        if len(results) > 1:
            second = results[1][0]
            if second.score >= 2 and best.score - second.score <= 2:
                evidence = "; ".join(
                    f"{result.harness or 'unknown'}: {', '.join(result.evidence)}"
                    for result, _candidate in results
                    if result.score
                )
                raise DetectionError(f"Harness detection is ambiguous ({evidence}).")
        return adapter

    def for_harness(self, harness: HarnessName) -> HarnessAdapter:
        return self._adapters[harness]
