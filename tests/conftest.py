import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def db_path() -> Path:
    p = ROOT / "data" / "northwind.sqlite"
    if not p.exists():
        pytest.skip("data/northwind.sqlite not downloaded")
    return p


@pytest.fixture(scope="session")
def docs_dir() -> Path:
    return ROOT / "docs"


class FakeLM:
    """Deterministic stand-in for the model in end-to-end tests. Map prompt substrings to canned outputs."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, prompt: str, **kwargs) -> list[str]:
        self.calls.append(prompt)
        for key, val in self.responses.items():
            if key in prompt:
                return [val]
        raise AssertionError(f"FakeLM has no response for prompt: {prompt[:120]!r}")


@pytest.fixture
def fake_lm():
    return FakeLM
