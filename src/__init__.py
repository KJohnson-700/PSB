# Package marker so `python -m src.main` works in containers and CI.

from __future__ import annotations

import os
import warnings


def _suppress_urllib3_libressl_warning() -> None:
    """urllib3 v2 emits NotOpenSSLWarning when CPython is built against LibreSSL (stock macOS).

    Connections still use TLS; this is urllib3's compatibility notice. Silence it for local runs
    unless PSB_VERBOSE_SSL=1 (truthy) to see the warning again.

    Long-term fix on Mac: use Homebrew / python.org Python linked to OpenSSL (see README).
    """
    v = (os.environ.get("PSB_VERBOSE_SSL") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return
    # Do not import urllib3 here: importing urllib3 can emit the warning first.
    # Filter by module + message prefix so the suppression applies before requests/urllib3 loads.
    warnings.filterwarnings(
        "ignore",
        category=Warning,
        module=r"urllib3(\..*)?$",
        message=r".*urllib3 v2 only supports OpenSSL 1\.1\.1\+.*",
    )


_suppress_urllib3_libressl_warning()
