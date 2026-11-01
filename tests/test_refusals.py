"""Every code the API refuses with is explained in `refusals.py`, and every
explanation there is of a code still raised: read from the source itself."""

import ast
import re
from pathlib import Path

from registry_api import refusals
from registry_api.main import app

SOURCE = Path(__file__).resolve().parent.parent / "src" / "registry_api"
CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
# Calls whose string arguments are refusal codes: the exceptions that carry
# one to the client, and the helpers that build them.
REFUSING = {
    "_refuse",
    "refusal",
    "Refusal",
    "LogError",
    "WalletError",
    "DomainError",
    "MediatorError",
    "ProviderError",
}
# Values written in backticks beside codes in a response's description.
NOT_CODES = {"single_user", "any_admin"}


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _results(node: ast.AST) -> set[str]:
    """The strings an expression may come to: a constant, or either side of
    a conditional — never what its condition compares."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _results(node.body) | _results(node.orelse)
    return set()


def _strings(node: ast.AST) -> set[str]:
    return {
        value.value
        for value in ast.walk(node)
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }


def _raised() -> tuple[set[str], set[str]]:
    """The codes the source refuses with, and the presentation problems."""
    codes: set[str] = set()
    problems: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        if path.name == "refusals.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = _name(node)
                if name == "Problem":
                    problems |= {value for arg in node.args for value in _results(arg)}
                elif name == "HTTPException":
                    for keyword in node.keywords:
                        if keyword.arg == "detail":
                            codes |= {v for v in _strings(keyword.value) if CODE.match(v)}
                elif name == "check":
                    # throttle.check(db, "<code>", …)
                    codes |= {v for arg in node.args[1:2] for v in _strings(arg) if CODE.match(v)}
                elif name in REFUSING:
                    codes |= {v for arg in node.args for v in _strings(arg) if CODE.match(v)}
                for keyword in node.keywords:
                    if keyword.arg == "refusal":
                        codes |= {v for v in _strings(keyword.value) if CODE.match(v)}
    return codes, problems


def _documented() -> set[str]:
    """The codes the OpenAPI document names in its responses."""
    found: set[str] = set()
    for operations in app.openapi()["paths"].values():
        for operation in operations.values():
            for status, response in operation.get("responses", {}).items():
                if status[0] in "45":
                    found |= set(
                        re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", response["description"])
                    )
    return found - NOT_CODES


def test_every_refusal_is_explained() -> None:
    codes, problems = _raised()
    assert codes - refusals.REFUSALS.keys() == set()
    assert _documented() - refusals.REFUSALS.keys() == set()
    assert problems - refusals.PROBLEMS.keys() == set()


def test_nothing_is_explained_that_is_no_longer_raised() -> None:
    source = "\n".join(
        path.read_text() for path in SOURCE.rglob("*.py") if path.name != "refusals.py"
    )
    for code in [*refusals.REFUSALS, *refusals.PROBLEMS]:
        assert f'"{code}"' in source, code


def test_the_openapi_document_publishes_them() -> None:
    schema = app.openapi()
    assert schema["x-refusals"] == refusals.REFUSALS
    assert schema["x-presentation-problems"] == refusals.PROBLEMS
    assert all(meaning.endswith(".") for meaning in refusals.REFUSALS.values())
