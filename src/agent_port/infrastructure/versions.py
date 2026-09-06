from __future__ import annotations

import re
from collections.abc import Iterable

VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?")


def compatibility_profile(value: str | None) -> str | None:
    if value is None:
        return None
    match = VERSION_PATTERN.search(value)
    return f"{match.group(1)}.{match.group(2)}" if match else None


def compatibility_profiles(values: Iterable[str]) -> list[str]:
    return sorted({profile for value in values if (profile := compatibility_profile(value))})
