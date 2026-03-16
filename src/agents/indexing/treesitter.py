"""Tree-sitter based source file parser.

Extracts symbol definitions (functions, classes, methods, etc.) from source files
using tree-sitter grammars. Supports Python, JavaScript, TypeScript, Go, Rust,
Java, C, C++, and Ruby.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Map file extensions to tree-sitter language names
EXT_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
}


@dataclass
class Symbol:
    """A code symbol extracted from a source file."""
    name: str
    kind: str          # function, class, method, interface, type, variable, import
    line: int          # 1-indexed start line
    end_line: int      # 1-indexed end line
    file_path: str


# Tree-sitter query patterns per language
# Each entry is (language, query_string) — queries extract named symbol definitions
LANGUAGE_QUERIES: dict[str, str] = {
    "python": """
        (function_definition name: (identifier) @name) @def
        (class_definition name: (identifier) @name) @def
        (assignment left: (identifier) @name) @def
    """,
    "javascript": """
        (function_declaration name: (identifier) @name) @def
        (class_declaration name: (identifier) @name) @def
        (variable_declarator name: (identifier) @name) @def
        (method_definition name: (property_identifier) @name) @def
        (arrow_function) @def
    """,
    "typescript": """
        (function_declaration name: (identifier) @name) @def
        (class_declaration name: (identifier) @name) @def
        (variable_declarator name: (identifier) @name) @def
        (method_definition name: (property_identifier) @name) @def
        (interface_declaration name: (type_identifier) @name) @def
        (type_alias_declaration name: (type_identifier) @name) @def
    """,
    "tsx": """
        (function_declaration name: (identifier) @name) @def
        (class_declaration name: (identifier) @name) @def
        (variable_declarator name: (identifier) @name) @def
        (method_definition name: (property_identifier) @name) @def
        (interface_declaration name: (type_identifier) @name) @def
        (type_alias_declaration name: (type_identifier) @name) @def
    """,
    "go": """
        (function_declaration name: (identifier) @name) @def
        (method_declaration name: (field_identifier) @name) @def
        (type_declaration (type_spec name: (type_identifier) @name)) @def
    """,
    "rust": """
        (function_item name: (identifier) @name) @def
        (struct_item name: (type_identifier) @name) @def
        (enum_item name: (type_identifier) @name) @def
        (impl_item type: (type_identifier) @name) @def
        (trait_item name: (type_identifier) @name) @def
    """,
    "java": """
        (method_declaration name: (identifier) @name) @def
        (class_declaration name: (identifier) @name) @def
        (interface_declaration name: (identifier) @name) @def
    """,
    "c": """
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @def
        (struct_specifier name: (type_identifier) @name) @def
        (enum_specifier name: (type_identifier) @name) @def
    """,
    "cpp": """
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @def
        (class_specifier name: (type_identifier) @name) @def
        (struct_specifier name: (type_identifier) @name) @def
    """,
    "ruby": """
        (method name: (identifier) @name) @def
        (class name: (constant) @name) @def
        (module name: (constant) @name) @def
    """,
}

# Classify node types to symbol kinds
NODE_KIND_MAP: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
        "assignment": "variable",
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "variable_declarator": "variable",
        "method_definition": "method",
        "arrow_function": "function",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "variable_declarator": "variable",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    },
    "tsx": {
        "function_declaration": "function",
        "class_declaration": "class",
        "variable_declarator": "variable",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "class",
        "enum_item": "type",
        "impl_item": "class",
        "trait_item": "interface",
    },
    "java": {
        "method_declaration": "method",
        "class_declaration": "class",
        "interface_declaration": "interface",
    },
    "c": {
        "function_definition": "function",
        "struct_specifier": "class",
        "enum_specifier": "type",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "class",
    },
    "ruby": {
        "method": "method",
        "class": "class",
        "module": "class",
    },
}

_parser_cache: dict[str, object] = {}


def _get_parser(language: str):
    """Get or create a tree-sitter parser for a language."""
    if language in _parser_cache:
        return _parser_cache[language]

    try:
        import tree_sitter_languages
        parser = tree_sitter_languages.get_parser(language)
        _parser_cache[language] = parser
        return parser
    except Exception:
        logger.debug("tree-sitter parser not available for %s", language)
        _parser_cache[language] = None
        return None


def _get_language_obj(language: str):
    """Get the tree-sitter Language object for query compilation."""
    try:
        import tree_sitter_languages
        return tree_sitter_languages.get_language(language)
    except Exception:
        return None


def parse_file(file_path: str) -> list[Symbol]:
    """Parse a source file and extract symbol definitions.

    Returns an empty list if the language is not supported or parsing fails.
    Never raises — all errors are caught and logged.
    """
    path = Path(file_path)
    ext = path.suffix.lower()
    language = EXT_TO_LANGUAGE.get(ext)

    if not language:
        return []

    parser = _get_parser(language)
    if parser is None:
        return _fallback_parse(file_path, language)

    try:
        source = path.read_bytes()
        tree = parser.parse(source)
    except Exception as e:
        logger.debug("Failed to parse %s: %s", file_path, e)
        return _fallback_parse(file_path, language)

    symbols = []
    query_str = LANGUAGE_QUERIES.get(language)
    lang_obj = _get_language_obj(language)

    if query_str and lang_obj:
        try:
            symbols = _extract_with_query(tree, lang_obj, query_str, language, file_path)
        except Exception as e:
            logger.debug("Query-based extraction failed for %s: %s, falling back to walk", file_path, e)
            symbols = _extract_with_walk(tree, language, file_path)
    else:
        symbols = _extract_with_walk(tree, language, file_path)

    return symbols


def _extract_with_query(tree, lang_obj, query_str: str, language: str, file_path: str) -> list[Symbol]:
    """Extract symbols using tree-sitter queries."""
    from tree_sitter import Language

    # Compile query
    try:
        query = lang_obj.query(query_str)
    except Exception:
        # Query compilation can fail with some grammar versions
        return _extract_with_walk(tree, language, file_path)

    symbols = []
    captures = query.captures(tree.root_node)

    # Process captures: @name captures give us the symbol name,
    # @def captures give us the full definition node
    kind_map = NODE_KIND_MAP.get(language, {})

    # Group captures by their parent @def node
    i = 0
    while i < len(captures):
        node, capture_name = captures[i]

        if capture_name == "name":
            # Look for the corresponding @def (should be next or previous)
            name = node.text.decode("utf-8", errors="replace") if node.text else ""

            # Find the parent def node
            parent = node.parent
            while parent and parent.type not in kind_map:
                parent = parent.parent

            if parent and name:
                kind = kind_map.get(parent.type, "variable")
                symbols.append(Symbol(
                    name=name,
                    kind=kind,
                    line=parent.start_point[0] + 1,
                    end_line=parent.end_point[0] + 1,
                    file_path=file_path,
                ))
        i += 1

    # Deduplicate by (name, line)
    seen = set()
    deduped = []
    for s in symbols:
        key = (s.name, s.line)
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    return deduped


def _extract_with_walk(tree, language: str, file_path: str) -> list[Symbol]:
    """Fallback: extract symbols by walking the AST."""
    kind_map = NODE_KIND_MAP.get(language, {})
    target_types = set(kind_map.keys())
    symbols = []

    def walk(node):
        if node.type in target_types:
            # Try to find the name child
            name = _find_name_child(node)
            if name:
                symbols.append(Symbol(
                    name=name,
                    kind=kind_map[node.type],
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    file_path=file_path,
                ))
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return symbols


def _find_name_child(node) -> str | None:
    """Find the name identifier in a definition node."""
    name_types = {"identifier", "type_identifier", "property_identifier",
                  "field_identifier", "constant"}

    for child in node.children:
        if child.type in name_types:
            return child.text.decode("utf-8", errors="replace") if child.text else None
        # Check one level deeper (e.g., function_declarator > identifier)
        if child.type.endswith("_declarator"):
            for grandchild in child.children:
                if grandchild.type in name_types:
                    return grandchild.text.decode("utf-8", errors="replace") if grandchild.text else None
    return None


def _fallback_parse(file_path: str, language: str) -> list[Symbol]:
    """Regex-based fallback when tree-sitter is not available.

    Catches basic function/class definitions in common languages.
    """
    import re

    patterns: dict[str, list[tuple[str, str]]] = {
        "python": [
            (r'^(?:async\s+)?def\s+(\w+)', "function"),
            (r'^class\s+(\w+)', "class"),
        ],
        "javascript": [
            (r'(?:export\s+)?(?:async\s+)?function\s+(\w+)', "function"),
            (r'(?:export\s+)?class\s+(\w+)', "class"),
            (r'(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(', "function"),
        ],
        "typescript": [
            (r'(?:export\s+)?(?:async\s+)?function\s+(\w+)', "function"),
            (r'(?:export\s+)?class\s+(\w+)', "class"),
            (r'(?:export\s+)?interface\s+(\w+)', "interface"),
            (r'(?:export\s+)?type\s+(\w+)', "type"),
        ],
        "go": [
            (r'^func\s+(?:\([^)]+\)\s+)?(\w+)', "function"),
            (r'^type\s+(\w+)\s+struct', "class"),
            (r'^type\s+(\w+)\s+interface', "interface"),
        ],
        "rust": [
            (r'(?:pub\s+)?(?:async\s+)?fn\s+(\w+)', "function"),
            (r'(?:pub\s+)?struct\s+(\w+)', "class"),
            (r'(?:pub\s+)?enum\s+(\w+)', "type"),
            (r'(?:pub\s+)?trait\s+(\w+)', "interface"),
        ],
        "java": [
            (r'(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(', "method"),
            (r'(?:public\s+)?class\s+(\w+)', "class"),
            (r'(?:public\s+)?interface\s+(\w+)', "interface"),
        ],
    }

    lang_patterns = patterns.get(language, [])
    if not lang_patterns:
        return []

    symbols = []
    try:
        with open(file_path, "r", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []

    for i, line in enumerate(lines):
        for pattern, kind in lang_patterns:
            m = re.search(pattern, line)
            if m:
                name = m.group(1)
                if name and not name.startswith("_") or kind in ("class", "function"):
                    symbols.append(Symbol(
                        name=name,
                        kind=kind,
                        line=i + 1,
                        end_line=i + 1,  # Can't determine end line with regex
                        file_path=file_path,
                    ))

    return symbols
