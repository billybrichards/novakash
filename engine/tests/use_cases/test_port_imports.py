"""Verify domain.ports has no upward imports from use_cases, AND that no
use_case file imports symbols from domain.ports that live in use_cases.ports.

Regression guard for a Clean-Arch Phase 2 live bug where
``reconcile_positions.py`` still imported ``AlerterPort`` from
``domain.ports`` after the compat re-exports were removed — the import
raised at startup and silently gated off the entire live-trade execution
path (ExecuteTradeUseCase never wired → TRADE decisions logged but no
orders placed, no Telegram alerts).
"""
import ast
import pathlib


# Symbols that moved from domain.ports → use_cases.ports in PR #204
_MOVED_TO_USE_CASES_PORTS = {
    "AlerterPort",
    "Clock",
    "OrderExecutionPort",
    "TradeRecorderPort",
    "RiskManagerPort",
}


def test_domain_ports_no_use_cases_import():
    src = (pathlib.Path(__file__).parent.parent.parent / "domain" / "ports.py").read_text()
    tree = ast.parse(src)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            if module.startswith("use_cases"):
                violations.append(f"line {node.lineno}: {ast.unparse(node)}")
    assert not violations, (
        "domain/ports.py must not import from use_cases. Found:\n" + "\n".join(violations)
    )


def test_no_file_imports_moved_symbols_from_domain_ports():
    """Scan every engine .py file for ``from domain.ports import X`` where
    X is a symbol that now lives in use_cases.ports. Catches the exact
    class of stale-import bug that silently broke live execution.
    """
    engine_root = pathlib.Path(__file__).parent.parent.parent
    violations = []
    for py_file in engine_root.rglob("*.py"):
        # Skip generated/cache/test-fixture files
        if "__pycache__" in py_file.parts:
            continue
        try:
            tree = ast.parse(py_file.read_text())
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "domain.ports":
                for alias in node.names:
                    if alias.name in _MOVED_TO_USE_CASES_PORTS:
                        rel = py_file.relative_to(engine_root)
                        violations.append(
                            f"{rel}:{node.lineno}: imports {alias.name} from "
                            f"domain.ports — must be use_cases.ports"
                        )
    assert not violations, (
        "Stale imports of symbols moved to use_cases.ports:\n  "
        + "\n  ".join(violations)
    )
