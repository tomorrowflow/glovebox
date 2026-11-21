"""Reusable check that a tool's JSON schema is closed (ADR 0001, PRD "Permitted fields").

A closed schema has no unconstrained string and no open object, so nothing a
source wrote can fit in it. Every later tool's output schema goes through
`closed_schema_problems`.
"""

from typing import Any

ANCHORED = ("^", "$")


def closed_schema_problems(schema: dict[str, Any]) -> list[str]:
    """What is open in `schema`: each problem as a path and a reason. Empty means closed."""
    problems: list[str] = []
    _walk(schema, schema, "#", problems)
    return problems


def _resolve(root: dict[str, Any], reference: str) -> dict[str, Any]:
    assert reference.startswith("#/"), f"only local references are allowed: {reference}"
    node: Any = root
    for part in reference[2:].split("/"):
        node = node[part]
    assert isinstance(node, dict)
    return node


def _walk(root: dict[str, Any], node: Any, path: str, problems: list[str]) -> None:
    if not isinstance(node, dict):
        problems.append(f"{path}: not a schema object")
        return
    if "$ref" in node:
        _walk(root, _resolve(root, node["$ref"]), path, problems)
        return
    for keyword in ("anyOf", "oneOf", "allOf"):
        for index, branch in enumerate(node.get(keyword, [])):
            _walk(root, branch, f"{path}/{keyword}[{index}]", problems)
    kind = node.get("type")
    if kind == "string":
        _check_string(node, path, problems)
    elif kind == "object":
        if node.get("additionalProperties") is not False:
            problems.append(f"{path}: object is open")
        for name, child in node.get("properties", {}).items():
            _walk(root, child, f"{path}/{name}", problems)
    elif kind == "array":
        if "maxItems" not in node:
            problems.append(f"{path}: array has no maxItems")
        _walk(root, node.get("items"), f"{path}/[]", problems)
    elif kind in ("boolean",):
        pass
    elif kind in ("integer", "number"):
        if "minimum" not in node or "maximum" not in node:
            problems.append(f"{path}: number has no bounds")
    elif "enum" in node or "const" in node:
        pass
    elif not any(keyword in node for keyword in ("anyOf", "oneOf", "allOf")):
        problems.append(f"{path}: no type")


def _check_string(node: dict[str, Any], path: str, problems: list[str]) -> None:
    if "enum" in node or "const" in node:
        return
    pattern = node.get("pattern")
    if not (
        isinstance(pattern, str)
        and pattern.startswith(ANCHORED[0])
        and pattern.endswith(ANCHORED[1])
    ):
        problems.append(f"{path}: string is neither an enum nor an anchored pattern")
    if "maxLength" not in node:
        problems.append(f"{path}: string has no maxLength")
