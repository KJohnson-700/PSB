#!/usr/bin/env python3
"""
One-shot security checks for PSB (dependency CVEs + static analysis).

  pip install -r requirements-dev.txt   # first time / after clone
  python scripts/run_security_suite.py

Exit non-zero if pip-audit finds vulnerabilities or bandit reports issues
at the configured level.

pip-audit: by default audits the **current Python environment** (same as ``pip list``),
which avoids temp-venv/ensurepip failures on some Python builds. Use
``--audit-requirements`` to audit ``requirements*.txt`` in isolated venvs (needs working ensurepip).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(cmd: list[str], *, cwd: Path) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(cwd))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--bandit-level",
        default="medium",
        choices=("low", "medium", "high"),
        help="Bandit minimum severity (default: medium)",
    )
    ap.add_argument(
        "--skip-audit",
        action="store_true",
        help="Skip pip-audit (offline or known noisy lockfile)",
    )
    ap.add_argument(
        "--audit-requirements",
        action="store_true",
        help="Audit requirements.txt + requirements-railway.txt in isolated venvs (not all Python builds support this)",
    )
    ap.add_argument(
        "--strict-bandit",
        action="store_true",
        help="Do not skip known intentional findings (B104 bind 0.0.0.0 for PaaS, B602 local port cleanup)",
    )
    ap.add_argument(
        "--skip-bandit",
        action="store_true",
        help="Skip bandit",
    )
    args = ap.parse_args()

    code = 0
    req = REPO / "requirements.txt"
    req_rail = REPO / "requirements-railway.txt"

    if not args.skip_audit:
        if args.audit_requirements:
            if not req.exists():
                print("ERROR: requirements.txt not found", file=sys.stderr)
                return 2
            for label, path in (("app", req), ("railway", req_rail)):
                if not path.exists():
                    continue
                r = run(
                    [
                        sys.executable,
                        "-m",
                        "pip_audit",
                        "--requirement",
                        str(path),
                        "--desc",
                        "on",
                    ],
                    cwd=REPO,
                )
                if r != 0:
                    print(
                        f"[security] pip-audit FAILED for {label} ({path.name})",
                        file=sys.stderr,
                    )
                    code = max(code, r)
        else:
            r = run(
                [sys.executable, "-m", "pip_audit", "--desc", "on"],
                cwd=REPO,
            )
            if r != 0:
                print("[security] pip-audit FAILED (current venv)", file=sys.stderr)
                code = max(code, r)

    if not args.skip_bandit:
        # Bandit: -ll = report medium+, -lll = high only (see `bandit -h`)
        sev_flag = {"low": "-l", "medium": "-ll", "high": "-lll"}[args.bandit_level]
        # One -x: comma-separated relative path segments (bandit docs)
        cmd: list[str] = [
            sys.executable,
            "-m",
            "bandit",
        ]
        if not args.strict_bandit:
            # B104: 0.0.0.0 bind for PaaS; B602: local dev port-free helpers only
            cmd.extend(["-s", "B104,B602"])
        cmd.extend(
            [
                "-r",
                str(REPO / "src"),
                str(REPO / "scripts"),
                "-x",
                "tests,.venv,projects,data",
                "-f",
                "txt",
                sev_flag,
            ]
        )
        r = run(cmd, cwd=REPO)
        if r != 0:
            print("[security] bandit reported issues — review output above", file=sys.stderr)
            code = max(code, r)

    if code == 0:
        print("Security suite: OK (no failing checks).")
    return code


if __name__ == "__main__":
    sys.exit(main())
