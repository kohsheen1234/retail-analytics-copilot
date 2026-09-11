"""The review packet's word limit is a contract term, so it is enforced, not hoped for.

The contract: when `status` is `needs_review`, the packet holds the question, what the
agent understood, the blocker, what it considered, and what a human must decide - under
150 words. The character caps on individual fields bound the common case; this is the
guarantee for the uncommon one.
"""
from __future__ import annotations

from agent.graph_hybrid import REVIEW_PACKET_MAX_WORDS, _fit_packet, _packet_words
from models import ReviewPacket

LONG_SQL = ('SELECT c.CompanyName, ROUND(SUM(od.UnitPrice*od.Quantity*(1-od.Discount)),2) AS r '
            'FROM "Order Details" od JOIN Orders o ON o.OrderID=od.OrderID '
            'JOIN Customers c ON c.CustomerID=o.CustomerID '
            "WHERE date(o.OrderDate) BETWEEN '2019-01-01' AND '2019-12-31' "
            'GROUP BY c.CustomerID ORDER BY r DESC LIMIT 3')


def worst_case() -> ReviewPacket:
    return ReviewPacket(
        question=" ".join(["word"] * 60),
        understood="route=hybrid; window 2019-01-01..2019-12-31; formulas Revenue, AOV; expected shape list[{customer:str, revenue:float}]",
        blocker=("The documents give two different end dates for this campaign and neither "
                 "claims precedence, so the window cannot be fixed responsibly. " * 3)[:400],
        considered=[LONG_SQL] * 6,
        decision_needed=("Confirm which document is authoritative for this figure, then re-run. "
                         "The corpus states no precedence between them."),
    )


def test_the_worst_case_really_is_over_the_limit_before_fitting():
    assert _packet_words(worst_case()) > REVIEW_PACKET_MAX_WORDS


def test_fitting_brings_it_under_the_limit():
    fitted, trimmed = _fit_packet(worst_case())
    assert trimmed is True
    assert _packet_words(fitted) < REVIEW_PACKET_MAX_WORDS


def test_the_question_and_the_decision_are_never_cut():
    original = worst_case()
    fitted, _ = _fit_packet(original)
    assert fitted.question == original.question
    assert fitted.decision_needed == original.decision_needed


def test_the_last_executed_sql_is_kept():
    fitted, _ = _fit_packet(worst_case())
    assert fitted.considered and fitted.considered[0] == LONG_SQL


def test_a_short_packet_is_left_alone():
    short = ReviewPacket(question="How many orders in 2017?", understood="route=sql",
                         blocker="No SQL produced.", considered=["(no SQL produced)"],
                         decision_needed="Supply the expected SQL.")
    fitted, trimmed = _fit_packet(short)
    assert trimmed is False and fitted == short
