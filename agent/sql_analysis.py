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


def physical_tables(sql: str, known_tables: list[str] | set[str]) -> list[str]:
    """Physical tables referenced by `sql`, in the spelling used by the schema.

    Names not present in `known_tables` are dropped: they are CTEs, aliases, or a
    hallucinated table. A hallucinated table is caught separately by execution failing,
    which is the signal we want to keep rather than mask here.
    """
    by_fold = {t.lower(): t for t in known_tables}
    ctes = cte_names(tokenize(sql))
    out: list[str] = []
    for name in referenced_names(sql):
        fold = name.lower()
        if fold in ctes or fold.startswith("sqlite_"):
            continue
        real = by_fold.get(fold)
        if real is not None and real not in out:
            out.append(real)
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
    for name, alias in refs:
        real = by_fold.get(name.lower())
        if real is None:
            continue                    # CTE or unknown; unknown surfaces at execution
        scope[name.lower()] = real
        if alias:
            scope[alias.lower()] = real

    body = _STRINGS.sub("''", _COMMENTS.sub(" ", sql))
    problems: list[str] = []
    for m in _QUALIFIED.finditer(body):
        qual = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        col = (m.group(4) or m.group(5) or m.group(6) or "")
        if not qual or not col or qual in ctes:
            continue
        table = scope.get(qual)
        if table is None:
            if qual in derived:
                continue            # derived-table alias; columns not knowable statically
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
