"""Lightweight AST extraction using tree-sitter."""

from __future__ import annotations

import os
from typing import Any

_EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
}


def _get_parser(language: str) -> Any | None:
    """Return a tree-sitter ``Parser`` for *language*, or *None*."""
    try:
        import tree_sitter as ts  # type: ignore[import-untyped]
    except ImportError:
        return None

    lang_obj: Any | None = None
    try:
        if language == "python":
            import tree_sitter_python as tsp  # type: ignore[import-untyped]
            lang_obj = ts.Language(tsp.language())
        elif language == "javascript":
            import tree_sitter_javascript as tsjs  # type: ignore[import-untyped]
            lang_obj = ts.Language(tsjs.language())
        elif language == "typescript":
            # tree-sitter-typescript may not be installed
            import tree_sitter_javascript as tsjs  # type: ignore[import-untyped]
            lang_obj = ts.Language(tsjs.language())
    except Exception:
        return None

    if lang_obj is None:
        return None

    parser = ts.Parser(lang_obj)
    return parser


# ── Python extraction ────────────────────────────────────────────────────

def _extract_python(tree: Any) -> list[dict]:
    declarations: list[dict] = []
    root = tree.root_node
    for child in root.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            declarations.append({
                "name": name_node.text.decode() if name_node else "<unknown>",
                "type": "function",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "is_exported": True,
            })
        elif child.type == "class_definition":
            name_node = child.child_by_field_name("name")
            declarations.append({
                "name": name_node.text.decode() if name_node else "<unknown>",
                "type": "class",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "is_exported": True,
            })
        elif child.type == "expression_statement":
            expr = child.children[0] if child.children else None
            if expr and expr.type == "assignment":
                left = expr.child_by_field_name("left")
                if left and left.text.decode() == "__all__":
                    declarations.append({
                        "name": "__all__",
                        "type": "variable",
                        "line_start": child.start_point[0] + 1,
                        "line_end": child.end_point[0] + 1,
                        "is_exported": True,
                    })
    return declarations


# ── JS/TS extraction ────────────────────────────────────────────────────

_JS_EXPORT_TYPES = {
    "export_statement",
    "function_declaration",
    "class_declaration",
}


def _extract_js_ts(tree: Any) -> list[dict]:
    declarations: list[dict] = []
    root = tree.root_node
    for child in root.children:
        if child.type == "export_statement":
            # Try to find the inner declaration
            name = "<export>"
            decl_type = "export"
            for sub in child.children:
                if sub.type in ("function_declaration", "class_declaration"):
                    name_node = sub.child_by_field_name("name")
                    if name_node:
                        name = name_node.text.decode()
                    decl_type = sub.type.replace("_declaration", "")
                    break
                if sub.type == "lexical_declaration":
                    for declarator in sub.children:
                        if declarator.type == "variable_declarator":
                            vn = declarator.child_by_field_name("name")
                            if vn:
                                name = vn.text.decode()
                            decl_type = "variable"
                            break
                    break
            declarations.append({
                "name": name,
                "type": decl_type,
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "is_exported": True,
            })
        elif child.type == "function_declaration":
            name_node = child.child_by_field_name("name")
            declarations.append({
                "name": name_node.text.decode() if name_node else "<unknown>",
                "type": "function",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "is_exported": False,
            })
        elif child.type == "class_declaration":
            name_node = child.child_by_field_name("name")
            declarations.append({
                "name": name_node.text.decode() if name_node else "<unknown>",
                "type": "class",
                "line_start": child.start_point[0] + 1,
                "line_end": child.end_point[0] + 1,
                "is_exported": False,
            })
    return declarations


# ── public API ───────────────────────────────────────────────────────────

def extract_top_level_declarations(file_path: str, content: str) -> dict:
    """Extract top-level declarations from *content* based on file extension.

    Returns a dict with ``language``, ``declarations``, and ``line_count``.
    """
    _, ext = os.path.splitext(file_path)
    line_count = content.count("\n") + 1 if content else 0
    language = _EXTENSION_LANGUAGE_MAP.get(ext, "unknown")

    if language == "unknown":
        return {"language": "unknown", "declarations": [], "line_count": line_count}

    parser = _get_parser(language)
    if parser is None:
        return {"language": language, "declarations": [], "line_count": line_count}

    tree = parser.parse(content.encode())

    if language == "python":
        decls = _extract_python(tree)
    elif language in ("javascript", "typescript"):
        decls = _extract_js_ts(tree)
    else:
        decls = []

    return {"language": language, "declarations": decls, "line_count": line_count}
