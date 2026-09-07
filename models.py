"""Input and output contracts. Do not change field names or semantics.

Type-check final_answer against format_hint with `check_format`; the
validator responsibility in your graph should call it.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class QuestionRecord(BaseModel):
    id: str
    question: str
    format_hint: str


class ReviewPacket(BaseModel):
    question: str
    understood: str = Field(description="What the agent understood the question to ask")
    blocker: str = Field(description="The specific reason it cannot answer responsibly")
    considered: list[str] = Field(default_factory=list, description="Candidate SQL or chunk IDs considered")
    decision_needed: str = Field(description="What a human would need to decide")


class OutputRecord(BaseModel):
    id: str
    status: Literal["answered", "needs_review"]
    final_answer: Any = None
    sql: str = ""
    confidence: float | None = None
    explanation: str = ""
    assumptions: list[str] = Field(default_factory=list)
    repairs: int = 0
    citations: list[str] = Field(default_factory=list)
    review_packet: ReviewPacket | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "OutputRecord":
        if self.status == "answered":
            if self.final_answer is None:
                raise ValueError("answered requires final_answer")
            if self.confidence is None or not 0.0 <= self.confidence <= 1.0:
                raise ValueError("answered requires confidence in [0, 1]")
            if self.review_packet is not None:
                raise ValueError("answered must not carry a review_packet")
        else:
            if self.final_answer is not None:
                raise ValueError("needs_review requires final_answer null")
            if self.confidence is not None:
                raise ValueError("needs_review requires confidence null")
            if self.review_packet is None:
                raise ValueError("needs_review requires review_packet")
        if not (0 <= self.repairs <= 2):
            raise ValueError("repairs must be 0, 1 or 2")
        return self


class TraceEvent(BaseModel):
    ts: str = Field(description="ISO 8601 timestamp")
    question_id: str
    responsibility: str = Field(description="routing | retrieval | planning | nl2sql | execution | synthesis | validation | repair | review_gate")
    attempt: int = 0
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    elapsed_ms: int = 0


_SCALARS = {"int": int, "float": float, "str": str}
_OBJ = re.compile(r"^\{(.+)\}$")
_LIST = re.compile(r"^list\[\{(.+)\}\]$")


def _fields(spec: str) -> dict[str, type]:
    out: dict[str, type] = {}
    for part in spec.split(","):
        name, typ = (p.strip() for p in part.split(":"))
        if typ not in _SCALARS:
            raise ValueError(f"unsupported field type {typ!r}")
        out[name] = _SCALARS[typ]
    return out


def _is(value: Any, typ: type) -> bool:
    if typ is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, typ)


def check_format(value: Any, format_hint: str) -> bool:
    """True if value matches format_hint exactly. Supports the published vocabulary only."""
    hint = format_hint.strip()
    if hint in _SCALARS:
        return _is(value, _SCALARS[hint])
    if m := _LIST.match(hint):
        fields = _fields(m.group(1))
        return isinstance(value, list) and all(
            isinstance(v, dict) and set(v) == set(fields) and all(_is(v[k], t) for k, t in fields.items())
            for v in value
        )
    if m := _OBJ.match(hint):
        fields = _fields(m.group(1))
        return isinstance(value, dict) and set(value) == set(fields) and all(_is(value[k], t) for k, t in fields.items())
    raise ValueError(f"unknown format_hint {format_hint!r}")
