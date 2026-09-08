"""The database must be the exact file the assessment pins.

The brief is explicit that a different Northwind build circulates online under the same
filename and that its numbers will not match. Every gold answer in train/dev, and every
number this agent reports, is only meaningful against the pinned file, so its identity is
asserted rather than assumed.

Skips rather than fails when the database is absent, so the rest of the suite stays
runnable without it.
"""
from __future__ import annotations

import hashlib

import pytest

from agent import config


@pytest.fixture(scope="module")
def db():
    if not config.DB_PATH.exists():
        pytest.skip(f"{config.DB_PATH.name} not present")
    return config.DB_PATH


def test_database_matches_the_published_checksum(db):
    h = hashlib.sha256()
    with open(db, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    assert h.hexdigest() == config.DB_SHA256, (
        "data/northwind.sqlite is not the pinned build; gold answers will not match"
    )
