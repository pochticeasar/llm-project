"""Сообщества графа и их сводки. Требование FR-3."""

import json
from collections import Counter

import networkx as nx

from assistant.infra.llm import LLMClient
from assistant.infra.schemas import Community


def _replacement_context(graph: nx.DiGraph, nodes: list[str]) -> set[tuple[str, str]]:
    """Связи замен и их моделей, в том числе пересекающие границу сообщества."""
    members = set(nodes)
    replacements = {
        (source, target)
        for source, target, data in graph.edges(data=True)
        if data.get("type") == "REPLACED_BY" and (source in members or target in members)
    }
    edges = set(replacements)
    for part, _ in replacements:
        for model, _, data in graph.in_edges(part, data=True):
            if data.get("type") != "HAS_PART":
                continue
            edges.add((model, part))
            edges.update((model, target) for target, attrs in graph[model].items()
                         if attrs.get("type") == "BELONGS_TO")
    return edges


def detect_communities(graph: nx.DiGraph) -> list[Community]:
    """Разбивает все узлы на воспроизводимые сообщества Лувена."""
    if not graph:
        return []
    undirected = nx.Graph()
    undirected.add_nodes_from(sorted(graph.nodes))
    for source, target, data in sorted(graph.edges(data=True)):
        weight = data.get("weight", 1)
        if undirected.has_edge(source, target):
            undirected[source][target]["weight"] += weight
        else:
            undirected.add_edge(source, target, weight=weight)
    if not undirected.edges:
        groups = [{node} for node in undirected.nodes]
    else:
        groups = nx.community.louvain_communities(undirected, weight="weight", seed=42)
    ordered = sorted((sorted(group) for group in groups), key=lambda group: group[0])
    return [Community(number, nodes) for number, nodes in enumerate(ordered)]


def summarize_community(community: Community, graph: nx.DiGraph, llm: LLMClient) -> str:
    """Создаёт сводку по внутренним связям и подсчитанным весам сообщества."""
    subgraph = graph.subgraph(community.nodes)
    counts: Counter[str] = Counter()
    by_line: Counter[tuple[str, str]] = Counter()
    facts = []

    def count_failure(source: str, target: str, weight: int) -> None:
        component = graph.nodes[target]["name"]
        counts[component] += weight
        for line, relation in graph[source].items():
            if relation.get("type") == "BELONGS_TO":
                by_line[(graph.nodes[line]["name"], component)] += weight

    for source, target, data in sorted(subgraph.edges(data=True)):
        weight = data.get("weight", 1)
        facts.append(f"{source} -{data['type']}-> {target} (вес {weight})")
        if data["type"] == "FAILED":
            count_failure(source, target, weight)
    # Include incoming failures for components even when their model belongs to another community.
    models = {node for node in subgraph if graph.nodes[node].get("type") == "Model"}
    for source, target, data in sorted(graph.in_edges(subgraph.nodes, data=True)):
        if source in subgraph or data.get("type") != "FAILED":
            continue
        weight = data.get("weight", 1)
        facts.append(f"{source} -FAILED-> {target} (вес {weight})")
        count_failure(source, target, weight)
        models.add(source)
    # Model affiliations may cross the partition boundary but are needed to interpret failures.
    for source in sorted(models):
        if graph.nodes[source].get("type") != "Model":
            continue
        for target, data in sorted(graph[source].items()):
            if data.get("type") == "BELONGS_TO" and (source not in subgraph or target not in subgraph):
                facts.append(f"{source} -BELONGS_TO-> {target} (вес {data.get('weight', 1)})")
    replacement_facts = []
    for source, target in sorted(_replacement_context(graph, community.nodes)):
        data = graph[source][target]
        replacement_facts.append(f"{source} -{data['type']}-> {target} (вес {data.get('weight', 1)})")
    facts = list(dict.fromkeys(facts + replacement_facts))
    failures = [f"Сумма весов FAILED для {component} в линейке {line}: {count}"
                for (line, component), count in sorted(by_line.items())]
    failures += [f"Сумма весов FAILED для {name}: {count}" for name, count in sorted(counts.items())]
    for model in sorted(models):
        if graph.nodes[model].get("type") != "Model":
            continue
        total = sum(data.get("weight", 1) for data in graph[model].values() if data.get("type") == "FAILED")
        if total:
            failures.append(f"Сумма весов FAILED для модели {graph.nodes[model]['name']}: {total}")
    priority = [fact for fact in facts if " -FAILED-> " in fact or " -BELONGS_TO-> " in fact]
    candidates = failures + priority + [fact for fact in facts if fact not in priority]
    messages = [
        {"role": "system", "content": (
            "Составь сводку сообщества не более 150 слов. Выбери не более пяти важных строк из списка. "
            "Верни JSON с массивом lines: скопируй выбранные строки дословно в порядке важности. "
            "Сначала выбери подсчёты FAILED и связи FAILED, затем остальные связи. "
            "Вес означает число подтверждающих фрагментов, а не число случаев отказа."
        )},
        {"role": "user", "content": (
            f"Сущности: {', '.join(sorted(subgraph.nodes))}\n"
            "Строки для сводки:\n" + "\n".join(candidates)
        )},
    ]
    if not facts:
        expected = f"В сообществе {community.community_id} нет связей между сущностями."
        messages[1]["content"] += f"\nНет связей. Верни только эту фразу: {expected}"
        text = llm.chat(messages, max_tokens=128).content.strip()
        return text if text == expected else expected
    schema = {
        "type": "object",
        "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
        "required": ["lines"],
    }
    response = llm.chat(messages, json_schema=schema, max_tokens=256).content
    selected = []
    try:
        lines = json.loads(response)["lines"]
        if isinstance(lines, list):
            for line in lines:
                if isinstance(line, str) and line in candidates and line not in selected:
                    selected.append(line)
    except (ValueError, TypeError, KeyError):
        pass
    # Retain only exact graph facts; reject paraphrases that could change their meaning.
    if not selected:
        selected = candidates
    # Keep context required for comparing failures even if the model omits it.
    selected = list(dict.fromkeys(failures + replacement_facts + priority + selected))
    summary = []
    words = 0
    for line in selected:
        size = len(line.split())
        if words + size > 150:
            continue
        summary.append(line)
        words += size
    return "\n".join(summary)
