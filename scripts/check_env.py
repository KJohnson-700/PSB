#!/usr/bin/env python3
"""Quick local environment sanity check for bot/backtest runs.

Usage:
  python scripts/check_env.py
"""

from __future__ import annotations

import importlib
import os
import platform
import ssl
import sys
import warnings
from typing import List, Tuple


def _suppress_urllib3_warning_for_probe() -> None:
    if os.environ.get("PSB_VERBOSE_SSL", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    warnings.filterwarnings(
        "ignore",
        category=Warning,
        module=r"urllib3(\..*)?$",
        message=r".*urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
    )


def _status(ok: bool) -> str:
    return "OK" if ok else "WARN"


def _check_python() -> Tuple[bool, str]:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    return ok, f"{major}.{minor}.{sys.version_info.micro} ({platform.python_implementation()})"


def _check_ssl() -> Tuple[bool, str]:
    version = ssl.OPENSSL_VERSION
    ok = "LibreSSL" not in version
    return ok, version


def _check_imports() -> List[Tuple[str, bool, str]]:
    modules = [
        "requests",
        "urllib3",
        "yaml",
        "dotenv",
        "web3",
    ]
    out: List[Tuple[str, bool, str]] = []
    for name in modules:
        try:
            mod = importlib.import_module(name)
            ver = getattr(mod, "__version__", "unknown")
            out.append((name, True, str(ver)))
        except Exception as exc:
            out.append((name, False, str(exc)))
    return out


def main() -> int:
    _suppress_urllib3_warning_for_probe()
    print("PSB environment check\n")
    print(f"Python executable: {sys.executable}")

    py_ok, py_detail = _check_python()
    ssl_ok, ssl_detail = _check_ssl()
    print(f"[{_status(py_ok)}] Python version : {py_detail}")
    print(f"[{_status(ssl_ok)}] SSL backend    : {ssl_detail}")

    print("\nDependency imports:")
    deps = _check_imports()
    for name, ok, detail in deps:
        print(f"[{_status(ok)}] {name:<8} -> {detail}")

    failures = [name for name, ok, _ in deps if not ok]
    if failures:
        print("\nMissing/broken imports:", ", ".join(failures))
        print("Install with: pip install -r requirements.txt")

    if not ssl_ok:
        print(
            "\nLibreSSL detected: this can trigger urllib3 NotOpenSSLWarning on macOS.\n"
            "Bot logs are filtered for this warning, but for a clean base environment use\n"
            "Python 3.11+ from Homebrew/python.org and a project venv."
        )

    if py_ok and ssl_ok and not failures:
        print("\nEnvironment looks healthy for local bot/backtests.")
        return 0

    print("\nEnvironment has warnings; see notes above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
