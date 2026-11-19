"""Граничные случаи локального поиска."""

import networkx as nx

from assistant.graphrag.search import local_search
from assistant.infra.schemas import KnowledgeBase
from assistant.infra.testing import FakeLLM


def test_mixed_question_can_return_graph_answer_without_erp_data() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", type="Model", name="AR-210")
    graph.add_node("Part:P-1042B", type="Part", name="P-1042B")
    graph.add_edge("Model:AR-210", "Part:P-1042B", type="HAS_PART", sources=["manual.md#0"])
    llm = FakeLLM(["Подходит деталь P-1042B."])
    answer = local_search("Какая деталь для AR-210, её цена и остаток?", KnowledgeBase(graph, [], {}), llm)
    assert not answer.refused
    assert answer.text == "Подходит деталь P-1042B."
    assert answer.sources == ["manual.md"]
    assert "На составной вопрос ответь по тем частям" in str(llm.requests[0]["messages"])


def test_mixed_question_preserves_replacement_facts_if_model_refuses() -> None:
    graph = nx.DiGraph()
    for kind, name in [("Model", "AR-210"), ("Part", "P-1042"), ("Part", "P-1042B")]:
        graph.add_node(f"{kind}:{name}", type=kind, name=name)
    graph.add_edge("Model:AR-210", "Part:P-1042", type="HAS_PART", sources=["manual.md#0"])
    graph.add_edge("Part:P-1042", "Part:P-1042B", type="REPLACED_BY", sources=["bulletin.md#0"])
    result = local_search(
        "Какая деталь для AR-210, сколько она стоит и есть ли на складе?",
        KnowledgeBase(graph, [], {}), FakeLLM(["в базе знаний не найдено"]),
    )
    assert not result.refused
    assert "P-1042 -REPLACED_BY-> P-1042B" in result.text
    assert result.sources == ["bulletin.md", "manual.md"]


def test_mixed_question_preserves_part_facts_without_replacement() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", type="Model", name="AR-210")
    graph.add_node("Part:P-1042", type="Part", name="P-1042")
    graph.add_edge("Model:AR-210", "Part:P-1042", type="HAS_PART", sources=["manual.md#0"])
    answer = local_search("Какая деталь для AR-210 и её цена?", KnowledgeBase(graph, [], {}),
                          FakeLLM(["в базе знаний не найдено"]))
    assert not answer.refused
    assert "AR-210 -HAS_PART-> P-1042" in answer.text


def test_component_price_question_preserves_graph_facts_if_model_refuses() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:BZ-110", type="Model", name="BZ-110")
    graph.add_node("Part:P-2022", type="Part", name="P-2022")
    graph.add_node("Component:контроллер", type="Component", name="контроллер")
    graph.add_edge("Model:BZ-110", "Part:P-2022", type="HAS_PART", sources=["manual.md#0"])
    graph.add_edge("Part:P-2022", "Component:контроллер", type="IS_A", sources=["manual.md#1"])
    answer = local_search(
        "Сколько стоит деталь узла «контроллер» для модели BZ-110?",
        KnowledgeBase(graph, [], {}), FakeLLM(["в базе знаний не найдено"]),
    )
    assert not answer.refused
    assert "BZ-110 -HAS_PART-> P-2022" in answer.text
    assert answer.sources == ["manual.md"]


def test_empty_model_response_is_refusal() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", type="Model", name="AR-210")
    graph.add_node("Part:P-1042", type="Part", name="P-1042")
    graph.add_edge("Model:AR-210", "Part:P-1042", type="HAS_PART", sources=["manual.md#0"])
    answer = local_search("Какая деталь у AR-210?", KnowledgeBase(graph, [], {}), FakeLLM(["  "]))
    assert answer.refused
    assert answer.text == "В базе знаний не найдено достаточно данных для ответа."


def test_no_part_fact_does_not_turn_model_refusal_into_success() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", type="Model", name="AR-210")
    graph.add_node("ProductLine:Арктика", type="ProductLine", name="Арктика")
    graph.add_edge("Model:AR-210", "ProductLine:Арктика", type="BELONGS_TO", sources=["manual.md#0"])
    answer = local_search("Какая деталь для AR-210 и её цена?", KnowledgeBase(graph, [], {}),
                          FakeLLM(["в базе знаний не найдено"]))
    assert answer.refused


def test_stock_only_question_does_not_turn_graph_facts_into_an_answer() -> None:
    graph = nx.DiGraph()
    for name in ("P-1042", "P-1042B"):
        graph.add_node(f"Part:{name}", type="Part", name=name)
    graph.add_edge("Part:P-1042", "Part:P-1042B", type="REPLACED_BY", sources=["bulletin.md#0"])
    answer = local_search("Сколько штук детали P-1042B на складе?", KnowledgeBase(graph, [], {}),
                          FakeLLM(["в базе знаний не найдено"]))
    assert answer.refused


def test_replacement_chain_cycle_terminates_and_sources_are_documents() -> None:
    graph = nx.DiGraph()
    for node in ("Model:AR-210", "Part:P-1001", "Part:P-1002", "Part:P-1003"):
        kind, name = node.split(":")
        graph.add_node(node, type=kind, name=name)
    graph.add_edge("Model:AR-210", "Part:P-1001", type="HAS_PART", sources=["manual.md#0"])
    graph.add_edge("Part:P-1001", "Part:P-1002", type="REPLACED_BY", sources=["bulletin.md#0"])
    graph.add_edge("Part:P-1002", "Part:P-1003", type="REPLACED_BY", sources=["bulletin.md#1"])
    graph.add_edge("Part:P-1003", "Part:P-1002", type="REPLACED_BY", sources=["other.md#0"])
    result = local_search("AR-210 P-1001", KnowledgeBase(graph, [], {}), FakeLLM(["ответ"]))
    assert any("P-1003" in fact for fact in result.facts)
    assert result.sources == ["bulletin.md", "manual.md", "other.md"]


def test_facts_over_limit_do_not_contribute_sources() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", type="Model", name="AR-210")
    for number in range(120):
        node = f"Part:P-{number:04d}"
        graph.add_node(node, type="Part", name="x" * 100 + str(number))
        graph.add_edge("Model:AR-210", node, type="HAS_PART", sources=[f"doc-{number:03d}.md#0"])
    llm = FakeLLM(["ответ"])
    answer = local_search("AR-210", KnowledgeBase(graph, [], {}), llm)
    lines = [f"{index}. {fact}" for index, fact in enumerate(answer.facts, 1)]
    assert len("\n".join(lines)) <= 8000
    assert 0 < len(answer.facts) < 120
    assert len(answer.sources) == len(answer.facts)


def test_refusal_does_not_call_llm_on_missing_code() -> None:
    graph = nx.DiGraph()
    graph.add_node("Model:AR-210", type="Model", name="AR-210")
    llm = FakeLLM([])
    result = local_search("AR-210 E-99: какая деталь, её цена и остаток?", KnowledgeBase(graph, [], {}), llm)
    assert result.refused and not result.sources and not llm.requests


def test_one_anchor_includes_edges_between_nodes_in_two_step_neighbourhood() -> None:
    graph = nx.DiGraph()
    for node in ("Model:AR-210", "Part:P-1001", "Part:P-1002", "Part:P-1003"):
        kind, name = node.split(":")
        graph.add_node(node, type=kind, name=name)
    graph.add_edge("Model:AR-210", "Part:P-1001", type="HAS_PART", sources=["one.md#0"])
    graph.add_edge("Part:P-1001", "Part:P-1002", type="REPLACED_BY", sources=["two.md#0"])
    graph.add_edge("Part:P-1001", "Part:P-1003", type="REPLACED_BY", sources=["three.md#0"])
    graph.add_edge("Part:P-1002", "Part:P-1003", type="REPLACED_BY", sources=["four.md#0"])
    result = local_search("AR-210", KnowledgeBase(graph, [], {}), FakeLLM(["ответ"]))
    assert "P-1002 -REPLACED_BY-> P-1003" in result.facts
    assert "four.md" in result.sources
