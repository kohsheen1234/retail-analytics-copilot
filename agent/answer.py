"""Typed answer construction and the confidence rubric.

Both are deterministic code, not model output. `final_answer` must match `format_hint`
exactly and is compared across two fresh runs, so it is built from executed rows by
positional coercion rather than asked for in natural language.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from models import check_format

_FIELDS = re.compile(r"^\{(.+)\}$")
_LIST = re.compile(r"^list\[\{(.+)\}\]$")
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


class AnswerError(ValueError):
    """The rows cannot be shaped into the requested format."""


def _spec(inner: str) -> list[tuple[str, str]]:
    out = []
    for part in inner.split(","):
        name, _, typ = part.partition(":")
        out.append((name.strip(), typ.strip()))
    return out


def coerce_scalar(value: Any, typ: str) -> Any:
    if value is None:
        raise AnswerError(f"NULL cannot satisfy {typ}")
    if typ == "int":
        if isinstance(value, bool):
            raise AnswerError("bool is not int")
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if abs(value - round(value)) > 1e-9:
                raise AnswerError(f"{value} is not integral")
            return int(round(value))
        m = _NUMBER.search(str(value))
        if not m:
            raise AnswerError(f"cannot read an int from {value!r}")
        return int(float(m.group().replace(",", ".")))
    if typ == "float":
        if isinstance(value, bool):
            raise AnswerError("bool is not float")
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        m = _NUMBER.search(str(value))
        if not m:
            raise AnswerError(f"cannot read a float from {value!r}")
        return round(float(m.group().replace(",", ".")), 2)
    if typ == "str":
        return str(value).strip()
    raise AnswerError(f"unsupported type {typ!r}")


def build_answer(format_hint: str, columns: list[str], rows: list[tuple]) -> Any:
    """Shape executed rows into `format_hint`. Raises AnswerError if they cannot be."""
    hint = format_hint.strip()

    if hint in {"int", "float", "str"}:
        if not rows or not rows[0]:
            raise AnswerError("no rows to build a scalar from")
        return coerce_scalar(rows[0][0], hint)

    if m := _LIST.match(hint):
        spec = _spec(m.group(1))
        if not rows:
            raise AnswerError("no rows to build a list from")
        out = []
        for row in rows:
            if len(row) < len(spec):
                raise AnswerError(f"row has {len(row)} columns, need {len(spec)}")
            out.append({name: coerce_scalar(row[i], typ) for i, (name, typ) in enumerate(spec)})
        return out

    if m := _FIELDS.match(hint):
        spec = _spec(m.group(1))
        if not rows or len(rows[0]) < len(spec):
            raise AnswerError("first row does not have enough columns")
        return {name: coerce_scalar(rows[0][i], typ) for i, (name, typ) in enumerate(spec)}

    raise AnswerError(f"unknown format_hint {format_hint!r}")


def build_answer_from_text(format_hint: str, text: str) -> Any:
    """RAG-only path: shape a value extracted from documents."""
    hint = format_hint.strip()
    if hint in {"int", "float", "str"}:
        return coerce_scalar(text, hint)
    raise AnswerError(f"document extraction cannot produce {format_hint!r}")


# --- confidence --------------------------------------------------------------

@dataclass
class Confidence:
    """A deterministic rubric, not a model self-report.

    The escalation scoring penalises a wrong answer above 0.7 extra, so the rubric's job
    is to keep confidence under that line whenever a load-bearing assumption was invented
    rather than sourced, and to move with the signals that actually predict error here:
    repairs used, whether a document conflict had to be resolved, and whether the answer
    rests on an approximation the corpus never documented.
    """

    base: float = 0.90
    notes: list[str] = field(default_factory=list)
    _capped: bool = False

    def penalise(self, amount: float, why: str) -> None:
        self.base -= amount
        self.notes.append(f"-{amount:.2f} {why}")

    def cap(self, ceiling: float, why: str) -> None:
        if self.base > ceiling:
            self.base = ceiling
            self._capped = True
            self.notes.append(f"capped at {ceiling:.2f}: {why}")

    @property
    def value(self) -> float:
        return round(max(0.05, min(0.99, self.base)), 2)


def score_confidence(*, route: str, repairs: int, rows: int, conflicts_resolved: int,
                     invented_approximation: bool, doc_precedence_applied: bool,
                     legacy_window_ambiguity: bool, used_fallback_route: bool,
                     baseline_artifact: bool) -> Confidence:
    c = Confidence()
    if route == "rag":
        # No executable check on a document lookup: nothing verifies the extraction.
        c.penalise(0.10, "answer read from documents with no executable cross-check")
    if repairs:
        c.penalise(0.12 * repairs, f"{repairs} repair(s) needed")
    if rows == 0 and route != "rag":
        c.penalise(0.30, "no rows returned")
    if conflicts_resolved:
        c.penalise(0.05 * conflicts_resolved, "document conflict resolved by rule")
    if doc_precedence_applied:
        c.penalise(0.05, "one document given precedence over another")
    if legacy_window_ambiguity:
        c.penalise(0.10, "KPI effective-date boundary is ambiguous for this window")
    if used_fallback_route:
        c.penalise(0.05, "router fell back to the deterministic prior")
    if baseline_artifact:
        c.penalise(0.05, "running the uncompiled baseline module")
    if invented_approximation:
        c.cap(0.60, "answer rests on an approximation the corpus does not document")
    return c


def validate_shape(answer: Any, format_hint: str) -> bool:
    try:
        return check_format(answer, format_hint)
    except ValueError:
        return False
