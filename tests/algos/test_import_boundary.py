from __future__ import annotations

import ast
from pathlib import Path


FORBIDDEN_TRADER_IMPORTS = (
    "trader.provider",
    "trader.execution",
    "trader.console",
)
FORBIDDEN_LIBRARY_IMPORTS = (
    "yaml",
    "requests",
    "urllib",
    "urllib.request",
    "socket",
    "httpx",
    "http",
)
FORBIDDEN_FILE_IO_PATTERNS = (
    "open(",
    ".read_text(",
    ".write_text(",
    "datetime.now(",
    "time.time(",
    "pd.read_",
    ".to_parquet(",
    ".to_csv(",
)


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("could not locate repository root from test file")


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            if node.module == "trader":
                modules.update(f"trader.{alias.name}" for alias in node.names)
    return modules


def _matches_module_prefix(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(f"{prefix}.")


def test_algos_package_has_no_forbidden_imports_or_file_io() -> None:
    repo_root = _repo_root()
    algos_root = repo_root / "src" / "trader" / "algos"
    forbidden_imports = FORBIDDEN_TRADER_IMPORTS + FORBIDDEN_LIBRARY_IMPORTS
    violations: list[str] = []

    for source_path in sorted(algos_root.rglob("*.py")):
        source = source_path.read_text()
        relative_path = source_path.relative_to(repo_root)
        tree = ast.parse(source, filename=str(relative_path))

        for module in sorted(_imported_modules(tree)):
            if any(
                _matches_module_prefix(module, prefix)
                for prefix in forbidden_imports
            ):
                violations.append(
                    f"{relative_path}: forbidden import {module!r}"
                )

        for pattern in FORBIDDEN_FILE_IO_PATTERNS:
            if pattern in source:
                violations.append(
                    f"{relative_path}: forbidden filesystem pattern {pattern!r}"
                )

    assert not violations, (
        "trader.algos must remain a pure strategy library:\n"
        + "\n".join(violations)
    )
