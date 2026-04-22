"""Guard dashboard index.html + HTTP shell so pre-restart checks include the UI bundle."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "src" / "dashboard" / "index.html"


def _fetchall_promise_all_block(html: str) -> tuple[list[str], int]:
    """Parse fetchAll()'s main Promise.all — 18-way poll; tolerate let + split try/catch."""
    m = re.search(
        r"(?:const )?\[([^\]]+)\]\s*=\s*await Promise\.all\(\[([\s\S]*?)\]\);\s*\n\s*\} catch",
        html,
    )
    assert m, "fetchAll() Promise.all block not found (expected `] = await Promise.all([` then `} catch`)"
    names = [x.strip() for x in m.group(1).split(",")]
    block = m.group(2)
    fetches = len(re.findall(r"\bfetch\(", block))
    return names, fetches


def test_dashboard_index_fetchall_bind_count_matches_fetch_calls():
    html = INDEX.read_text(encoding="utf-8")
    names, fetches = _fetchall_promise_all_block(html)
    assert len(names) == fetches, (
        f"fetchAll destructuring has {len(names)} vars but Promise.all has {fetches} "
        f"fetch() calls — missing/extra binding breaks the whole dashboard in-browser."
    )


def test_dashboard_index_serves_and_health_has_ui_rev():
    from fastapi.testclient import TestClient

    from src.dashboard.server import app

    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "text/html" in (r.headers.get("content-type") or "")
    body = r.text
    assert "fetchAll" in body and "PolyBot" in body

    h = c.get("/health")
    assert h.status_code == 200
    data = h.json()
    assert data.get("status") == "ok"
    assert data.get("dashboard_ui_rev"), "bump dashboard_ui_rev in server.py when shipping HTML/JS"

    snippet = c.get("/api/dashboard/health-snippet")
    assert snippet.status_code == 200
    assert "text/html" in (snippet.headers.get("content-type") or "")
    assert data.get("dashboard_ui_rev") in snippet.text
