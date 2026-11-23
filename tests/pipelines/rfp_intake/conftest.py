import pytest

from data.pipelines.rfp_intake import llm
from tests.pipelines.rag_support import pinned_rag_settings
from tests.pipelines.rfp_intake.support import FakeModel, sqlite_engine


@pytest.fixture
def engine(tmp_path):
    engine = sqlite_engine(tmp_path / "rfp.sqlite")
    yield engine
    engine.dispose()


@pytest.fixture
def model(monkeypatch):
    """Install a `FakeModel` behind `llm.call_tool`, with pinned model settings."""
    settings = pinned_rag_settings()
    monkeypatch.setattr(llm, "get_rag_settings", lambda: settings)

    def install(*replies):
        fake = FakeModel(*replies)
        monkeypatch.setattr(llm, "_client", lambda: fake)
        return fake

    return install
