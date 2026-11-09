"""Сборка графа знаний из результатов извлечения. Требование FR-2."""

import networkx as nx

from assistant.infra.schemas import Entity, ExtractionResult, Relation


def build_graph(results: list[ExtractionResult]) -> nx.DiGraph:
    """Объединяет результаты по всем чанкам в один направленный граф.

    Узел: идентификатор `Entity.node_id`, атрибуты `type`, `name`, `sources` (список chunk_id).
    Ребро: атрибуты `type`, `weight` (число разных чанков, где связь найдена), `sources`.
    Связи, не соответствующие схеме (`Relation.matches_schema`), отбрасываются. Если между двумя
    деталями есть `REPLACED_BY` в обе стороны, остаётся связь с большим весом, при равенстве
    отбрасываются обе. Число таких конфликтов записывается в `graph.graph["conflicts"]`.
    """
    entity_sources: dict[Entity, set[str]] = {}
    relation_sources: dict[Relation, set[str]] = {}
    for result in results:
        for entity in result.entities:
            entity_sources.setdefault(entity, set()).add(result.chunk_id)
        for relation in result.relations:
            if not relation.matches_schema():
                continue
            relation_sources.setdefault(relation, set()).add(result.chunk_id)
            for entity in (relation.subject, relation.object):
                entity_sources.setdefault(entity, set()).add(result.chunk_id)

    graph = nx.DiGraph(conflicts=0)
    for entity in sorted(entity_sources, key=lambda entity: entity.node_id):
        graph.add_node(
            entity.node_id, type=entity.type, name=entity.name, sources=sorted(entity_sources[entity])
        )
    for relation in sorted(relation_sources, key=lambda r: (r.subject.node_id, r.object.node_id, r.type)):
        sources = sorted(relation_sources[relation])
        graph.add_edge(
            relation.subject.node_id, relation.object.node_id, type=relation.type, weight=len(sources), sources=sources
        )

    _resolve_replacements(graph)
    return graph


def _resolve_replacements(graph: nx.DiGraph) -> None:
    """Сравнивает каждую встречную пару один раз, после накопления всех свидетельств."""
    for source, target, edge in list(graph.edges(data=True)):
        if edge["type"] != "REPLACED_BY" or source >= target:
            continue
        reverse = graph.get_edge_data(target, source)
        if reverse is None or reverse["type"] != "REPLACED_BY":
            continue
        graph.graph["conflicts"] += 1
        if edge["weight"] <= reverse["weight"]:
            graph.remove_edge(source, target)
        if reverse["weight"] <= edge["weight"]:
            graph.remove_edge(target, source)
