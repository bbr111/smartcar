"""Update the manifest file."""

import json
from pathlib import Path
import re
import sys


def normalize_version(version: str) -> str:
    """Return a Home Assistant compatible version key.

    Tags may carry a non-numeric prefix (a leading "v" or a fork marker like
    "bbr0.0.1"). Home Assistant rejects manifests whose version is not a valid
    version, so drop everything before the first numeric component while
    keeping any trailing SemVer pre-release ("-bbr") or build ("+bbr") suffix,
    e.g. "bbr0.0.1" -> "0.0.1" and "v0.0.1+bbr" -> "0.0.1+bbr".
    """
    return re.sub(r"^[^0-9]*", "", version.strip())


def update_manifest():
    """Update the manifest file."""
    version = "0.0.0"
    for index, value in enumerate(sys.argv):
        if value in {"--version", "-V"}:
            version = sys.argv[index + 1]

    version = normalize_version(version)

    path = Path(f"{Path.cwd()}/custom_components/smartcar/manifest.json")

    with path.open(encoding="utf-8") as manifestfile:
        manifest = json.load(manifestfile)

    manifest["version"] = version

    path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )


update_manifest()
