"""Load a pipeline publish snapshot after the authoritative validator accepts it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .errors import PublishError
from .result_store import sha256_file


PLUGIN_ROOT = Path(__file__).resolve().parents[4]
SNAPSHOT_VALIDATOR = PLUGIN_ROOT / "scripts" / "build_publish_snapshot.py"


def load_pipeline_snapshot(value: str | None, canonical: Path, mode: str) -> dict:
    if not value:
        raise PublishError("pipeline publishing requires --snapshot")
    path = Path(value).expanduser().resolve()
    expected = canonical / ".pipeline" / "publish-snapshot.json"
    if path != expected:
        raise PublishError(f"pipeline publish snapshot must be {expected}")
    result = subprocess.run(
        [sys.executable, str(SNAPSHOT_VALIDATOR), str(canonical), "--validate"],
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    if result.returncode != 0:
        detail = (result.stdout or result.stderr).strip()
        raise PublishError(f"publish snapshot validation failed: {detail}")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PublishError(f"unable to read publish snapshot: {err}") from err
    if snapshot.get("mode") != mode:
        raise PublishError(f"publish snapshot mode must be {mode}")
    return {
        "path": path,
        "sha256": sha256_file(path),
        "fingerprint": snapshot.get("fingerprint"),
        "account": snapshot.get("account"),
        "data": snapshot,
    }

