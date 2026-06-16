#!/usr/bin/env python3
"""Audit SolMacroStrategy (sol_macro.py) for HARDCODED thresholds in SHARED
methods — the only real cross-alt bleed surface.

A shared method runs identically for sol/xrp/hype/bnb/doge (subclasses override
only a handful). A literal inside a Compare that is NOT a config.get(key,DEFAULT)
default is a "bare" threshold: it cannot be tuned per-asset without a code edit,
so it applies to all five alts at once. Those are what we want to find.

We exclude:
  - literals that are the default arg of a .get(...) / getattr(...) call
    (overridable per-asset via that asset's config block)
  - trivial structural literals (0, 1, -1, None comparisons, len()==0, etc.)
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "strategies" / "sol_macro.py"

# methods a subclass overrides -> NOT shared identically (skip from bleed list)
OVERRIDDEN = {
    "_build_alt_service", "__init__", "_alt_asset_code",
    "_is_solana_market", "_is_updown_market",
    "scan_and_analyze",  # hype & bnb override; still shared by sol/xrp/doge
}
TRIVIAL = {0, 1, -1, 2, 0.0, 1.0, 100, 1000}  # structural / index / pct-scale


def get_default_literal_nodes(tree):
    """Collect id() of Constant nodes that are a default arg of .get/getattr."""
    defaults = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", getattr(fn, "id", ""))
            if name in ("get", "getattr"):
                for a in node.args[1:]:
                    for sub in ast.walk(a):
                        if isinstance(sub, ast.Constant):
                            defaults.add(id(sub))
    return defaults


def main():
    src = SRC.read_text()
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "SolMacroStrategy")
    defaults = get_default_literal_nodes(cls)

    findings = []  # (method, lineno, op_text, literal)
    for m in cls.body:
        if not isinstance(m, ast.FunctionDef):
            continue
        shared = m.name not in OVERRIDDEN
        if not shared:
            continue
        for node in ast.walk(m):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left] + list(node.comparators)
            for operand in operands:
                if isinstance(operand, ast.Constant) and id(operand) not in defaults:
                    v = operand.value
                    if isinstance(v, bool) or v is None:
                        continue
                    if isinstance(v, (int, float)) and v in TRIVIAL:
                        continue
                    if isinstance(v, str):
                        continue  # string compares = regime/side labels, not thresholds
                    findings.append((m.name, operand.lineno, v))

    # group by method
    by_method = {}
    for meth, ln, v in findings:
        by_method.setdefault(meth, []).append((ln, v))

    print(f"Shared methods with BARE numeric thresholds (sol_macro.py)\n"
          f"  = applies identically to sol/xrp/hype/bnb/doge, not per-asset\n")
    print(f"{'method':<42} {'line':>5}  literal")
    print("-" * 64)
    for meth in sorted(by_method, key=lambda k: by_method[k][0][0]):
        for ln, v in by_method[meth]:
            print(f"{meth:<42} {ln:>5}  {v}")
    print(f"\n{len(findings)} bare thresholds across {len(by_method)} shared methods.")
    print("Next: review which are decision-affecting (side/admit/edge/size) vs benign.")


if __name__ == "__main__":
    main()
