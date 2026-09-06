from __future__ import annotations

import sys
from pathlib import Path

from agent_port.application.handoff import RestoreHandoffWorker


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    RestoreHandoffWorker().execute(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
