from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "agent-port"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def build(output_directory: Path) -> tuple[Path, Path]:
    version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    output_directory.mkdir(parents=True, exist_ok=True)
    archive = output_directory / f"agent-port-plugin-{version}.zip"

    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(PLUGIN.rglob("*")):
            if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
                continue
            relative = Path("agent-port") / path.relative_to(PLUGIN)
            info = zipfile.ZipInfo(relative.as_posix(), ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, path.read_bytes(), compresslevel=9)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Agent Port dual-harness plugin bundle.")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    archive, checksum = build(args.output)
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()
