"""Pytest hooks — shared fixtures and session-level notices."""

import sys


def pytest_sessionfinish(session, exitstatus: int) -> None:
    """Print an unmissable footer after the suite so long runs visibly terminate."""
    line = "=" * 58
    if exitstatus == 0:
        label = "PASSED — all selected tests finished"
    else:
        label = f"FINISHED — exit status {exitstatus} (failures/errors present)"
    # stdout so it lands after pytest's own summary block in the terminal
    print(f"\n{line}\n  PYTEST COMPLETE · {label}\n{line}\n", file=sys.stdout, flush=True)
