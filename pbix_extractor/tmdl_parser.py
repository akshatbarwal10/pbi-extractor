"""
TMDL (Tabular Model Definition Language) parser.

TMDL is the text format a .pbip project uses for its semantic model —
one file per table under `definition/tables/`, plus `model.tmdl`,
`relationships.tmdl`, `cultures/` and so on. Recent Power BI Desktop
versions default to it, so without this a .pbip drop is mostly useless.

Design decision: this parser emits **TMSL-shaped dictionaries**, i.e.
exactly what `DataModelSchema` in a .pbit would have contained. That
means `model_extractor.extract_model()` consumes it unchanged and every
downstream layer — DAX analysis, findings, bridge, index — works with
no knowledge that TMDL was involved. One parser, zero forking.

Format, abbreviated:

    /// A description line
    table Sales
        isHidden

        column SalesKey
            dataType: int64
            summarizeBy: none

        column 'Margin Amount' = Sales[NetAmount] - Sales[Cost]
            dataType: decimal

        measure 'Total Sales' = SUM(Sales[NetAmount])
            formatString: #,##0

        measure 'Top Share' =
                VAR Top = TOPN(1, VALUES(Product[Name]), [Total Sales])
                RETURN DIVIDE(Top, 1)
            formatString: 0.0%

        partition Sales = m
            mode: import
            source =
                    let Source = Sql.Database("srv", "db") in Source

Structural rules implemented here:

  - indentation (tabs or spaces) defines nesting
  - `objectType name` opens a block
  - `objectType name = value` sets the block's *default property*
    (expression for a measure, source for a partition, and so on)
  - a bare `=` at end of line means the value is the indented block
    that follows
  - `property: value` assigns; a bare `property` is boolean true
  - `/// text` accumulates as the next object's description
  - `//  text` is a comment — only stripped at statement level, never
    inside a multi-line expression, because `//` is also a DAX comment
    and stripping it there would corrupt the measure.
"""
from __future__ import annotations

import re

# `objectType name` / `objectType name = inline value`
# Names are bare identifiers or single-quoted when they contain spaces.
DECL = re.compile(
    r"^(?P<kind>[A-Za-z][A-Za-z0-9_]*)"
    r"(?:\s+(?P<name>'(?:[^']|'')*'|[^\s=][^=]*?))?"
    r"\s*(?:=\s*(?P<value>.*))?$"
)
PROPERTY = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*:\s*(?P<value>.*)$")

# Object kinds that take a *default property* after `=`, and what that
# property is called in TMSL.
DEFAULT_PROPERTY = {
    "measure": "expression",
    "column": "expression",
    "calculationItem": "expression",
    "tablePermission": "filterExpression",
    "partition": "source",
    "expression": "expression",
    "table": None,
    "model": None,
    "relationship": None,
    "role": None,
    "hierarchy": None,
    "level": None,
    "calculationGroup": None,
    "annotation": "value",
    "extendedProperty": "value",
}

# Object kinds that legitimately appear without a name.
NAMELESS_KINDS = {"calculationGroup"}

# Properties whose TMSL value is a number rather than a string.
NUMERIC_PROPERTIES = {"ordinal", "precedence"}

BOOLEAN_TRUE = {"true", "yes"}
BOOLEAN_FALSE = {"false", "no"}


class TmdlError(Exception):
    pass


class _Node:
    __slots__ = ("kind", "name", "value", "properties", "children", "description")

    def __init__(self, kind: str, name: str | None, value: str | None):
        self.kind = kind
        self.name = name
        self.value = value
        self.properties: dict = {}
        self.children: list[_Node] = []
        self.description: str | None = None

    def kids(self, kind: str) -> list["_Node"]:
        return [c for c in self.children if c.kind == kind]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind} {self.name!r} props={len(self.properties)} kids={len(self.children)}>"


def _unquote(name: str | None) -> str | None:
    """TMDL quotes names containing spaces; '' is an escaped quote."""
    if name is None:
        return None
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    return name


def _indent_width(line: str) -> int:
    """Count leading whitespace, treating a tab as one level.

    TMDL is tab-indented by default but hand-edited files drift to
    spaces, so both are accepted. Tabs are weighted to 4 so a mixed
    file still nests in the right order.
    """
    width = 0
    for ch in line:
        if ch == "\t":
            width += 4
        elif ch == " ":
            width += 1
        else:
            break
    return width


def _coerce(key: str, raw: str):
    """Turn a TMDL property value into a JSON-ish Python value."""
    value = raw.strip()
    if value == "":
        return True  # bare property == boolean true
    low = value.lower()
    if low in BOOLEAN_TRUE:
        return True
    if low in BOOLEAN_FALSE:
        return False
    if key in NUMERIC_PROPERTIES or re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            return value
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _next_meaningful_indent(lines: list[str], i: int) -> int | None:
    while i < len(lines):
        if lines[i].strip():
            return _indent_width(lines[i])
        i += 1
    return None


def _parse_scope(lines: list[str], start: int, base_indent: int,
                 node: _Node | None, out: list[_Node]) -> int:
    """Parse one indentation scope.

    `node` is the object these lines belong to (None at document top
    level, where objects are collected into `out` instead). One function
    handles both because the grammar is identical at every depth — the
    only difference is where a parsed object gets attached.
    """
    i = start
    pending_description: list[str] = []

    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue

        indent = _indent_width(raw)
        if indent < base_indent:
            break

        stripped = raw.strip()

        # `///` descriptions attach to whatever object comes next.
        if stripped.startswith("///"):
            pending_description.append(stripped[3:].strip())
            i += 1
            continue
        # A statement-level `//` comment. Safe to drop here because a
        # DAX `//` only ever appears inside a multi-line value block,
        # and those are consumed wholesale rather than walked line by
        # line — so this never touches expression text.
        if stripped.startswith("//"):
            i += 1
            continue

        decl = DECL.match(stripped)
        prop = PROPERTY.match(stripped)

        kind = decl.group("kind") if decl else None
        name = _unquote(decl.group("name")) if decl else None
        value = decl.group("value") if decl else None

        # --- what kind of line is this? --------------------------------
        # A child object needs a known kind AND a name (`measure 'X' =`,
        # `column Y`, `level Year`), except for the handful of kinds
        # that are legitimately anonymous (`calculationGroup`).
        is_child = bool(
            decl and kind in DEFAULT_PROPERTY
            and (name or kind in NAMELESS_KINDS)
        )
        # A colon always means a property, never a declaration — this is
        # what separates `column: Year` (a hierarchy level's property)
        # from `column Year` (a real column object).
        if prop and prop.group("key") == kind and ":" in stripped.split("=", 1)[0]:
            is_child = False

        if is_child:
            if value is not None and value.strip() == "":
                value, i = _consume_block(lines, i + 1, indent)
            elif value is not None:
                value = value.strip()
                i += 1
            else:
                i += 1

            child = _Node(kind, name, value)
            if pending_description:
                child.description = " ".join(pending_description)
                pending_description = []

            deeper = _next_meaningful_indent(lines, i)
            if deeper is not None and deeper > indent:
                i = _parse_scope(lines, i, deeper, child, out)

            if node is not None:
                node.children.append(child)
            else:
                out.append(child)
            continue

        target = node
        if target is None:
            # A stray property at document level belongs to the object
            # most recently opened here.
            target = out[-1] if out else None

        # `key: value` — scalar property, possibly with an indented block.
        if prop:
            key = prop.group("key")
            text = prop.group("value")
            if text.strip() == "":
                text, i = _consume_block(lines, i + 1, indent)
                parsed = text
            else:
                parsed = _coerce(key, text)
                i += 1
            if target is not None:
                target.properties[key] = parsed
            continue

        # `key =` or `key = value` — an expression-valued property such
        # as a partition's `source` or a calculation item's `expression`.
        # Distinguished from a child object by having no name token.
        if decl and name is None and value is not None:
            if value.strip() == "":
                text, i = _consume_block(lines, i + 1, indent)
            else:
                text = value.strip()
                i += 1
            if target is not None:
                target.properties[kind] = text
            continue

        # A bare word on its own line is a boolean property (`isHidden`).
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", stripped):
            if target is not None:
                target.properties[stripped] = True
            i += 1
            continue

        i += 1

    return i


def _consume_block(lines: list[str], start: int, parent_indent: int) -> tuple[str, int]:
    """Collect an indented multi-line value, dedented consistently.

    Used for measure expressions, M partition sources and RLS filters —
    the places where text matters verbatim and must not be touched by
    comment stripping or property parsing.

    The block ends when indentation drops **below the block's own first
    line**, not merely back to the parent. That distinction matters:
    TMDL puts an object's remaining properties at a shallower indent
    than its expression body, so stopping at the parent would swallow
    `formatString` into the DAX.
    """
    body: list[str] = []
    i = start
    block_indent = None

    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            body.append("")
            i += 1
            continue
        indent = _indent_width(raw)
        if indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = indent
        elif indent < block_indent:
            break
        # Preserve relative indentation inside the expression.
        extra = max(indent - (block_indent or indent), 0)
        body.append(" " * extra + raw.strip())
        i += 1

    while body and not body[-1].strip():
        body.pop()

    return "\n".join(body), i


def parse_tmdl(text: str) -> list[_Node]:
    """Parse one TMDL document into its top-level nodes."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    nodes: list[_Node] = []
    _parse_scope(lines, 0, 0, None, nodes)
    return nodes


# ---------------------------------------------------------------------
# TMDL nodes -> TMSL dictionaries
# ---------------------------------------------------------------------

def _split_column_ref(ref: str) -> tuple[str | None, str | None]:
    """`Sales.Amount` or `'Fact Sales'.Amount` -> (table, column)."""
    ref = (ref or "").strip()
    if not ref:
        return None, None
    if ref.startswith("'"):
        end = ref.find("'", 1)
        while end != -1 and end + 1 < len(ref) and ref[end + 1] == "'":
            end = ref.find("'", end + 2)
        if end == -1:
            return None, None
        table = ref[1:end].replace("''", "'")
        rest = ref[end + 1:].lstrip(".")
        return table, rest.strip() or None
    if "." not in ref:
        return None, ref
    table, column = ref.split(".", 1)
    return table.strip(), column.strip()


def _node_to_column(node: _Node) -> dict:
    props = node.properties
    column = {
        "name": node.name,
        "dataType": props.get("dataType"),
        "isHidden": bool(props.get("isHidden", False)),
        "formatString": props.get("formatString"),
        "summarizeBy": props.get("summarizeBy"),
        "sortByColumn": props.get("sortByColumn"),
        "description": node.description,
    }
    # A column with an expression is a calculated column.
    if node.value:
        column["type"] = "calculated"
        column["expression"] = node.value
    else:
        column["type"] = props.get("type", "data")
    return column


def _node_to_measure(node: _Node) -> dict:
    props = node.properties
    return {
        "name": node.name,
        "expression": node.value or props.get("expression", ""),
        "formatString": props.get("formatString"),
        "displayFolder": props.get("displayFolder"),
        "description": node.description or props.get("description"),
        "isHidden": bool(props.get("isHidden", False)),
    }


def _node_to_partition(node: _Node) -> dict:
    props = node.properties
    # `partition Sales = m` — the inline value is the source *type*,
    # while the actual M sits in the `source` property.
    source_type = (node.value or props.get("sourceType") or "").strip() or None
    expression = props.get("source") or ""
    return {
        "name": node.name,
        "mode": props.get("mode"),
        "source": {
            "type": source_type,
            "expression": expression,
        },
    }


def _node_to_hierarchy(node: _Node) -> dict:
    levels = []
    for idx, lv in enumerate(node.kids("level")):
        levels.append({
            "name": lv.name,
            "column": lv.properties.get("column"),
            "ordinal": lv.properties.get("ordinal", idx),
        })
    return {"name": node.name, "levels": levels}


def _node_to_table(node: _Node) -> dict:
    table = {
        "name": node.name,
        "isHidden": bool(node.properties.get("isHidden", False)),
        "description": node.description,
        "columns": [_node_to_column(c) for c in node.kids("column")],
        "measures": [_node_to_measure(m) for m in node.kids("measure")],
        "partitions": [_node_to_partition(p) for p in node.kids("partition")],
        "hierarchies": [_node_to_hierarchy(h) for h in node.kids("hierarchy")],
    }

    calc_groups = node.kids("calculationGroup")
    if calc_groups:
        cg = calc_groups[0]
        table["calculationGroup"] = {
            "precedence": cg.properties.get("precedence"),
            "calculationItems": [
                {
                    "name": item.name,
                    "expression": item.value or "",
                }
                for item in cg.kids("calculationItem")
            ],
        }
    return table


def _node_to_relationship(node: _Node) -> dict:
    props = node.properties
    from_table, from_column = _split_column_ref(props.get("fromColumn", ""))
    to_table, to_column = _split_column_ref(props.get("toColumn", ""))
    return {
        "name": node.name,
        "fromTable": from_table,
        "fromColumn": from_column,
        "toTable": to_table,
        "toColumn": to_column,
        "fromCardinality": props.get("fromCardinality", "many"),
        "toCardinality": props.get("toCardinality", "one"),
        "crossFilteringBehavior": props.get("crossFilteringBehavior", "singleDirection"),
        "isActive": props.get("isActive", True),
        "securityFilteringBehavior": props.get("securityFilteringBehavior"),
    }


def _node_to_role(node: _Node) -> dict:
    permissions = []
    for tp in node.kids("tablePermission"):
        permissions.append({
            "name": tp.name,
            "filterExpression": tp.value or tp.properties.get("filterExpression", ""),
        })
    return {
        "name": node.name,
        "modelPermission": node.properties.get("modelPermission"),
        "tablePermissions": permissions,
    }


def build_tmsl(documents: dict[str, str]) -> dict:
    """Assemble a TMSL `{"model": {...}}` from a set of TMDL documents.

    `documents` maps a relative path (e.g. `tables/Sales.tmdl`) to its
    text. Every document is parsed and its objects merged, because a
    .pbip splits one logical model across many files.
    """
    tables: list[dict] = []
    relationships: list[dict] = []
    roles: list[dict] = []
    expressions: list[dict] = []
    model_props: dict = {}

    for path, text in sorted(documents.items()):
        try:
            nodes = parse_tmdl(text)
        except Exception as e:  # noqa: BLE001 - one bad file must not sink the model
            raise TmdlError(f"Failed to parse '{path}': {e}") from e

        for node in nodes:
            if node.kind == "table":
                tables.append(_node_to_table(node))
            elif node.kind == "relationship":
                relationships.append(_node_to_relationship(node))
            elif node.kind == "role":
                roles.append(_node_to_role(node))
            elif node.kind == "expression":
                # A shared M expression (parameter or staged query).
                expressions.append({
                    "name": node.name,
                    "kind": node.properties.get("kind", "m"),
                    "expression": node.value or "",
                })
            elif node.kind == "model":
                model_props.update(node.properties)
                # model.tmdl may carry tables/relationships inline.
                for child in node.children:
                    if child.kind == "table":
                        tables.append(_node_to_table(child))
                    elif child.kind == "relationship":
                        relationships.append(_node_to_relationship(child))
                    elif child.kind == "role":
                        roles.append(_node_to_role(child))

    model: dict = {
        "name": model_props.get("name", "Model"),
        "culture": model_props.get("culture"),
        "tables": tables,
        "relationships": relationships,
        "roles": roles,
    }
    if expressions:
        model["expressions"] = expressions

    return {"model": model}