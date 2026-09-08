"""BM25 retrieval over the canonical chunks, plus conflict expansion.

Two responsibilities, kept separate so the trace shows them separately:

1. `bm25(question, k)` — plain BM25 top-k, which is the contract the assessment asks
   for ("TF-IDF or BM25 over chunks, returning top-k chunk IDs with scores").

2. `expand_for_conflicts(...)` — after top-k, pull in any chunk that describes the same
   named entity as a retrieved chunk. Without this, a question about a campaign can
   retrieve `marketing_calendar::chunk1` (2017-06-01..2017-06-30) and never see
   `campaign_memo::chunk0` (revised end date 2017-07-07), and the planner would then
   "resolve" a conflict it cannot see. Conflicts must be surfaced, so retrieval has to
   put both candidates in front of the planner. Expansion is reported separately from
   the top-k so the two are never confused.

Scores are BM25 raw scores. Ordering is deterministic: score descending, then chunk id
ascending, so ties never depend on dict or filesystem order.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

from chunker import Chunk, chunk_corpus

_WORD = re.compile(r"[a-z0-9]+")

# Cheap morphology: the corpus is 13 chunks, so a real stemmer buys nothing, but
# question wording ("categories" vs "category") should not miss.
_SUFFIXES = ("ies", "es", "s")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for tok in _WORD.findall(text.lower()):
        out.append(tok)
        for suf in _SUFFIXES:
            if len(tok) > len(suf) + 2 and tok.endswith(suf):
                out.append(tok[: -len(suf)] + ("y" if suf == "ies" else ""))
                break
    return out


def _stems(text: str) -> set[str]:
    """Word stems, collapsing simple plurals to one form (not both)."""
    out: set[str] = set()
    for tok in _WORD.findall(text.lower()):
        for suf, repl in (("ies", "y"), ("es", ""), ("s", "")):
            if len(tok) > len(suf) + 2 and tok.endswith(suf):
                tok = tok[: -len(suf)] + repl
                break
        out.add(tok)
    return out


def heading_variants(content: str) -> list[str]:
    """Names a chunk's heading claims to define.

    "## Average Order Value (AOV)" -> ["Average Order Value", "AOV"], so a question
    saying either form finds the chunk. A definition chunk's heading is the most reliable
    statement of what it defines, and it is document-shape rather than document-specific,
    so a new document works the same way.
    """
    for line in content.splitlines():
        if line.startswith("#"):
            head = line.lstrip("#").strip()
            inside = re.findall(r"\(([^)]*)\)", head)
            outside = re.sub(r"\([^)]*\)", " ", head).strip()
            return [v for v in ([outside] + inside) if _is_definable(v)]
    return []


def _is_definable(variant: str) -> bool:
    """Reject variants that cannot be the name of a defined term.

    Specifically bare years: `# Northwind Marketing Calendar (2017)` yields the
    parenthesised variant "2017", which matches every question mentioning 2017 and pulled
    a title-only chunk into the AOV and gross-margin prompts. A definition name has to
    contain a word.
    """
    v = variant.strip()
    if len(v) < 3:
        return False
    words = _WORD.findall(v.lower())
    return any(not w.isdigit() for w in words)


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    score: float
    source: str          # "bm25" or "conflict-expansion"
    content: str


# A heading like "## Summer Beverages 2017" names an entity that other documents may
# also describe. Used only to decide what to co-retrieve, never to answer.
_YEARED_NAME = re.compile(r"\b([A-Z][A-Za-z/&'-]*(?:\s+[A-Z][A-Za-z/&'-]*){0,3}\s+((?:19|20)\d{2}))\b")


def entity_names(text: str) -> set[str]:
    """Title-cased multiword names carrying a year, e.g. 'Summer Beverages 2017'."""
    return {m.group(1).strip().lower() for m in _YEARED_NAME.finditer(text)}


class Retriever:
    def __init__(self, docs_dir: Path, chunks: list[Chunk] | None = None):
        self.chunks: list[Chunk] = list(chunks if chunks is not None else chunk_corpus(docs_dir))
        self.by_id = {c.id: c for c in self.chunks}
        self._corpus_tokens = [tokenize(c.content) for c in self.chunks]
        self._bm25 = BM25Okapi(self._corpus_tokens)

    # -- 1. top-k ------------------------------------------------------------
    def bm25(self, question: str, k: int) -> list[Hit]:
        scores = self._bm25.get_scores(tokenize(question))
        ranked = sorted(
            zip(self.chunks, scores),
            key=lambda p: (-float(p[1]), p[0].id),
        )
        return [
            Hit(chunk_id=c.id, score=round(float(s), 6), source="bm25", content=c.content)
            for c, s in ranked[:k]
            if s > 0.0
        ]

    # -- 2. conflict expansion ----------------------------------------------
    def expand_for_conflicts(self, question: str, hits: list[Hit]) -> list[Hit]:
        """Chunks describing a named entity that the *question* asks about.

        Anchored to the question, not to the retrieved chunks. Unioning in entities
        found in `hits` drifts: a gross-margin question for calendar year 2017 retrieves
        `campaign_memo::chunk0` on the word "2017", and the memo names "Summer Beverages
        2017", which would drag the calendar in for a question that never mentioned a
        campaign. Context is scarce at num_ctx=4096, so expansion has to be earned by
        the question.
        """
        wanted: set[str] = entity_names(question)
        if not wanted:
            return []
        have = {h.chunk_id for h in hits}
        extra: list[Hit] = []
        for c in self.chunks:
            if c.id in have:
                continue
            names = entity_names(c.content)
            if names & wanted:
                extra.append(Hit(chunk_id=c.id, score=0.0, source="conflict-expansion",
                                 content=c.content))
        return sorted(extra, key=lambda h: h.chunk_id)

    # -- 3. definition expansion ---------------------------------------------
    def expand_for_definitions(self, question: str, hits: list[Hit]) -> list[Hit]:
        """Chunks that define a term the question uses.

        Added after an end-to-end failure, not from first principles. On
        "Total revenue from the 'Beverages' category during the 'Summer Beverages 2017'
        dates as defined in the marketing calendar", BM25 ranks the three calendar and
        memo chunks above `kpi_definitions::chunk3`, which lands 6th and falls outside
        k=4. The Revenue formula therefore never reached the planner, no formula
        constraint reached the prompt, and the model summed `UnitPrice * Quantity`
        without `(1 - Discount)` -- answering 611679.25 against a gold of 611562.68. A
        near-miss of that shape is the worst kind of wrong: plausible, uncheckable
        without the gold, and caused by a ranking cutoff rather than by the model.

        Raising k would also have fixed this one case, at the cost of spending context on
        whatever ranks 5th and 6th for every other question. Targeting definitions spends
        it only where a definition is actually invoked.
        """
        qs = _stems(question)
        have = {h.chunk_id for h in hits}
        extra: list[Hit] = []
        for c in self.chunks:
            if c.id in have:
                continue
            for variant in heading_variants(c.content):
                vs = _stems(variant)
                if vs and vs <= qs:
                    extra.append(Hit(chunk_id=c.id, score=0.0,
                                     source="definition-expansion", content=c.content))
                    break
        return sorted(extra, key=lambda h: h.chunk_id)

    def retrieve(self, question: str, k: int) -> tuple[list[Hit], list[Hit]]:
        """(top_k, expansion). Callers pass top_k + expansion to planning."""
        top = self.bm25(question, k)
        extra = self.expand_for_conflicts(question, top)
        seen = {h.chunk_id for h in top + extra}
        extra += [h for h in self.expand_for_definitions(question, top) if h.chunk_id not in seen]
        return top, sorted(extra, key=lambda h: h.chunk_id)

    def exists(self, chunk_id: str) -> bool:
        return chunk_id in self.by_id
