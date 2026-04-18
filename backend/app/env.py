"""Zero-dependency `.env` loader.

Walks up from the backend package to find a `.env` at the repo root and
fills in any keys that aren't already set in `os.environ`. Deliberately
minimal — no interpolation, no multiline values — because our `.env`
only needs to carry a handful of scalar port/host settings.
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_env_file(start: Path) -> Path | None:
    for parent in (start, *start.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(path: str | os.PathLike[str] | None = None) -> None:
    """Merge `.env` values into `os.environ` without overriding existing vars.

    Real environment beats file — so `BACKEND_PORT=9000 uv run ...` still wins
    over whatever the committed `.env` says.
    """
    env_path = Path(path) if path else _find_env_file(Path(__file__).resolve().parent)
    if env_path is None or not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        os.environ[key] = value
