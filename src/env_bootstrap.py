"""
Load local secrets for development: project root `.env`, then `config/secrets.env`
(overrides). Uses pathlib — safe on Windows and Unix.

Railway/Docker typically inject env only; missing files is normal there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def project_root_from_here() -> Path:
    """Repo root (parent of `src/`)."""
    return Path(__file__).resolve().parent.parent


def load_project_dotenv(
    project_root: Optional[Path] = None, *, quiet: bool = False
) -> None:
    """Merge env files into os.environ."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        if not quiet:
            print(
                "WARNING: python-dotenv not installed. Using process environment only."
            )
        return

    root = project_root or project_root_from_here()
    root_env = root / ".env"
    secrets = root / "config" / "secrets.env"

    try:
        if secrets.exists():
            if root_env.exists():
                load_dotenv(dotenv_path=root_env, override=False)
            load_dotenv(dotenv_path=secrets, override=True)
        elif root_env.exists():
            load_dotenv(dotenv_path=root_env, override=True)
        elif not quiet:
            print(
                f"INFO: No {secrets} and no {root_env} — "
                "using process environment (expected on Railway/Docker)."
            )
    except Exception as e:
        if not quiet:
            print(f"ERROR: Could not load .env files: {e}")
