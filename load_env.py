"""Load causelists/.env into os.environ (no-op if python-dotenv missing)."""

from __future__ import annotations

from pathlib import Path

_LOADED = False


def load_env() -> Path | None:
    """Load ``.env`` from the project root once. Returns the path if loaded."""
    global _LOADED
    if _LOADED:
        return None
    _LOADED = True
    root = Path(__file__).resolve().parent
    env_path = root / ".env"
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    if env_path.is_file():
        # Project .env wins over stale shell env (e.g. old PARSE_QUEUE=parse-jobs).
        load_dotenv(env_path, override=True)
        return env_path
    example = root / ".env.example"
    if example.is_file():
        load_dotenv(example, override=False)
        return example
    return None
