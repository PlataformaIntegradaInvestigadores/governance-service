import ast
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
ALLOWED_INTERNAL = {
    "domain": ("app.domain",),
    "application": ("app.domain", "app.application"),
}


def imported_modules(path: Path) -> set[str]:
    modules = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def is_allowed(module: str, layer: str) -> bool:
    if module.split(".")[0] in sys.stdlib_module_names - {"sqlite3"}:
        return True
    return module.startswith(ALLOWED_INTERNAL[layer])


@pytest.mark.parametrize("layer", sorted(ALLOWED_INTERNAL))
def test_inner_layers_depend_only_on_standard_library_and_inner_layers(layer):
    files = sorted((APP / layer).rglob("*.py"))
    violations = [
        f"{path.relative_to(APP)}: {module}"
        for path in files
        for module in sorted(imported_modules(path))
        if not is_allowed(module, layer)
    ]

    assert files
    assert violations == []


def test_dependency_check_detects_framework_and_outer_layer_imports():
    assert not is_allowed("fastapi", "domain")
    assert not is_allowed("sqlite3", "application")
    assert not is_allowed("app.infrastructure.qa_response", "application")
    assert not is_allowed("app.application.assess_generation", "domain")
    assert is_allowed("dataclasses", "domain")
