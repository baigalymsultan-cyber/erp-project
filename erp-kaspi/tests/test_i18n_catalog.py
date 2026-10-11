"""Static and runtime checks for the single RU/KK frontend catalog.

The current ERP contract is Russian/Kazakh (``ru``/``kk``). These tests use
AST rather than only the imported dictionary so duplicate literal keys cannot
be hidden by Python's last-write-wins behavior.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from string import Formatter

import pytest

from frontend.app import I18N

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_TECHNICAL_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_LANGUAGE_KEYS = frozenset({"ru", "kk"})


def _frontend_trees() -> list[tuple[Path, ast.Module]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        for path in sorted(_FRONTEND_DIR.glob("*.py"))
    ]


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _root_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_catalog_mapping(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases and node.id != "I18N"
    if isinstance(node, ast.Subscript):
        return _root_name(node) == "I18N"
    return False


def _referenced_catalog_keys() -> set[str]:
    referenced: set[str] = set()

    class CatalogAccessVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.aliases = {"I18N"}

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.module == "frontend.app":
                for imported in node.names:
                    if imported.name == "I18N":
                        self.aliases.add(imported.asname or imported.name)
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            if _root_name(node.value) == "I18N":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.aliases.add(target.id)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if (
                node.value is not None
                and _root_name(node.value) == "I18N"
                and isinstance(node.target, ast.Name)
            ):
                self.aliases.add(node.target.id)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:
            if _is_catalog_mapping(node.value, self.aliases):
                key = _literal_string(node.slice)
                if key is not None and key not in _LANGUAGE_KEYS:
                    referenced.add(key)
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and _is_catalog_mapping(node.func.value, self.aliases)
                and node.args
            ):
                key = _literal_string(node.args[0])
                if key is not None and key not in _LANGUAGE_KEYS:
                    referenced.add(key)
            self.generic_visit(node)

    for _, tree in _frontend_trees():
        CatalogAccessVisitor().visit(tree)
    return referenced


def _catalog_dict_nodes() -> dict[str, ast.Dict]:
    result: dict[str, ast.Dict] = {}
    for path, tree in _frontend_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "I18N" for target in node.targets
            ):
                continue
            if not isinstance(node.value, ast.Dict):
                raise AssertionError(f"I18N must be a literal dict: {path}:{node.lineno}")
            for key_node, value_node in zip(
                node.value.keys, node.value.values, strict=True
            ):
                lang = _literal_string(key_node)
                if lang in _LANGUAGE_KEYS and isinstance(value_node, ast.Dict):
                    result[lang] = value_node
    return result


def _duplicate_literal_keys(node: ast.Dict) -> list[str]:
    keys = [
        key
        for key_node in node.keys
        if (key := _literal_string(key_node)) is not None
    ]
    return sorted(key for key, count in Counter(keys).items() if count > 1)


def _assignment_count(name: str) -> int:
    count = 0
    for _, tree in _frontend_trees():
        for node in ast.walk(tree):
            is_named_assign = isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets
            )
            is_named_annotated_assign = (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
            )
            if is_named_assign or is_named_annotated_assign:
                count += 1
    return count


def _placeholders(value: str) -> Counter[str]:
    return Counter(
        field_name
        for _, field_name, _, _ in Formatter().parse(value)
        if field_name is not None
    )


def test_catalogs_ru_kk_symmetric() -> None:
    ru, kk = I18N["ru"], I18N["kk"]
    assert set(ru) == set(kk)


@pytest.mark.parametrize("lang", ["ru", "kk"])
def test_catalog_values_are_user_text(lang: str) -> None:
    for key, value in I18N[lang].items():
        assert isinstance(value, str) and value.strip(), f"I18N[{lang}][{key!r}] is empty"
        if set(key) <= _TECHNICAL_KEY_CHARS and "_" in key:
            assert value.strip() != key, f"I18N[{lang}][{key!r}] exposes a raw key"


def test_i18n_catalog_has_no_duplicate_literal_keys_or_shadowed_tables() -> None:
    catalogs = _catalog_dict_nodes()
    assert set(catalogs) == set(_LANGUAGE_KEYS)
    for lang, node in catalogs.items():
        assert _duplicate_literal_keys(node) == [], f"duplicate {lang} keys at source level"
    assert _assignment_count("I18N") == 1
    assert _assignment_count("MODULE_SUBTITLES") == 1


def test_all_literal_catalog_accesses_exist_in_both_catalogs() -> None:
    referenced = _referenced_catalog_keys()
    unresolved = sorted(
        key for key in referenced if key not in I18N["ru"] or key not in I18N["kk"]
    )
    assert unresolved == [], f"frontend references missing catalog keys: {unresolved}"


def test_ru_kk_placeholders_have_parity() -> None:
    mismatches = {
        key: (_placeholders(I18N["ru"][key]), _placeholders(I18N["kk"][key]))
        for key in I18N["ru"]
        if _placeholders(I18N["ru"][key]) != _placeholders(I18N["kk"][key])
    }
    assert mismatches == {}, f"RU/KK format placeholders differ: {mismatches}"
