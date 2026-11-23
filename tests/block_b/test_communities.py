"""Разбиение графа, сводки и их использование в глобальном поиске."""

import networkx as nx
import pytest

from assistant.graphrag.communities import detect_communities, summarize_community
from assistant.graphrag.search import global_search
from assistant.infra.schemas import Community, KnowledgeBase
from assistant.infra.testing import FakeLLM


def test_empty_and_isolated_graphs() -> None:
    assert detect_communities(nx.DiGraph()) == []
    graph = nx.DiGraph()
    graph.add_nodes_from(["Part:P-1002", "Part:P-1001"])
    communities = detect_communities(graph)
    assert [community.nodes for community in communities] == [["Part:P-1001"], ["Part:P-1002"]]


def test_reciprocal_weights_are_combined_for_community_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = nx.DiGraph()
    graph.add_edge("a", "b", weight=2)
    graph.add_edge("b", "a", weight=3)
    observed = {}

    def partition(undirected: nx.Graph, **kwargs: object) -> list[set[str]]:
        observed["weight"] = undirected["a"]["b"]["weight"]
        return [{"a", "b"}]

    monkeypatch.setattr(nx.community, "louvain_communities", partition)
    detect_communities(graph)
    assert observed["weight"] == 5


def test_summary_counts_only_failures_inside_community() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", name="AR-210")
    graph.add_node("Component:реле", name="реле")
    graph.add_node("Component:датчик", name="датчик")
    graph.add_edge("Model:AR-210", "Component:реле", type="FAILED", weight=3)
    graph.add_edge("Model:AR-210", "Component:датчик", type="FAILED", weight=9)
    llm = FakeLLM(["Сводка"])
    summarize_community(Community(0, ["Model:AR-210", "Component:реле"]), graph, llm)
    prompt = str(llm.requests[0]["messages"])
    assert "реле: 3" in prompt and "датчик" not in prompt


def test_isolated_summary_does_not_invent_absence_of_failures() -> None:
    graph = nx.DiGraph()
    graph.add_node("Procedure:SP-118", name="SP-118", type="Procedure")
    llm = FakeLLM(["Отказы отсутствуют, оборудование работает стабильно."])
    summary = summarize_community(Community(2, ["Procedure:SP-118"]), graph, llm)
    assert summary == "В сообществе 2 нет связей между сущностями."
    assert len(llm.requests) == 1


def test_summary_discards_invented_relations_and_numbers() -> None:
    graph = nx.DiGraph()
    graph.add_node("Part:P-1050", name="P-1050")
    graph.add_node("Component:блок освещения", name="блок освещения")
    graph.add_edge("Part:P-1050", "Component:блок освещения", type="IS_A", weight=1)
    llm = FakeLLM(["Part:P-1050 -IS_A-> Component:пусковое реле (вес 99)"])
    summary = summarize_community(Community(0, list(graph.nodes)), graph, llm)
    assert "пусковое реле" not in summary and "99" not in summary
    assert "Part:P-1050 -IS_A-> Component:блок освещения (вес 1)" in summary


def test_summary_selects_only_valid_fact_lines() -> None:
    graph = nx.DiGraph()
    graph.add_node("Part:P-1001", name="P-1001")
    graph.add_node("Part:P-1002", name="P-1002")
    graph.add_edge("Part:P-1001", "Part:P-1002", type="REPLACED_BY", weight=1)
    llm = FakeLLM(['{"lines": ["выдумка", "Part:P-1001 -REPLACED_BY-> Part:P-1002 (вес 1)"]}'])
    summary = summarize_community(Community(0, list(graph.nodes)), graph, llm)
    assert summary == "Part:P-1001 -REPLACED_BY-> Part:P-1002 (вес 1)"
    assert llm.requests[0]["json_schema"] is not None


def test_summary_includes_cross_community_lineage_and_failure() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", name="AR-210", type="Model")
    graph.add_node("Component:реле", name="реле", type="Component")
    graph.add_node("ProductLine:Арктика", name="Арктика", type="ProductLine")
    graph.add_edge("Model:AR-210", "Component:реле", type="FAILED", weight=2, sources=["report.md#0"])
    graph.add_edge("Model:AR-210", "ProductLine:Арктика", type="BELONGS_TO", weight=1,
                   sources=["manual.md#0"])
    community = Community(0, ["Model:AR-210", "Component:реле"])
    summary = summarize_community(community, graph, FakeLLM(['{"indices": []}']))
    assert "Model:AR-210 -BELONGS_TO-> ProductLine:Арктика (вес 1)" in summary
    assert "Model:AR-210 -FAILED-> Component:реле (вес 2)" in summary
    community.summary = summary
    kb = KnowledgeBase(graph, [community], {})
    result = global_search("вопрос", kb, FakeLLM(['{"answer":"реле","score":3}', "реле"]))
    assert result.sources == ["manual.md", "report.md"]


def test_summary_contains_incoming_failures_and_model_lineage() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", name="AR-210", type="Model")
    graph.add_node("Component:реле", name="реле", type="Component")
    graph.add_node("ProductLine:Арктика", name="Арктика", type="ProductLine")
    graph.add_edge("Model:AR-210", "Component:реле", type="FAILED", weight=2, sources=["report.md#0"])
    graph.add_edge("Model:AR-210", "ProductLine:Арктика", type="BELONGS_TO", weight=1,
                   sources=["manual.md#0"])
    community = Community(0, ["Component:реле"])
    summary = summarize_community(community, graph, FakeLLM(['{"indices": []}']))
    assert "Model:AR-210 -FAILED-> Component:реле (вес 2)" in summary
    assert "Model:AR-210 -BELONGS_TO-> ProductLine:Арктика (вес 1)" in summary
    community.summary = summary
    kb = KnowledgeBase(graph, [community], {})
    llm = FakeLLM(['{"answer":"реле","score":3}', "реле"])
    result = global_search("вопрос", kb, llm)
    assert result.sources == ["manual.md", "report.md"]
    assert "Model:AR-210 -FAILED-> Component:реле" in str(llm.requests[1]["messages"])


def test_summary_connects_replacement_to_model_and_line_across_communities() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:BZ-110", type="Model", name="BZ-110")
    graph.add_node("ProductLine:Бриз", type="ProductLine", name="Бриз")
    graph.add_node("Part:P-2054", type="Part", name="P-2054")
    graph.add_node("Part:P-2054B", type="Part", name="P-2054B")
    graph.add_edge("Model:BZ-110", "ProductLine:Бриз", type="BELONGS_TO", sources=["line.md#0"])
    graph.add_edge("Model:BZ-110", "Part:P-2054", type="HAS_PART", sources=["manual.md#0"])
    graph.add_edge("Part:P-2054", "Part:P-2054B", type="REPLACED_BY", sources=["bulletin.md#0"])
    community = Community(0, ["Part:P-2054B"])
    community.summary = summarize_community(community, graph, FakeLLM(['{"lines": []}']))
    assert "Model:BZ-110 -HAS_PART-> Part:P-2054" in community.summary
    assert "Model:BZ-110 -BELONGS_TO-> ProductLine:Бриз" in community.summary
    assert "Part:P-2054 -REPLACED_BY-> Part:P-2054B" in community.summary
    answer = global_search("Какие детали заменены?", KnowledgeBase(graph, [community], {}),
                           FakeLLM(['{"answer":"P-2054","score":3}', "P-2054"]))
    assert answer.sources == ["bulletin.md", "line.md", "manual.md"]


def test_failure_counts_are_separate_for_each_product_line() -> None:
    graph = nx.DiGraph()
    for node, kind, name in [
        ("Model:AR-210", "Model", "AR-210"), ("Model:GS-510", "Model", "GS-510"),
        ("Component:датчик", "Component", "датчик"), ("ProductLine:Арктика", "ProductLine", "Арктика"),
        ("ProductLine:Гольфстрим", "ProductLine", "Гольфстрим"),
    ]:
        graph.add_node(node, type=kind, name=name)
    graph.add_edge("Model:AR-210", "Component:датчик", type="FAILED", weight=4)
    graph.add_edge("Model:GS-510", "Component:датчик", type="FAILED", weight=1)
    graph.add_edge("Model:AR-210", "ProductLine:Арктика", type="BELONGS_TO", weight=1)
    graph.add_edge("Model:GS-510", "ProductLine:Гольфстрим", type="BELONGS_TO", weight=1)
    result = summarize_community(Community(0, ["Component:датчик"]), graph, FakeLLM(['{"indices":[]}']))
    assert "Сумма весов FAILED для датчик в линейке Арктика: 4" in result
    assert "Сумма весов FAILED для датчик в линейке Гольфстрим: 1" in result
    assert "Сумма весов FAILED для датчик: 5" in result


def test_model_total_includes_failures_in_other_communities() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:BZ-120", type="Model", name="BZ-120")
    graph.add_node("Component:вентилятор", type="Component", name="вентилятор")
    graph.add_node("Component:компрессор", type="Component", name="компрессор")
    graph.add_edge("Model:BZ-120", "Component:вентилятор", type="FAILED", weight=3, sources=["one.md#0"])
    graph.add_edge("Model:BZ-120", "Component:компрессор", type="FAILED", weight=1, sources=["two.md#0"])
    community = Community(0, ["Model:BZ-120", "Component:вентилятор"])
    community.summary = summarize_community(community, graph, FakeLLM(['{"lines": []}']))
    assert "Сумма весов FAILED для модели BZ-120: 4" in community.summary
    answer = global_search("Какая модель?", KnowledgeBase(graph, [community], {}),
                           FakeLLM(['{"answer":"BZ-120","score":3}', "BZ-120"]))
    assert answer.sources == ["one.md", "two.md"]
