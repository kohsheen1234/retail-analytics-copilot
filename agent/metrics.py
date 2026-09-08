"""Execution-grounded metric for the NL-to-SQL module.

`sql_metric(example, pred, trace=None)` runs the predicted SQL through the same read-only
execution boundary the agent uses, runs the gold SQL, and compares the two result sets.
No part of it looks at SQL text similarity: two spellings of the same query must score
identically, and a query that merely *looks* like the gold must score 0 if it returns
different rows.

--------------------------------------------------------------------------------
Return type, and why it is a bool in both modes
--------------------------------------------------------------------------------
DSPy 3.3.1 calls a metric in two modes and consumes the result differently.

*Scoring* (`Evaluate`, `trace is None`): the return value is aggregated numerically.
`bool` is a subclass of `int`, so True/False average to a proportion exactly as 1.0/0.0
would.

*Bootstrapping* (`trace is not None`): `dspy/teleprompt/bootstrap.py` does

    metric_val = self.metric(example, prediction, trace)
    if self.metric_threshold:
        success = metric_val >= self.metric_threshold
    else:
        success = metric_val          # <- used for its TRUTHINESS

so with no `metric_threshold` the value is tested for truthiness. **Any** non-zero float
is truthy. A metric returning 0.3 for "three of five rows matched" would therefore admit
that trace as a demonstration, and the demo's wrong SQL would then be shown to the model
as a worked example on every subsequent call — the loose-metric failure the assessment
warns about. Returning a bool makes the filter semantics identical to the scoring
semantics and removes the trap entirely. It also means I never depend on
`metric_threshold`, which has its own edge: `if self.metric_threshold:` is falsey for a
threshold of `0.0`, so a caller passing 0.0 silently gets truthiness behaviour back.

The metric is deliberately all-or-nothing. Partial credit is not meaningful here: a
revenue figure that is 3% low because a date boundary dropped rows is simply wrong, and
it is exactly the kind of near-miss that partial credit would reward.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from agent import config
from agent.sql_analysis import has_top_level_order_by
from sqlite_tool import SQLiteTool

NUMERIC_TOLERANCE = 0.01
_WS = re.compile(r"\s+")


# --- result comparison -------------------------------------------------------

def normalize_cell(value: Any) -> Any:
    """NULL stays distinguishable; strings get whitespace-normalised; numbers stay numeric."""
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, bytes):
        return value
    return _WS.sub(" ", str(value)).strip()


def cells_equal(a: Any, b: Any) -> bool:
    """One cell. NULL is compared explicitly, never coerced to 0 or ''."""
    a, b = normalize_cell(a), normalize_cell(b)
    if a is None or b is None:
        return a is None and b is None          # NULL equals only NULL
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= NUMERIC_TOLERANCE
    if isinstance(a, float) != isinstance(b, float):
        # One side numeric, the other text. Compare numerically when the text parses,
        # so '14' from a CAST and 14 from a COUNT are not spuriously unequal.
        try:
            return abs(float(a) - float(b)) <= NUMERIC_TOLERANCE
        except (TypeError, ValueError):
            return False
    return a == b


def rows_equal(got: list[tuple], want: list[tuple], ordered: bool) -> bool:
    """Multiset comparison unless `ordered`, in which case position matters.

    Duplicates count in both modes: the unordered comparison is a multiset match, not a
    set match, so `GROUP BY` that collapses rows the gold keeps is a failure.
    """
    if len(got) != len(want):
        return False
    if ordered:
        return all(
            len(g) == len(w) and all(cells_equal(x, y) for x, y in zip(g, w))
            for g, w in zip(got, want)
        )
    # Greedy matching over a multiset. O(n^2) but n is bounded by the row limit and the
    # dev set is 15 examples; a hash-based bucket would need an exact key, which
    # tolerant float comparison does not admit.
    remaining = list(want)
    for g in got:
        for i, w in enumerate(remaining):
            if len(g) == len(w) and all(cells_equal(x, y) for x, y in zip(g, w)):
                del remaining[i]
                break
        else:
            return False
    return not remaining


# --- execution ---------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    ok: bool
    reason: str
    columns: int = 0
    rows: int = 0


def _tool(db_path: str) -> SQLiteTool:
    return SQLiteTool(db_path, row_limit=config.SQL_ROW_LIMIT, timeout_s=config.SQL_TIMEOUT_S)


@lru_cache(maxsize=512)
def _run_cached(db_path: str, sql: str):
    """Gold SQL is re-executed for every candidate on every seed and configuration.

    Caching it is what keeps the experiment inside the stated 30-minute budget: some gold
    statements aggregate over 609k line items. Keyed on (db_path, sql), so it is
    invalidated by pointing at a different database. Predicted SQL is cached too, since
    the same wrong query recurs across seeds.
    """
    res = _tool(db_path).execute(sql)
    return res.error, res.columns, tuple(res.rows), res.truncated


def execute_for_metric(sql: str, db_path: str | None = None):
    return _run_cached(str(db_path or config.DB_PATH), sql)


def clear_execution_cache() -> None:
    _run_cached.cache_clear()


# --- the metric --------------------------------------------------------------

def resolve_ordered(example: Any) -> bool:
    """Does row order count for this example?

    An explicit `ordered` field always wins. When it is absent, order is inferred from a
    top-level `ORDER BY` in the gold SQL. The provided train/dev files as delivered carry
    no `ordered` field at all, yet seven of their examples are rankings with
    `ORDER BY ... LIMIT n` and an ordered `gold_answer` list. Reading "absent" as
    "unordered" would let a reversed top-3 score as correct, and would let such a demo
    through the bootstrap filter and teach the model to invert rankings. Inference can
    only ever make the metric stricter, which is the safe direction, and it is inert once
    the specified data with explicit flags is used.
    """
    explicit = _field(example, "ordered")
    if explicit is not None:
        return bool(explicit)
    gold = _field(example, "gold_sql") or ""
    return has_top_level_order_by(gold)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def evaluate_sql(example: Any, predicted_sql: str | None,
                 db_path: str | None = None) -> Outcome:
    """The equivalence contract, as an inspectable Outcome. `sql_metric` wraps this."""
    gold_sql = (_field(example, "gold_sql") or "").strip()
    pred_sql = (predicted_sql or "").strip()
    ordered = resolve_ordered(example)

    # Abstention contract, justified in DECISIONS.md: a RAG-only example has empty gold
    # SQL, and the correct behaviour for the NL-to-SQL module is to emit nothing. This
    # keeps the full dev set scoreable by one metric while still being able to fail in
    # both directions -- emitting SQL when none is wanted fails, and staying silent when
    # SQL is wanted fails.
    if not gold_sql:
        if pred_sql:
            return Outcome(False, "abstention expected: gold has no SQL but SQL was produced")
        return Outcome(True, "correctly abstained (RAG-only example)")
    if not pred_sql:
        return Outcome(False, "no SQL produced but gold has SQL")

    p_err, p_cols, p_rows, p_trunc = execute_for_metric(pred_sql, db_path)
    if p_err:
        # Covers execution errors, unsafe SQL and multi-statement input alike: the
        # boundary rejects the latter two before execution and reports them as errors.
        return Outcome(False, f"predicted SQL failed: {p_err}")

    g_err, g_cols, g_rows, g_trunc = execute_for_metric(gold_sql, db_path)
    if g_err:
        # A broken gold statement is a dataset bug, not a model failure. Fail loudly
        # rather than silently scoring the prediction against nothing.
        raise ValueError(f"gold SQL failed for {_field(example, 'id')!r}: {g_err}")

    if len(p_cols) != len(g_cols):
        return Outcome(False, f"column count {len(p_cols)} != gold {len(g_cols)}",
                       len(p_cols), len(p_rows))
    if p_trunc or g_trunc:
        return Outcome(False, "result truncated at the row limit; comparison unsound",
                       len(p_cols), len(p_rows))
    if not rows_equal(list(p_rows), list(g_rows), ordered):
        how = "ordered" if ordered else "multiset"
        return Outcome(False, f"rows differ ({how}): {len(p_rows)} vs gold {len(g_rows)}",
                       len(p_cols), len(p_rows))
    return Outcome(True, "match", len(p_cols), len(p_rows))


def sql_metric(example: Any, pred: Any, trace: Any = None) -> bool:
    """Execution-grounded equivalence. Returns bool in both DSPy call modes.

    See the module docstring for why bool rather than a float: during bootstrapping DSPy
    uses the return value's truthiness, so any non-zero float would admit a partially
    wrong demonstration.
    """
    predicted = _field(pred, "sql")
    if predicted is None and isinstance(pred, str):
        predicted = pred
    try:
        return evaluate_sql(example, predicted).ok
    except ValueError:
        raise
    except Exception:
        # A malformed prediction is a failure, not a crash of the optimizer run.
        return False


def sql_metric_verbose(example: Any, pred: Any, trace: Any = None) -> Outcome:
    """Same judgement, with the reason kept. Used for per-example artifacts."""
    predicted = _field(pred, "sql")
    if predicted is None and isinstance(pred, str):
        predicted = pred
    return evaluate_sql(example, predicted)
