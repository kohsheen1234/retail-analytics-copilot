"""The corpus must be exactly as delivered, and chunk IDs must not drift.

Chunk IDs are graded against `chunker.py`, and citations are compared across candidates,
so a renumbering would invalidate every citation the agent emits. `artifacts/
corpus_manifest.json` pins both the file hashes and the ID list so this is checkable
without the original pack being present.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from chunker import chunk_corpus

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "artifacts" / "corpus_manifest.json").read_text())

EXPECTED_IDS = [
    "campaign_memo::chunk0",
    "catalog::chunk0", "catalog::chunk1",
    "kpi_definitions::chunk0", "kpi_definitions::chunk1",
    "kpi_definitions::chunk2", "kpi_definitions::chunk3",
    "marketing_calendar::chunk0", "marketing_calendar::chunk1", "marketing_calendar::chunk2",
    "product_policy::chunk0", "product_policy::chunk1", "product_policy::chunk2",
]


def test_provided_documents_are_unmodified():
    """'Do not modify the documents.' This proves it rather than asserting it."""
    for name, digest in MANIFEST["documents"].items():
        path = ROOT / "docs" / name
        assert path.exists(), f"{name} is missing"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, f"{name} was edited"


def test_chunk_ids_are_stable():
    assert [c.id for c in chunk_corpus(ROOT / "docs")] == EXPECTED_IDS


def test_manifest_agrees_with_the_chunker():
    assert MANIFEST["chunk_ids"] == EXPECTED_IDS


def test_spec_worked_examples_land_on_the_right_chunks():
    """Three cross-checks the assessment itself gives us."""
    by_id = {c.id: c for c in chunk_corpus(ROOT / "docs")}
    # "marketing_calendar::chunk1 is the Summer Beverages 2017 section"
    assert "Summer Beverages 2017" in by_id["marketing_calendar::chunk1"].content
    # the output-contract example cites kpi_definitions::chunk1 for AOV
    assert "Average Order Value" in by_id["kpi_definitions::chunk1"].content
    # every provided gold_chunks value must resolve
    for name in ("train.jsonl", "dev.jsonl"):
        for line in (ROOT / "data" / name).read_text().splitlines():
            if line.strip():
                for cid in json.loads(line)["gold_chunks"]:
                    assert cid in by_id, cid


def test_heading_only_chunks_still_consume_an_index():
    """Four documents open with a bare H1; those chunks are numbered, not skipped."""
    by_id = {c.id: c for c in chunk_corpus(ROOT / "docs")}
    assert by_id["kpi_definitions::chunk0"].content == "# KPI Definitions"
    assert by_id["marketing_calendar::chunk0"].content.startswith("# Northwind Marketing Calendar")


def test_a_new_document_is_picked_up_without_disturbing_existing_ids(tmp_path):
    """Rehearsal for the live modification: a handed-over document must not renumber
    the citations the agent has already been emitting."""
    for p in (ROOT / "docs").glob("*.md"):
        (tmp_path / p.name).write_bytes(p.read_bytes())
    (tmp_path / "zz_new_policy.md").write_text("# New Policy\n\n## Section A\n- Something.\n")
    ids = [c.id for c in chunk_corpus(tmp_path)]
    assert EXPECTED_IDS == [i for i in ids if not i.startswith("zz_new_policy")]
    assert "zz_new_policy::chunk1" in ids
