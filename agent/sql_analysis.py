"""Static analysis of a SQL statement, for citations and for lint checks.

Why this exists rather than `sqlite_tool.tables_used`: validation requires the table
citations to *exactly* cover the physical tables of the executed SQL — an extra or a
missing table both fail. The starter helper substring-matches known table names anywhere
in the query text, so it cannot tell a physical table from a same-named CTE, and it
matches a table name used as a column alias. Its own docstring says "Verify, do not
trust blindly", so this module does the parsing properly:

  * literals and comments are stripped before anything is inspected;
  * `FROM` / `JOIN` targets are resolved at any nesting depth, including comma joins;
  * quoting styles are normalised (`"Order Details"`, `[Order Details]`, backticks);
  * table aliases are discarded;
  * CTE names defined by the same statement are subtracted;
  * `sqlite_*` internal tables are excluded.

Everything here is pure text analysis: no database connection, no LM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Keywords that terminate a table reference in a FROM list.
_REF_STOP = {
    "where", "group", "order", "having", "limit", "offset", "union", "intersect",
    "except", "join", "inner", "left", "right", "full", "cross", "natural", "on",
    "using", "window", "returning",
}
# Words that may follow a table name but are not an alias.
_NOT_ALIAS = _REF_STOP | {"as", "select", "with", "recursive", "and", "or", "not"}

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STRINGS = re.compile(r"'(?:[^']|'')*'")

_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<dq>"(?:[^"]|"")*")
    | (?P<bq>`(?:[^`]|``)*`)
    | (?P<br>\[[^\]]*\])
    | (?P<word>[A-Za-z_][A-Za-z_0-9$]*)
    | (?P<num>[0-9][0-9.eE+\-]*)
    | (?P<punc>.)
    """,
    re.X,
)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str

    @property
    def word(self) -> str:
        """Lowercased bare word, or '' for anything that is not a bare word."""
        return self.text.lower() if self.kind == "word" else ""


def _unquote(text: str) -> str:
    if len(text) >= 2:
        if text[0] == '"' and text[-1] == '"':
            return text[1:-1].replace('""', '"')
        if text[0] == "`" and text[-1] == "`":
            return text[1:-1].replace("``", "`")
        if text[0] == "[" and text[-1] == "]":
            return text[1:-1]
    return text


def tokenize(sql: str) -> list[Token]:
    """Tokens with comments, string literals and whitespace removed."""
    scrubbed = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    out: list[Token] = []
    for m in _TOKEN.finditer(scrubbed):
        kind = m.lastgroup or "punc"
        if kind == "ws":
            continue
        out.append(Token("ident" if kind in {"dq", "bq", "br"} else kind, m.group()))
    return out


def _skip_parens(toks: list[Token], i: int) -> int:
    """Given toks[i] == '(', return the index just past the matching ')'."""
    depth = 0
    while i < len(toks):
        t = toks[i].text
        if t == "(":
            depth += 1
        elif t == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


def cte_names(toks: list[Token]) -> set[str]:
    """Names bound by a top-level WITH clause. Case-folded."""
    if not toks:
        return set()
    i = 0
    if toks[i].word != "with":
        return set()
    i += 1
    if i < len(toks) and toks[i].word == "recursive":
        i += 1
    names: set[str] = set()
    while i < len(toks):
        if toks[i].kind not in {"word", "ident"}:
            break
        names.add(_unquote(toks[i].text).lower())
        i += 1
        if i < len(toks) and toks[i].text == "(":       # optional column list
            i = _skip_parens(toks, i)
        if i < len(toks) and toks[i].word == "as":
            i += 1
        if i < len(toks) and toks[i].word in {"materialized", "not"}:
            i += 1
            if i < len(toks) and toks[i].word == "materialized":
                i += 1
        if i < len(toks) and toks[i].text == "(":
            i = _skip_parens(toks, i)
        if i < len(toks) and toks[i].text == ",":
            i += 1
            continue
        break
    return names


def _read_ref(toks: list[Token], i: int, found: list[str]) -> int:
    """Read one table reference at toks[i]; append its name if it is an identifier."""
    if i >= len(toks):
        return i
    if toks[i].text == "(":
        # Descend, do not skip. A derived table or parenthesised join still references
        # real tables inside it: in `FROM (SELECT OrderID FROM Orders) x JOIN Products p`
        # jumping over the parens loses Orders, which is a missing citation and a
        # validation failure. Returning i+1 lets the outer scanner walk the inner
        # FROM/JOIN keywords as normal.
        return i + 1
    if toks[i].kind not in {"word", "ident"}:
        return i + 1
    if toks[i].word in _REF_STOP:               # e.g. FROM ... immediately followed by JOIN
        return i
    name = _unquote(toks[i].text)
    i += 1
    # schema-qualified: main.Orders  ->  keep the last part
    while i + 1 < len(toks) and toks[i].text == "." and toks[i + 1].kind in {"word", "ident"}:
        name = _unquote(toks[i + 1].text)
        i += 2
    if i < len(toks) and toks[i].text == "(":   # table-valued function, not a table
        return _skip_parens(toks, i)
    found.append(name)
    if i < len(toks) and toks[i].word == "as":  # alias
        i += 1
        if i < len(toks) and toks[i].kind in {"word", "ident"}:
            i += 1
    elif i < len(toks) and toks[i].kind in {"word", "ident"} and toks[i].word not in _NOT_ALIAS:
        i += 1
    return i


def referenced_names(sql: str) -> list[str]:
    """Every name appearing in a FROM or JOIN position, CTE names not yet removed."""
    toks = tokenize(sql)
    found: list[str] = []
    i = 0
    while i < len(toks):
        w = toks[i].word
        if w == "from":
            i = _read_ref(toks, i + 1, found)
            while i < len(toks) and toks[i].text == ",":
                i = _read_ref(toks, i + 1, found)
        elif w == "join":
            i = _read_ref(toks, i + 1, found)
        else:
            i += 1
    return found


def physical_tables(sql: str, known_tables: list[str] | set[str],
                    views: dict[str, set[str]] | None = None) -> list[str]:
    """Physical tables referenced by `sql`, in the spelling used by the schema.

    Names not present in `known_tables` are dropped: they are CTEs, aliases, or a
    hallucinated table. A hallucinated table is caught separately by execution failing,
    which is the signal we want to keep rather than mask here.

    `views` maps a view name to the physical tables it reads. The delivered database
    ships 18 views, two of them lowercase shims (`order_items`, `ProductDetails_V`) that
    look candidate-authored but are original to the fixture. A query through a view is
    valid SQL that reads real tables, and the citation contract asks for the physical
    tables, so a view is cited as what it reads rather than as itself or as nothing.
    """
    by_fold = {t.lower(): t for t in known_tables}
    view_fold = {v.lower(): bases for v, bases in (views or {}).items()}
    ctes = cte_names(tokenize(sql))
    out: list[str] = []
    for name in referenced_names(sql):
        fold = name.lower()
        if fold in ctes or fold.startswith("sqlite_"):
            continue
        real = by_fold.get(fold)
        if real is not None:
            if real not in out:
                out.append(real)
            continue
        for base in sorted(view_fold.get(fold, ())):
            if base not in out:
                out.append(base)
    return sorted(out)


def has_top_level_order_by(sql: str) -> bool:
    """True if the statement has an ORDER BY outside any parentheses.

    Used to infer the `ordered` flag for gold examples that omit it (see DECISIONS.md).
    """
    toks = tokenize(sql)
    depth = 0
    for i, t in enumerate(toks):
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and t.word == "order" and i + 1 < len(toks) and toks[i + 1].word == "by":
            return True
    return False


# --- Date handling lint -------------------------------------------------------
# `OrderDate` holds mixed 'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS' values, so a bare
# lexicographic comparison silently drops timestamped rows on the closing day of a
# window. See DECISIONS.md and tests/test_sql_dates.py.

_NORMALISERS = ("date", "datetime", "julianday", "strftime", "substr", "cast", "unixepoch")
_WRAPPED = re.compile(
    r"(?:" + "|".join(_NORMALISERS) + r")\s*\([^()]*$", re.I)
_COMPARISON = re.compile(r"^\s*(between\b|>=|<=|<>|!=|=|<|>)", re.I)
_DATE_COL = re.compile(r'(?:(?:[A-Za-z_]\w*|"[^"]+"|\[[^\]]+\])\s*\.\s*)?'
                       r'(?:([A-Za-z_]\w*[Dd]ate)|"([^"]*[Dd]ate)"|\[([^\]]*[Dd]ate)\])')


def unwrapped_date_comparisons(sql: str) -> list[str]:
    """Date-ish columns compared to something without a normalising function.

    Heuristic and deliberately advisory: it drives a repair attempt and a trace note, it
    never rewrites the model's SQL. False negatives are possible (a normaliser applied to
    the literal side rather than the column side); false positives are possible on a
    column merely *named* like a date but stored as a number.
    """
    body = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    hits: list[str] = []
    for m in _DATE_COL.finditer(body):
        col = m.group(1) or m.group(2) or m.group(3)
        if _WRAPPED.search(body[: m.start()]):
            continue
        if not _COMPARISON.match(body[m.end():]):
            continue
        if col not in hits:
            hits.append(col)
    return hits


# --- static schema checking ---------------------------------------------------
# Catching an invented column before execution turns a wasted 16-20s generation plus a
# consumed repair slot into a precise, actionable message. The smoke run produced
# `Customers.CustomerID` (Customers never joined), `Discontins`, and `Orders.ProductID`
# in three consecutive attempts, none of which the generic executor error helped fix.

def _refs_with_aliases(sql: str) -> tuple[list[tuple[str, str | None]], set[str]]:
    """((table_name, alias) per FROM/JOIN target, aliases of derived tables)."""
    toks = tokenize(sql)
    out: list[tuple[str, str | None]] = []
    derived: set[str] = set()
    i = 0
    while i < len(toks):
        w = toks[i].word
        if w in ("from", "join"):
            i, _ = _read_ref_alias(toks, i + 1, out, derived)
            if w == "from":
                while i < len(toks) and toks[i].text == ",":
                    i, _ = _read_ref_alias(toks, i + 1, out, derived)
        else:
            i += 1
    return out, derived


def _read_ref_alias(toks: list[Token], i: int, found: list[tuple[str, str | None]],
                    derived: set[str] | None = None) -> tuple[int, bool]:
    if i >= len(toks):
        return i, False
    if toks[i].text == "(":
        # Derived table. Its columns are unknowable statically, but its alias is a
        # legitimately defined name and must not be reported as undefined.
        j = _skip_parens(toks, i)
        if derived is not None and j < len(toks):
            k = j + 1 if toks[j].word == "as" else j
            if k < len(toks) and toks[k].kind in {"word", "ident"} and toks[k].word not in _NOT_ALIAS:
                derived.add(_unquote(toks[k].text).lower())
        return i + 1, False
    if toks[i].kind not in {"word", "ident"} or toks[i].word in _REF_STOP:
        return i, False
    name = _unquote(toks[i].text)
    i += 1
    while i + 1 < len(toks) and toks[i].text == "." and toks[i + 1].kind in {"word", "ident"}:
        name = _unquote(toks[i + 1].text)
        i += 2
    if i < len(toks) and toks[i].text == "(":
        return _skip_parens(toks, i), False
    alias = None
    if i < len(toks) and toks[i].word == "as":
        i += 1
        if i < len(toks) and toks[i].kind in {"word", "ident"}:
            alias = _unquote(toks[i].text)
            i += 1
    elif i < len(toks) and toks[i].kind in {"word", "ident"} and toks[i].word not in _NOT_ALIAS:
        alias = _unquote(toks[i].text)
        i += 1
    found.append((name, alias))
    return i, True


_QUALIFIED = re.compile(
    r'(?<![\w."])(?:([A-Za-z_]\w*)|"([^"]+)"|\[([^\]]+)\])\s*\.\s*'
    r'(?:([A-Za-z_]\w*)|"([^"]+)"|\[([^\]]+)\])')


def schema_errors(sql: str, schema: dict[str, list[dict]]) -> list[str]:
    """Qualified column references that the live schema cannot satisfy.

    Only *qualified* references (`alias.column` / `Table.column`) are checked. Bare
    column names are left alone: resolving them correctly needs full scope analysis, and
    a false positive here would trigger a pointless repair on valid SQL. Qualified
    references are unambiguous and are where the model's mistakes actually land.
    """
    by_fold = {t.lower(): t for t in schema}
    cols_by_table = {t: {c["name"].lower() for c in cols} for t, cols in schema.items()}

    refs, derived = _refs_with_aliases(sql)
    ctes = cte_names(tokenize(sql))
    scope: dict[str, str] = {}         # alias-or-name (folded) -> real table
    unknown: dict[str, str] = {}       # alias-or-name (folded) -> the invented table name
    for name, alias in refs:
        real = by_fold.get(name.lower())
        if real is None:
            if name.lower() not in ctes:
                # An invented table. Previously this fell through and every column on
                # its alias was reported as "alias never defined; this query has: o, x"
                # - a message that names the alias it claims is missing. Say what is
                # actually wrong instead, so the repair prompt is not self-contradictory.
                unknown[name.lower()] = name
                if alias:
                    unknown[alias.lower()] = name
            continue
        scope[name.lower()] = real
        if alias:
            scope[alias.lower()] = real

    body = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    problems: list[str] = []
    for bad in dict.fromkeys(unknown.values()):
        known = ", ".join(sorted(schema))
        problems.append(f"table '{bad}' does not exist; the database has: {known}")
    for m in _QUALIFIED.finditer(body):
        qual = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        col = (m.group(4) or m.group(5) or m.group(6) or "")
        if not qual or not col or qual in ctes:
            continue
        table = scope.get(qual)
        if table is None:
            if qual in derived or qual in unknown:
                continue            # derived-table alias, or already reported as an
                                    # invented table; its columns are not the problem
            if qual in by_fold:
                problems.append(
                    f"'{by_fold[qual]}.{col}' refers to table {by_fold[qual]}, which is not "
                    f"in any FROM or JOIN clause of this query")
            else:
                joined = ", ".join(sorted({a or t for t, a in refs})) or "(none)"
                problems.append(
                    f"alias '{qual}' in '{qual}.{col}' is never defined; this query only "
                    f"has: {joined}. Add the missing JOIN or use a defined alias.")
            continue
        if col.lower() not in cols_by_table[table]:
            available = ", ".join(sorted(c["name"] for c in schema[table]))
            problems.append(
                f"column '{col}' does not exist on {table}; {table} has: {available}")
    seen: list[str] = []
    for p in problems:
        if p not in seen:
            seen.append(p)
    return seen


# --- date boundary: the enforced subset ---------------------------------------
# `unwrapped_date_comparisons` above is advisory: it is right that a half-open bare
# comparison (`>= '2017-01-01' AND < '2018-01-01'`) is *correct* on mixed formats, because
# 'YYYY-MM-DD HH:MM:SS' sorts after 'YYYY-MM-DD' lexicographically, so flagging it would
# spend a repair on valid SQL. What is never correct is an *inclusive* upper bound against a
# bare date column: `BETWEEN a AND b`, `<= b`, `= b` all drop every timestamped row on day
# b. Measured on this database: 3 of 134 orders in June 2017, a 3.2% silent undercount on
# the summer-revenue question. That subset is a repair trigger; the rest stays advisory.

_INCLUSIVE = re.compile(r"^\s*(between\b|<=|=)", re.I)


def date_boundary_errors(sql: str) -> list[str]:
    """Bare date columns under an inclusive comparison. Each hit is a repair trigger."""
    body = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    hits: list[str] = []
    for m in _DATE_COL.finditer(body):
        col = m.group(1) or m.group(2) or m.group(3)
        if _WRAPPED.search(body[: m.start()]):
            continue
        cmp = _INCLUSIVE.match(body[m.end():])
        if not cmp:
            continue
        op = cmp.group(1).upper()
        msg = (f"{col} is compared with {op} without date(): OrderDate mixes 'YYYY-MM-DD' "
               f"and 'YYYY-MM-DD HH:MM:SS', so an inclusive bound on the bare column drops "
               f"every timestamped row on the closing day. Write date({col}) {op} ...")
        if msg not in hits:
            hits.append(msg)
    return hits


# --- aggregation grain --------------------------------------------------------
# Found the hard way. The planner puts "group by the entity's id column and select its
# display name" into every prompt, and on `dev_top3_customers_revenue_2019` the model
# wrote `GROUP BY c.CompanyName` anyway. Two test-account rows in Customers share
# CompanyName='IT', so grouping by the label merged them into a $859,581 "customer" that
# ranked first - a wrong answer shipped at confidence 0.9. The shipped eval answer
# `hybrid_best_customer_margin_2017` has the same GROUP BY and is right only because the
# merged bucket lands third. Delivery of a constraint is observable in the trace; compliance
# needs a check on the SQL. This is that check, schema-driven: a "label" is a text column
# whose name ends in Name on a table with a single-column primary key.

_GROUP_STOP = {"order", "having", "limit", "offset", "union", "intersect", "except",
               "window", ")"}
_SQL_WORDS = {
    "select", "from", "where", "group", "by", "order", "having", "limit", "offset", "as",
    "on", "and", "or", "not", "in", "is", "null", "between", "like", "case", "when",
    "then", "else", "end", "join", "inner", "left", "right", "outer", "cross", "natural",
    "using", "distinct", "all", "union", "intersect", "except", "asc", "desc", "with",
    "recursive", "exists", "cast", "over", "partition", "rows", "range", "filter",
    "true", "false", "collate", "escape", "glob", "regexp", "match", "nulls", "first",
    "last", "current_date", "current_time", "current_timestamp",
}


def _scope(sql: str, schema: dict[str, list[dict]]) -> dict[str, str]:
    """alias-or-table (folded) -> real table name, for the FROM/JOIN targets in `sql`."""
    by_fold = {t.lower(): t for t in schema}
    refs, _ = _refs_with_aliases(sql)
    scope: dict[str, str] = {}
    for name, alias in refs:
        real = by_fold.get(name.lower())
        if real is None:
            continue
        scope[name.lower()] = real
        if alias:
            scope[alias.lower()] = real
    return scope


def _alias_for(scope: dict[str, str], table: str) -> str:
    """The alias the query wrote for `table`, else the table name quoted if needed."""
    for key, real in scope.items():
        if real == table and key != table.lower():
            return key
    return quote_ident(table)


def _group_by_terms(toks: list[Token]) -> list[list[Token]]:
    """Token runs for each depth-0 GROUP BY term, or [] when there is no GROUP BY."""
    depth = 0
    start = None
    for i, tk in enumerate(toks):
        if tk.text == "(":
            depth += 1
        elif tk.text == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and tk.word == "group" and i + 1 < len(toks) and toks[i + 1].word == "by":
            start = i + 2
            break
    if start is None:
        return []
    terms: list[list[Token]] = [[]]
    depth = 0
    for tk in toks[start:]:
        if tk.text == "(":
            depth += 1
        elif tk.text == ")":
            if depth == 0:
                break
            depth -= 1
        if depth == 0 and tk.word in _GROUP_STOP and tk.word != ")":
            break
        if depth == 0 and tk.text == ",":
            terms.append([])
            continue
        terms[-1].append(tk)
    return [t for t in terms if t]


def _resolve_term(term: list[Token], scope: dict[str, str],
                  schema: dict[str, list[dict]]) -> tuple[str, str, str] | None:
    """(alias-as-written, real table, column) for a `qual.col` or bare `col` term."""
    words = [tk for tk in term if tk.kind in {"word", "dq", "bq", "br"}]
    if len(term) == 3 and term[1].text == "." and len(words) == 2:
        qual, col = _unquote(words[0].text), _unquote(words[1].text)
        table = scope.get(qual.lower())
        return (qual, table, col) if table else None
    if len(term) == 1 and len(words) == 1:
        col = _unquote(words[0].text)
        owners = [t for t in dict.fromkeys(scope.values())
                  if any(c["name"].lower() == col.lower() for c in schema[t])]
        if len(owners) == 1:
            return (_alias_for(scope, owners[0]), owners[0], col)
    return None


def _label_terms(sql: str, schema: dict[str, list[dict]],
                 labels: dict[str, dict[str, str]] | None) -> list[tuple[str, str, str, str]]:
    """(alias, table, label_col, pk_col) for each GROUP BY term that groups by a label.

    With `labels` (table -> {label column -> key column}, see `schema.label_collisions`)
    only those columns count: the ones measured to be near-unique with collisions, where
    grouping by the label demonstrably changes the answer. Without it, the schema-only
    heuristic applies: any text column ending in `Name` on a single-key table.
    """
    if not sql.strip():
        return []
    toks = tokenize(_STRINGS.sub("''", _COMMENTS.sub(" ", sql)))
    terms = _group_by_terms(toks)
    if not terms:
        return []
    scope = _scope(sql, schema)
    resolved = [r for r in (_resolve_term(t, scope, schema) for t in terms) if r]
    keyed_tables = {table for _, table, col in resolved
                    if any(c["pk"] and c["name"].lower() == col.lower() for c in schema[table])}
    out: list[tuple[str, str, str, str]] = []
    for alias, table, col in resolved:
        cols = schema[table]
        pks = [c["name"] for c in cols if c["pk"]]
        spec = next((c for c in cols if c["name"].lower() == col.lower()), None)
        if spec is None or len(pks) != 1 or table in keyed_tables or pks[0].lower() == col.lower():
            continue
        if labels is not None:
            hit = next((pk for lc, pk in labels.get(table, {}).items() if lc.lower() == col.lower()), None)
            if hit is None:
                continue
            out.append((alias, table, spec["name"], hit))
            continue
        is_label = col.lower().endswith("name") and (spec["type"] or "").upper() in ("TEXT", "")
        if is_label:
            out.append((alias, table, spec["name"], pks[0]))
    return out


def label_grouping(sql: str, schema: dict[str, list[dict]],
                   labels: dict[str, dict[str, str]] | None = None) -> list[str]:
    """GROUP BY terms that group an entity by its display name rather than its key.

    Silent when the key is present (`GROUP BY c.CustomerID, c.CompanyName` is the
    documented form), on non-entity text columns (`ShipCountry`), and on every provided
    gold statement. See `_label_terms` for what `labels` restricts this to.
    """
    return [f"GROUP BY {alias}.{col} groups {table} by a display name, not by the entity; "
            f"two {table} rows can share a {col}. Group by {alias}.{pk} and select {alias}.{col}."
            for alias, table, col, pk in _label_terms(sql, schema, labels)]


# --- mechanical rewrites --------------------------------------------------------
# Why rewrite instead of asking the model to. Measured on the eval set: routing the grain
# finding through an LM repair produced SQL with two GROUP BY clauses, an invented
# Suppliers join, and an escalation, and - because each extra call perturbs Ollama's state -
# two cold runs then disagreed on 5 of 6 questions, failing the determinism gate that had
# passed with zero repairs. Where the fix is exact and the wrong form is never right, the
# fix is applied in code, recorded in the trace and stated in `assumptions`. This is the
# same class of operation as the keyword repair in `agent/modules.py`: deterministic string
# surgery before the execution boundary sees anything. Anything not mechanically fixable
# still goes to the LM repair.

def _blank_literals(sql: str) -> str:
    """Same-length copy with string literals and comments blanked, so offsets line up."""
    def blank(m: re.Match[str]) -> str:
        s = m.group(0)
        return s[0] + " " * (len(s) - 2) + s[-1] if len(s) >= 2 else " " * len(s)
    out = _COMMENTS.sub(lambda m: " " * len(m.group(0)), sql)
    return _STRINGS.sub(blank, out)


_GROUP_BY = re.compile(r"\bgroup\s+by\b", re.I)
_CLAUSE_END = re.compile(r"\b(order\s+by|having|limit|offset|union|intersect|except|window)\b", re.I)


def _group_by_span(sql: str) -> tuple[int, int] | None:
    """(start, end) offsets of the depth-0 GROUP BY term list in `sql`, or None."""
    blank = _blank_literals(sql)
    depth = 0
    start = None
    for i, ch in enumerate(blank):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and ch.lower() == "g":
            m = _GROUP_BY.match(blank, i)
            if m:
                start = m.end()
                break
    if start is None:
        return None
    depth = 0
    for i in range(start, len(blank)):
        ch = blank[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return start, i
            depth -= 1
        elif depth == 0 and _CLAUSE_END.match(blank, i):
            return start, i
    return start, len(blank)


def rewrite_label_grouping(sql: str, schema: dict[str, list[dict]],
                           labels: dict[str, dict[str, str]] | None) -> tuple[str, list[str]]:
    """Replace each label GROUP BY term with the entity key. Returns (sql, notes)."""
    terms = _label_terms(sql, schema, labels)
    span = _group_by_span(sql) if terms else None
    if not terms or span is None:
        return sql, []
    start, end = span
    clause = sql[start:end]
    notes: list[str] = []
    for alias, table, col, pk in terms:
        qualified = re.compile(rf'(?<![\w."\]])({re.escape(alias)}|"{re.escape(table)}")\s*\.\s*"?{re.escape(col)}"?(?![\w"])', re.I)
        bare = re.compile(rf'(?<![\w."\]])"?{re.escape(col)}"?(?![\w"])', re.I)
        new_clause, n = qualified.subn(lambda m: f"{m.group(1)}.{pk}", clause)
        if n == 0:
            new_clause, n = bare.subn(f"{alias}.{pk}", clause)
        if n:
            clause = new_clause
            notes.append(f"grouped {table} by {pk} instead of {col}: {col} is not unique in "
                         f"{table}, so grouping by it would merge distinct rows")
    return sql[:start] + clause + sql[end:], notes


def rewrite_date_boundaries(sql: str) -> tuple[str, list[str]]:
    """Wrap each bare date column under an inclusive comparison in date(). (sql, notes)."""
    blank = _blank_literals(sql)
    edits: list[tuple[int, int]] = []
    for m in _DATE_COL.finditer(blank):
        if _WRAPPED.search(blank[: m.start()]) or not _INCLUSIVE.match(blank[m.end():]):
            continue
        edits.append((m.start(), m.end()))
    if not edits:
        return sql, []
    out = sql
    notes: list[str] = []
    for s, e in reversed(edits):
        col = sql[s:e]
        out = out[:s] + f"date({col})" + out[e:]
        notes.append(f"compared date({col}) rather than the bare column: OrderDate mixes "
                     f"'YYYY-MM-DD' and 'YYYY-MM-DD HH:MM:SS', and an inclusive bound on the "
                     f"bare text drops every timestamped row on the closing day")
    return out, list(dict.fromkeys(notes))





# --- ambiguous bare columns ---------------------------------------------------
# `dev_added_grain_lines_vs_orders_2020` failed at execution with "ambiguous column name:
# OrderID" - a bare column that exists on both joined tables. SQLite decides this from
# the FROM scope alone, so it is decidable before execution and the message can name both
# owners and the qualified forms to choose between, which the executor's error does not.

def ambiguous_columns(sql: str, schema: dict[str, list[dict]]) -> list[str]:
    """Unqualified column references that more than one in-scope table could satisfy.

    Deliberately conservative: skipped when the statement has a subquery, a CTE, or a
    USING clause, since bare-name scope is no longer the flat FROM list in those cases.
    """
    if not sql.strip():
        return []
    body = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    toks = tokenize(body)
    words = [tk.word for tk in toks]
    if words.count("select") != 1 or "using" in words or "with" in words:
        return []
    scope = _scope(sql, schema)
    tables = list(dict.fromkeys(scope.values()))
    if len(tables) < 2:
        return []
    owners: dict[str, list[str]] = {}
    for tname in tables:
        for c in schema[tname]:
            owners.setdefault(c["name"].lower(), []).append(tname)
    names_in_scope = set(scope)
    problems: list[str] = []
    for i, tk in enumerate(toks):
        if tk.kind not in {"word", "dq", "bq", "br"}:
            continue
        name = _unquote(tk.text)
        fold = name.lower()
        if fold in _SQL_WORDS or fold in names_in_scope:
            continue
        prev = toks[i - 1].text if i > 0 else ""
        nxt = toks[i + 1].text if i + 1 < len(toks) else ""
        if prev == "." or nxt in {".", "("}:
            continue                      # qualified, or a function call
        if prev.lower() == "as" or (i > 0 and toks[i - 1].kind in {"word", "dq", "bq", "br"}
                                    and toks[i - 1].word not in _SQL_WORDS):
            continue                      # an alias being defined
        have = owners.get(fold, [])
        if len(have) < 2:
            continue
        forms = " or ".join(f"{_alias_for(scope, t)}.{name}" for t in have)
        msg = (f"column '{name}' is ambiguous: it exists on "
               f"{' and '.join(quote_ident(t) for t in have)}; write {forms}")
        if msg not in problems:
            problems.append(msg)
    return problems


def quote_ident(name: str) -> str:
    return f'"{name}"' if not name.isidentifier() else name


# --- interpretive choices -----------------------------------------------------
# Found by running an ad-hoc question the eval set does not contain:
# "How many orders were shipped to Germany in 2019?" has four defensible readings -
# ShipCountry or Customers.Country crossed with OrderDate or ShippedDate - giving 175,
# 173, 153 and 154. The agent answered 154 with `assumptions: []` and confidence 0.9.
# The contract is explicit that an empty assumptions list on a question needing an
# interpretation is a defect, and near-certainty on an ambiguous reading is the
# calibration failure the scoring punishes.
#
# The planner catches interpretations that come from the *documents*. This catches the
# ones the model makes inside the SQL, which the planner never sees.

_SEMANTIC_SUFFIXES = ("Date", "Country", "City", "Region", "PostalCode", "Address",
                      "Price", "Phone")


def _column_groups(schema: dict[str, list[dict]]) -> dict[str, list[str]]:
    """Columns sharing a semantic suffix, as `Table.Column`. A query must pick one."""
    groups: dict[str, list[str]] = {}
    for table, cols in schema.items():
        for c in cols:
            for suf in _SEMANTIC_SUFFIXES:
                if c["name"].endswith(suf):
                    groups.setdefault(suf, []).append(f"{table}.{c['name']}")
                    break
    return {k: sorted(v) for k, v in groups.items() if len(v) > 1}


def _question_tokens(question: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", question.lower()) if len(w) > 2}


def _names_column(question_tokens: set[str], column: str) -> bool:
    """Does the question name this column specifically, rather than its family?

    `ShippedDate` is named by "shipped"; `OrderDate` by "order"/"ordered"/"placed".
    Matching on the column's distinguishing prefix, not the shared suffix, is what
    separates "the question chose" from "the model chose".
    """
    bare = column.split(".", 1)[1]
    for suf in _SEMANTIC_SUFFIXES:
        if bare.endswith(suf) and len(bare) > len(suf):
            prefix = bare[: -len(suf)].lower()
            return any(t.startswith(prefix) or prefix.startswith(t) for t in question_tokens)
    return bare.lower() in question_tokens


def interpretive_choices(sql: str, schema: dict[str, list[dict]], question: str,
                         constraints: str = "") -> list[str]:
    """Choices the SQL made between equally available columns that the question left open.

    Reports rather than resolves: the point is to put the interpretation in `assumptions`
    and take it out of `confidence`, not to guess which reading the asker meant.

    A choice the planner already dictated is not the model's interpretation, so anything
    named in `constraints` is suppressed. That is what keeps this quiet on the ordinary
    cases: `DATES: ... date(OrderDate) ...` and `REVENUE uses "Order Details".UnitPrice`
    are always supplied, so picking those columns is compliance, not a judgement call.
    Deviating from them - `ShippedDate` where the constraint said `OrderDate` - still
    fires, which is exactly the case worth surfacing.
    """
    low_constraints = constraints.lower()
    if not sql.strip():
        return []
    refs, _ = _refs_with_aliases(sql)
    by_fold = {t.lower(): t for t in schema}
    scope = {}
    for name, alias in refs:
        real = by_fold.get(name.lower())
        if real:
            scope[name.lower()] = real
            if alias:
                scope[alias.lower()] = real

    body = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    used: set[str] = set()
    for m in _QUALIFIED.finditer(body):
        qual = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        col = m.group(4) or m.group(5) or m.group(6) or ""
        if (table := scope.get(qual)):
            used.add(f"{table}.{col}")
    # unqualified columns: attribute to whichever referenced table owns them uniquely
    owners: dict[str, list[str]] = {}
    for table, cols in schema.items():
        for c in cols:
            owners.setdefault(c["name"].lower(), []).append(table)
    referenced = {t for t in scope.values()}
    for tok in re.findall(r"\b[A-Za-z_]\w*\b", body):
        cands = [t for t in owners.get(tok.lower(), []) if t in referenced]
        if len(cands) == 1:
            real = next(c["name"] for c in schema[cands[0]] if c["name"].lower() == tok.lower())
            used.add(f"{cands[0]}.{real}")

    qtok = _question_tokens(question)
    out: list[str] = []
    for suf, members in _column_groups(schema).items():
        chosen = sorted(used & set(members))
        if len(chosen) != 1:
            continue                      # none used, or the query already spans several
        pick = chosen[0]
        if _names_column(qtok, pick):
            continue                      # the question asked for this one specifically
        bare = pick.split(".", 1)[1].lower()
        if bare in low_constraints:
            continue                      # the planner already dictated this column
        # only offer alternatives on tables the query already touches or could join
        alts = [m for m in members
                if m != pick and m.split(".")[0] in referenced]
        if not alts:
            continue
        out.append(f"Used {pick} where the question did not say which to use; "
                   f"{', '.join(alts)} would also have been defensible.")
    return sorted(out)
