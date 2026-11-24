"""Два режима поиска по графу. Требования FR-4, FR-5 и FR-10."""

import json
import re
from itertools import combinations, pairwise

import networkx as nx

from assistant.graphrag.communities import _replacement_context
from assistant.infra.grounding import code_type, find_codes, find_names
from assistant.infra.llm import LLMClient
from assistant.infra.schemas import MAP_SCHEMA, Answer, Community, KnowledgeBase

REFUSAL = "В базе знаний не найдено достаточно данных для ответа."


def _sources(chunks: set[str]) -> list[str]:
    return sorted({chunk.split("#", 1)[0] for chunk in chunks})


def _community_sources(community: Community, graph: nx.DiGraph) -> list[str]:
    subgraph = graph.subgraph(community.nodes)
    chunks = {chunk for _, data in subgraph.nodes(data=True) for chunk in data.get("sources", [])}
    chunks.update(chunk for _, _, data in subgraph.edges(data=True) for chunk in data.get("sources", []))
    for source, target in _replacement_context(graph, community.nodes):
        chunks.update(graph[source][target].get("sources", []))
    models = {node for node in subgraph if graph.nodes[node].get("type") == "Model"}
    for source, _, data in graph.in_edges(subgraph.nodes, data=True):
        if data.get("type") == "FAILED":
            models.add(source)
            chunks.update(data.get("sources", []))
    for node in models:
        for _, data in graph[node].items():
            if data.get("type") in {"BELONGS_TO", "FAILED"}:
                chunks.update(data.get("sources", []))
    return _sources(chunks)


def local_search(question: str, kb: KnowledgeBase, llm: LLMClient) -> Answer:
    """Отбирает факты на путях или в окружении опорных сущностей."""
    graph = kb.graph
    supplier_question = bool(re.search(r"\b(?:поставщик\w*|поставля\w*)\b", question.lower()))
    codes = find_codes(question)
    code_nodes = [f"{code_type(code)}:{code}" for code in codes]
    if any(node not in graph for node in code_nodes):
        return Answer(REFUSAL, refused=True)
    anchors = set(code_nodes)
    anchors.update(f"{kind}:{name}" for kind, name in find_names(question, kb.vocabulary)
                   if f"{kind}:{name}" in graph)
    if not anchors:
        return Answer(REFUSAL, refused=True)

    undirected = graph.to_undirected(as_view=True)
    pairs: set[tuple[str, str]] = set()
    if len(anchors) == 1:
        anchor = next(iter(anchors))
        distances = nx.single_source_shortest_path_length(undirected, anchor, cutoff=2)
        pairs.update((a, b) for a, b in graph.edges if a in distances and b in distances)
    else:
        for start, end in combinations(sorted(anchors), 2):
            for path in nx.all_simple_paths(undirected, start, end, cutoff=3):
                for a, b in pairwise(path):
                    if graph.has_edge(a, b):
                        pairs.add((a, b))
                    if graph.has_edge(b, a):
                        pairs.add((b, a))
        # Follow replacement chains only from parts on selected paths.
        pending = [node for edge in pairs for node in edge if graph.nodes[node].get("type") == "Part"]
        visited = set()
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            for target, data in graph[node].items():
                if data.get("type") == "REPLACED_BY":
                    pairs.add((node, target))
                    pending.append(target)
        # A supplier is one edge beyond the replacement chain, not between the anchors.
        if supplier_question:
            for part in visited:
                pairs.update((part, target) for target, data in graph[part].items()
                             if data.get("type") == "SUPPLIED_BY")

    facts: list[str] = []
    chunks: set[str] = set()
    length = 0
    for source, target in sorted(pairs):
        data = graph.edges[source, target]
        fact = f"{graph.nodes[source]['name']} -{data['type']}-> {graph.nodes[target]['name']}"
        line = f"{len(facts) + 1}. {fact}"
        if length + len(line) + (1 if facts else 0) > 8000:
            break
        facts.append(fact)
        length += len(line) + (1 if len(facts) > 1 else 0)
        chunks.update(data.get("sources", []))
    if not facts:
        return Answer(REFUSAL, refused=True)
    numbered = "\n".join(f"{number}. {fact}" for number, fact in enumerate(facts, 1))
    supplier_instruction = ""
    if supplier_question:
        supplier_instruction = (
            " На вопрос о поставщике ответь одним предложением: назови поставщика последней детали "
            "цепочки замен по SUPPLIED_BY. Не пересказывай цепочку и не переписывай коды оборудования."
        )
    messages = [
        {"role": "system", "content": (
            "Ты отвечаешь только за сведения из графа: модель, ошибка, деталь, замена, узел, "
            "регламент и поставщик. Кратко ответь по приведённым фактам на эту часть вопроса. "
            "В записи A -ТИП-> B направление идёт от A к B: "
            "INDICATES означает, что код ошибки A указывает на отказ узла B; "
            "FIXED_BY - ошибка A устраняется по регламенту B; "
            "HAS_PART - в модели A установлена деталь B; IS_A - деталь A является узлом B; "
            "BELONGS_TO - модель A входит в линейку B; SUPPLIED_BY - деталь A поставляется B; "
            "FAILED - в модели A зафиксирован отказ узла B; REPLACED_BY - деталь A заменена деталью B. "
            "Для вопроса о регламенте назови регламент, об узле - узел, о поставщике - поставщика. "
            "На составной вопрос ответь по тем частям, которые подтверждены фактами. "
            "Цена, остатки и оформление заказа относятся к ERP: не придумывай их и не утверждай, "
            "что заказ создан. Если часть ответа найдена, не используй фразу 'не найдено'. "
            "Если в фактах нет ответа ни на одну часть вопроса, "
            "напиши: в базе знаний не найдено. При наличии цепочки REPLACED_BY укажи последнюю замену детали."
            + supplier_instruction
        )},
        {"role": "user", "content": (
            f"Факты:\n{numbered}\n\nВопрос: {question}\n\n"
            "Ответь по фактам графа. Цену, остаток и оформление заказа определяет другой инструмент, "
            "они не мешают ответить про деталь или замену."
        )},
    ]
    text = llm.chat(messages, max_tokens=256).content.strip()
    if not text:
        return Answer(REFUSAL, refused=True)
    question_lower = question.lower()
    if (
        "не найдено" in text.lower()
        and re.search(r"\b(?:какую|какая|какой|какие)\s+детал|\bдетал\w*\s+(?:для|при|узла)\b", question_lower)
        and any(word in question_lower for word in ("цен", "стои", "склад", "остат"))
        and any(" -HAS_PART-> " in fact or " -REPLACED_BY-> " in fact for fact in facts)
    ):
        return Answer(f"Подтверждённые факты графа:\n{numbered}", sources=_sources(chunks), facts=facts)
    return Answer(text, sources=_sources(chunks), facts=facts, refused="не найдено" in text.lower())


def global_search(question: str, kb: KnowledgeBase, llm: LLMClient) -> Answer:
    """Объединяет полезные частичные ответы по сводкам сообществ."""
    partials: list[str] = []
    sources: set[str] = set()
    for community in kb.communities:
        messages = [
            {"role": "system", "content": (
                "Кратко ответь на вопрос только по сводке сообщества. Верни JSON с answer и score от 0 до 10. "
                "FAILED означает отказ узла, а не снятие детали с производства. REPLACED_BY означает, "
                "что деталь слева снята с производства и заменена деталью справа. "
                "Для вопроса о снятых деталях линейки указывай номера снятых деталей по связям "
                "REPLACED_BY, HAS_PART и BELONGS_TO; отказы FAILED не подтверждают снятие. "
                "Для сравнения частоты сохрани в answer названия и готовые суммы весов по нужной линейке. "
                "Если спрашивают о модели с наибольшим количеством отказов, сравнивай готовые суммы для моделей. "
                "Не считай суммы самостоятельно и не подменяй их суммами по всему сообществу. "
                "Для вопроса о линейке учитывай только модели с подтверждённой связью BELONGS_TO с этой линейкой; "
                "сумма весов для всего сообщества не означает частоту в отдельной линейке. "
                "Если сводка не подтверждает нужные сведения, верни пустой answer и score 0."
            )},
            {"role": "user", "content": f"Сводка:\n{community.summary}\n\nВопрос: {question}"},
        ]
        response = llm.chat(messages, json_schema=MAP_SCHEMA, max_tokens=256)
        try:
            payload = json.loads(response.content)
            answer, score = payload["answer"], payload["score"]
            if not isinstance(answer, str) or type(score) is not int or not 0 <= score <= 10:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        if score > 0 and answer.strip():
            partials.append(f"Частичный ответ: {answer.strip()}\nПодтверждающие факты:\n{community.summary}")
            sources.update(_community_sources(community, kb.graph))
    if not partials:
        return Answer(REFUSAL, refused=True)
    detail_instruction = ""
    if re.search(r"снят\w*.*производств", question.lower()):
        detail_instruction = (
            " Перечисли именно коды снятых деталей (P-...), а не названия узлов. "
            "Для каждого кода проверь цепочку: модель -BELONGS_TO-> нужная линейка, "
            "та же модель -HAS_PART-> снятая деталь, снятая деталь -REPLACED_BY-> замена. "
            "В ответ включай левую деталь REPLACED_BY, не её замену и не детали других линеек."
        )
    messages = [
        {"role": "system", "content": (
            "Объедини частичные ответы на вопрос, опираясь только на них и подтверждающие факты. "
            "Отвечай кратко, без рассуждений и без пересказа всего контекста. "
            "Для снятых деталей используй REPLACED_BY: снята деталь слева, FAILED означает отказ узла. "
            "Для сравнения используй только готовые суммы весов именно для указанной линейки, "
            "не считай самостоятельно и не подставляй общие суммы для всего сообщества. "
            "Вес каждого ребра - число подтверждающих его фрагментов. Сумма весов может считать "
            "один фрагмент несколько раз; не называй её числом уникальных фрагментов или инцидентов. "
            "Если данных недостаточно, напиши: в базе знаний не найдено." + detail_instruction
        )},
        {"role": "user", "content": f"Вопрос: {question}\nЧастичные ответы:\n" + "\n".join(partials)},
    ]
    text = llm.chat(messages, max_tokens=384).content.strip()
    if not text:
        return Answer(REFUSAL, refused=True)
    return Answer(text, sources=sorted(sources), refused="не найдено" in text.lower())
