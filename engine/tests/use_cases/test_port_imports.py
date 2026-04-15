"""Verify domain.ports has no upward imports from use_cases."""
import ast
import pathlib


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
