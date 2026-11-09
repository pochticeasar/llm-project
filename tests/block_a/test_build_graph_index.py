"""Самостоятельный handoff: полный набор документов, диагностика и безопасная публикация."""

import json

import pytest

from assistant.graphrag import build_graph_index as module
from assistant.infra.config import Settings
from assistant.infra.llm import LLMError
from assistant.infra.testing import FakeLLM

GOOD = json.dumps({"entities": [], "relations": [{
    "subject_type": "Part", "subject": "P-1042", "type": "REPLACED_BY",
    "object_type": "Part", "object": "P-1042B",
}]})
EMPTY = '{"entities": [], "relations": []}'


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    from assistant.graphrag import extraction

    data = tmp_path / "data"
    corpus = data / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "a.md").write_text("# Детали\n\nP-1042 заменена на P-1042B.")
    (corpus / "b.md").write_text("# Плановая чистка\n\nБез ошибок.")
    (data / "vocabulary.json").write_text("{}")
    gold = {
        "a.md": {"entities": ["Part:P-1042", "Part:P-1042B"],
                 "relations": [["Part:P-1042", "REPLACED_BY", "Part:P-1042B"]], "optional_relations": []},
        "b.md": {"entities": [], "relations": [], "optional_relations": []},
    }
    (data / "gold_dev.json").write_text(json.dumps(gold))
    monkeypatch.setattr(extraction, "vocabulary", lambda: {})
    monkeypatch.setattr(module, "runtime_metadata", lambda settings: {"model": settings.llm_model})
    return Settings(data_dir=data, artifacts_dir=tmp_path / "artifacts")


def test_build_saves_compatible_graph_and_scores_all_gold_documents(settings):
    stats = module.build_graph_index(settings, FakeLLM([GOOD, EMPTY]), log=lambda line: None)
    graph = module.load_graph(settings.artifacts_dir / "graph.json")
    assert graph.edges["Part:P-1042", "Part:P-1042B"] == {
        "type": "REPLACED_BY", "weight": 1, "sources": ["a.md#0"]
    }
    assert graph.graph["conflicts"] == 0
    assert stats["documents"] == 2 and stats["chunks"] == 2 and stats["empty_chunks"] == 1
    report = json.loads((settings.artifacts_dir / "eval_extraction.json").read_text())
    assert report["documents"] == 2 and report["relations"]["recall"] == 1.0
    assert not (settings.artifacts_dir / "communities.json").exists()
    assert not (settings.artifacts_dir / "index_stats.json").exists()
    rows = json.loads((settings.artifacts_dir / "extraction_results.json").read_text())
    assert len(rows) == 2 and rows[1]["diagnostics"]["status"] == "ok"


def test_invalid_responses_do_not_replace_previous_graph(settings):
    settings.artifacts_dir.mkdir()
    path = settings.artifacts_dir / "graph.json"
    path.write_text("previous graph")
    with pytest.raises(module.IndexBuildError, match="a.md#0"):
        module.build_graph_index(settings, FakeLLM(["broken", "broken"]), log=lambda line: None)
    assert path.read_text() == "previous graph"
    failures = json.loads((settings.artifacts_dir / "extraction_failures.json").read_text())
    assert failures[0]["diagnostics"]["status"] == "invalid_response"


def test_transport_failure_does_not_publish_a_graph(settings):
    class OfflineLLM(FakeLLM):
        def chat(self, *args, **kwargs):
            raise LLMError("offline")

    with pytest.raises(LLMError, match="offline"):
        module.build_graph_index(settings, OfflineLLM([]), log=lambda line: None)
    assert not (settings.artifacts_dir / "graph.json").exists()


def test_missing_gold_document_fails_before_model_calls(settings):
    (settings.data_dir / "corpus" / "b.md").unlink()
    llm = FakeLLM([])
    with pytest.raises(module.IndexBuildError, match="b.md"):
        module.build_graph_index(settings, llm, log=lambda line: None)
    assert llm.requests == []


def test_graph_is_identical_when_run_again(settings):
    module.build_graph_index(settings, FakeLLM([GOOD, EMPTY]), log=lambda line: None)
    before = (settings.artifacts_dir / "graph.json").read_bytes()
    module.build_graph_index(settings, FakeLLM([GOOD, EMPTY]), log=lambda line: None)
    assert (settings.artifacts_dir / "graph.json").read_bytes() == before


def test_input_changes_during_extraction_are_detected(settings):
    class MutatingLLM(FakeLLM):
        def chat(self, *args, **kwargs):
            (settings.data_dir / "corpus" / "a.md").write_text("changed")
            return super().chat(*args, **kwargs)

    with pytest.raises(module.IndexBuildError, match="изменил"):
        module.build_graph_index(settings, MutatingLLM([GOOD, EMPTY]), log=lambda line: None)
    assert not (settings.artifacts_dir / "graph.json").exists()


def test_runner_uses_its_settings_vocabulary_instead_of_global_cached_vocabulary(settings, monkeypatch):
    from assistant.graphrag import extraction

    monkeypatch.setattr(extraction, "vocabulary", lambda: {"Component": ["чужое название"]})
    (settings.data_dir / "vocabulary.json").write_text(json.dumps({"Component": ["особый узел"]}))
    (settings.data_dir / "corpus" / "a.md").write_text("# Детали\n\nP-1042 — особый узел.")
    answer = json.dumps({"entities": [], "relations": [{
        "subject_type": "Part", "subject": "P-1042", "type": "IS_A",
        "object_type": "Component", "object": "особый узел",
    }]})
    module.build_graph_index(settings, FakeLLM([answer, EMPTY]), log=lambda line: None)
    graph = module.load_graph(settings.artifacts_dir / "graph.json")
    assert graph.has_edge("Part:P-1042", "Component:особый узел")


@pytest.mark.parametrize("existing", ["communities.json", "index_stats.json"])
def test_graph_only_run_cannot_mix_new_graph_with_an_existing_full_index(settings, existing):
    settings.artifacts_dir.mkdir()
    (settings.artifacts_dir / existing).write_text("previous full index")
    llm = FakeLLM([])
    with pytest.raises(module.IndexBuildError, match="ARTIFACTS_DIR"):
        module.build_graph_index(settings, llm, log=lambda line: None)
    assert llm.requests == []


def test_stats_count_only_current_run_when_client_is_reused(settings):
    llm = FakeLLM([GOOD, EMPTY, GOOD, EMPTY])
    first = module.build_graph_index(settings, llm, log=lambda line: None)
    second = module.build_graph_index(settings, llm, log=lambda line: None)
    assert first["llm"]["calls"] == second["llm"]["calls"] == 2
