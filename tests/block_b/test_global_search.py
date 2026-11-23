"""Граничные случаи глобального поиска."""

import networkx as nx

from assistant.graphrag.search import global_search
from assistant.infra.schemas import Community, KnowledgeBase
from assistant.infra.testing import FakeLLM


def test_global_skips_invalid_scores_and_uses_only_selected_sources() -> None:
    graph = nx.DiGraph()
    graph.add_node("a", sources=["one.md#0"])
    graph.add_node("b", sources=["two.md#1"])
    graph.add_node("c", sources=["three.md#0"])
    kb = KnowledgeBase(graph, [Community(0, ["a"], "a"), Community(1, ["b"], "b"),
                               Community(2, ["c"], "c")], {})
    llm = FakeLLM(['{"answer":"x","score":true}', '{"answer":"y","score":11}',
                   '{"answer":"z","score":2}', "итог"])
    result = global_search("вопрос", kb, llm)
    assert result.text == "итог" and result.sources == ["three.md"]
    assert len(llm.requests) == 4


def test_global_empty_model_response_is_refusal() -> None:
    graph = nx.DiGraph()
    graph.add_node("a", sources=["one.md#0"])
    kb = KnowledgeBase(graph, [Community(0, ["a"], "a")], {})
    result = global_search("вопрос", kb, FakeLLM(['{"answer":"x","score":2}', "  "]))
    assert result.refused
    assert result.text == "В базе знаний не найдено достаточно данных для ответа."
