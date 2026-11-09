"""Дополнительные сценарии FR-2: повторная доставка и воспроизводимость."""

import networkx as nx

from assistant.graphrag.graph_builder import build_graph
from assistant.infra.schemas import Entity, ExtractionResult, Relation

OLD = Entity("Part", "P-1042")
NEW = Entity("Part", "P-1042B")


def extracted(chunk_id, relations=(), entities=()):
    return ExtractionResult(chunk_id, chunk_id.split("#")[0], list(entities), list(relations))


def test_empty_input_is_a_directed_graph_without_conflicts():
    graph = build_graph([])
    assert isinstance(graph, nx.DiGraph)
    assert len(graph) == 0 and graph.graph["conflicts"] == 0


def test_repeated_delivery_of_a_chunk_does_not_increase_weight():
    result = extracted("a.md#0", [Relation(OLD, "REPLACED_BY", NEW)])
    graph = build_graph([result, result])
    assert graph.edges[OLD.node_id, NEW.node_id] == {
        "type": "REPLACED_BY", "weight": 1, "sources": ["a.md#0"]
    }
    assert graph.nodes[OLD.node_id]["sources"] == ["a.md#0"]


def test_valid_relation_adds_both_endpoints_even_without_explicit_entities():
    graph = build_graph([extracted("a.md#0", [Relation(OLD, "REPLACED_BY", NEW)])])
    assert graph.nodes[NEW.node_id] == {"type": "Part", "name": NEW.name, "sources": ["a.md#0"]}


def test_isolated_entity_keeps_its_provenance():
    graph = build_graph([extracted("a.md#0", entities=[OLD])])
    assert list(graph.nodes) == [OLD.node_id] and graph.number_of_edges() == 0


def test_sources_and_graph_attributes_do_not_depend_on_input_order():
    results = [
        extracted("b.md#0", [Relation(OLD, "REPLACED_BY", NEW)]),
        extracted("a.md#0", [Relation(OLD, "REPLACED_BY", NEW)]),
    ]
    first, second = build_graph(results), build_graph(list(reversed(results)))
    assert dict(first.nodes(data=True)) == dict(second.nodes(data=True))
    assert dict(first.edges) == dict(second.edges)
    assert first.nodes[OLD.node_id]["sources"] == ["a.md#0", "b.md#0"]


def test_each_opposite_pair_is_counted_once():
    other = Entity("Part", "P-2054")
    latest = Entity("Part", "P-2054B")
    graph = build_graph([extracted("a.md#0", [
        Relation(OLD, "REPLACED_BY", NEW), Relation(NEW, "REPLACED_BY", OLD),
        Relation(other, "REPLACED_BY", latest), Relation(latest, "REPLACED_BY", other),
    ])])
    assert graph.number_of_edges() == 0 and graph.graph["conflicts"] == 2
