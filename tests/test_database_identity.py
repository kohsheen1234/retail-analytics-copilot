"""Is this the database the gold answers were computed against?

The published checksum does not match the delivered file (see DECISIONS.md). Rather than
trust either hash, the binding test is semantic: every provided gold SQL statement must
reproduce its provided gold answer. That is what actually has to hold for any number this
agent reports to be meaningful, and no other Northwind build could satisfy it.

The hash test is kept as a cheap "did the file change under me" guard.

Both skip when the database is absent, so the rest of the suite stays runnable.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from agent import config
from sqlite_tool import SQLiteTool

ROOT = config.ROOT


@pytest.fixture(scope="module")
def db():
    if not config.DB_PATH.exists():
        pytest.skip(f"{config.DB_PATH.name} not present")
    return config.DB_PATH


def _examples():
    for name in ("train.jsonl", "dev.jsonl"):
        for line in (ROOT / "data" / name).read_text().splitlines():
            if line.strip():
                ex = json.loads(line)
                if ex["gold_sql"]:
                    yield ex


def _close(got, want) -> bool:
    if isinstance(want, bool) or not isinstance(want, (int, float)):
        return str(got).strip() == str(want).strip()
    return got is not None and abs(float(got) - float(want)) <= 0.011


def test_file_has_not_changed_under_us(db):
    h = hashlib.sha256()
    with open(db, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    digest = h.hexdigest()
    if digest == config.DB_SHA256_PUBLISHED:
        return  # the corrected pack landed; nothing to explain
    assert digest == config.DB_SHA256_OBSERVED, (
        "northwind.sqlite matches neither the published checksum nor the file whose "
        "gold answers were verified; stop and re-verify before trusting any number"
    )


@pytest.mark.parametrize("ex", list(_examples()), ids=lambda e: e["id"])
def test_every_provided_gold_sql_reproduces_its_gold_answer(db, ex):
    """23 statements. This is the real identity check on the database."""
    res = SQLiteTool(db, row_limit=1000).execute(ex["gold_sql"])
    assert res.error is None, res.error
    gold = ex["gold_answer"]

    if isinstance(gold, list):
        assert len(res.rows) == len(gold)
        for row, want in zip(res.rows, gold):
            assert len(row) == len(want)
            for got, exp in zip(row, want.values()):
                assert _close(got, exp), (ex["id"], got, exp)
    elif isinstance(gold, dict):
        assert res.rows and len(res.rows[0]) == len(gold)
        for got, exp in zip(res.rows[0], gold.values()):
            assert _close(got, exp), (ex["id"], got, exp)
    else:
        assert res.rows, "gold answer is a scalar but the query returned no rows"
        assert _close(res.rows[0][0], gold), (ex["id"], res.rows[0][0], gold)
