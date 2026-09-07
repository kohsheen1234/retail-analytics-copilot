import pytest
from models import OutputRecord, ReviewPacket, check_format


def test_check_format_vocabulary():
    assert check_format(14, "int")
    assert not check_format(14.0, "int")
    assert check_format(21018.7, "float")
    assert check_format({"category": "Confections", "quantity": 18320}, "{category:str, quantity:int}")
    assert not check_format({"category": "Confections"}, "{category:str, quantity:int}")
    assert check_format([{"product": "Chai", "revenue": 1.5}], "list[{product:str, revenue:float}]")
    assert not check_format([{"product": "Chai", "revenue": "1.5"}], "list[{product:str, revenue:float}]")


def test_output_record_consistency():
    OutputRecord(id="x", status="answered", final_answer=1, confidence=0.5)
    with pytest.raises(ValueError):
        OutputRecord(id="x", status="answered", final_answer=None, confidence=0.5)
    with pytest.raises(ValueError):
        OutputRecord(id="x", status="needs_review", confidence=0.2,
                     review_packet=ReviewPacket(question="q", understood="u", blocker="b", decision_needed="d"))
