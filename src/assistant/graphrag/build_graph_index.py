"""Самостоятельный запуск блока А без зависимости от сообществ и поиска блока Б."""

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
import networkx as nx

from assistant.graphrag.extraction import MAX_EXTRACTION_TOKENS, extract_with_diagnostics
from assistant.graphrag.graph_builder import build_graph
from assistant.infra.chunking import load_chunks
from assistant.infra.config import Settings, make_llm
from assistant.infra.evaluation import evaluate_extraction
from assistant.infra.grounding import load_vocabulary
from assistant.infra.llm import SEED, LLMError


class IndexBuildError(RuntimeError):
    """Неполный или неуспешный прогон нельзя публиковать как готовый граф."""


def _json_bytes(data) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _fingerprints(settings: Settings) -> dict[str, str]:
    paths = sorted((settings.data_dir / "corpus").glob("*.md"))
    paths += [settings.data_dir / "vocabulary.json", settings.data_dir / "gold_dev.json"]
    result = {f"data/{path.relative_to(settings.data_dir)}": hashlib.sha256(path.read_bytes()).hexdigest()
              for path in paths}
    for name in ("extraction.py", "graph_builder.py", "build_graph_index.py"):
        result[f"graphrag/{name}"] = hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
    lock = Path(__file__).resolve().parents[3] / "uv.lock"
    if lock.exists():
        result["uv.lock"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    return result


def runtime_metadata(settings: Settings) -> dict:
    """Паспорт модели и Git; ошибки необязательного API метаданных не скрывают извлечение."""
    metadata: dict = {"model": settings.llm_model}
    root = Path(__file__).resolve().parents[3]
    try:
        metadata["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
        metadata["git_dirty"] = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip())
    except (OSError, subprocess.SubprocessError):
        metadata["git_commit"] = None
    base = settings.llm_base_url.rstrip("/").removesuffix("/v1")
    try:
        with httpx.Client(base_url=base, timeout=5) as client:
            version = client.get("/api/version")
            version.raise_for_status()
            metadata["ollama_version"] = version.json()["version"]
            tags = client.get("/api/tags")
            tags.raise_for_status()
            metadata["model_digest"] = next(
                (row["digest"] for row in tags.json()["models"] if row["name"] == settings.llm_model), None
            )
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        metadata["ollama_metadata"] = "unavailable"
    return metadata


def load_graph(path: Path) -> nx.DiGraph:
    """Загружает тот же graph.json, что использует infra.indexing, без communities.json."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    graph = nx.DiGraph(**data["graph"])
    for node in data["nodes"]:
        graph.add_node(node["id"], **{key: value for key, value in node.items() if key != "id"})
    for edge in data["edges"]:
        graph.add_edge(edge["source"], edge["target"], **{
            key: value for key, value in edge.items() if key not in ("source", "target")
        })
    return graph


def _publish(artifacts: Path, outputs: dict[str, bytes]) -> None:
    """Подготавливает весь комплект, затем атомарно заменяет файлы; graph.json — последним."""
    artifacts.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".block-a-", dir=artifacts) as directory:
        stage = Path(directory)
        for name, content in outputs.items():
            (stage / name).write_bytes(content)
        for name in outputs:
            (stage / name).replace(artifacts / name)


def build_graph_index(settings: Settings, llm=None, log: Callable[[str], None] = print) -> dict:
    """Строит и проверяет полный граф; возвращает статистику только опубликованного прогона."""
    if any((settings.artifacts_dir / name).exists() for name in ("communities.json", "index_stats.json")):
        raise IndexBuildError(
            "Каталог содержит общий индекс. Укажите отдельный ARTIFACTS_DIR для сборки только графа"
        )
    chunks = load_chunks(settings.data_dir / "corpus")
    if not chunks:
        raise IndexBuildError("Корпус пуст: граф не опубликован")
    gold = json.loads((settings.data_dir / "gold_dev.json").read_text(encoding="utf-8"))
    documents = {chunk.doc_id for chunk in chunks}
    missing = set(gold) - documents
    if missing:
        raise IndexBuildError(f"В корпусе отсутствуют эталонные документы: {', '.join(sorted(missing))}")
    fingerprints = _fingerprints(settings)
    metadata = runtime_metadata(settings)
    names = load_vocabulary(settings.data_dir / "vocabulary.json")
    llm = llm if llm is not None else make_llm(settings)
    before = llm.stats.as_dict()
    results, rows = [], []
    for number, chunk in enumerate(chunks, start=1):
        try:
            result, diagnostics = extract_with_diagnostics(chunk, llm, names=names)
        except LLMError:
            log(f"Сервис модели недоступен на {chunk.chunk_id}; прежний граф сохранён")
            raise
        rows.append({"result": asdict(result), "diagnostics": asdict(diagnostics)})
        if diagnostics.status != "ok":
            _publish(settings.artifacts_dir, {"extraction_failures.json": _json_bytes(rows)})
            raise IndexBuildError(f"Не удалось извлечь {chunk.chunk_id}; прежний граф сохранён")
        results.append(result)
        log(f"извлечение {number}/{len(chunks)}: {chunk.chunk_id}, связей {len(result.relations)}")

    if _fingerprints(settings) != fingerprints:
        raise IndexBuildError("Корпус или код изменился во время извлечения; граф не опубликован")
    if {result.doc_id for result in results} != documents or len(results) != len(chunks):
        raise IndexBuildError("Неполное покрытие корпуса; граф не опубликован")
    graph = build_graph(results)
    report = evaluate_extraction(gold, results)
    if report["documents"] != len(gold):
        raise IndexBuildError("Не все эталонные документы вошли в оценку")
    graph_data = {
        "graph": dict(graph.graph),
        "nodes": [{"id": node, **attrs} for node, attrs in graph.nodes(data=True)],
        "edges": [{"source": source, "target": target, **attrs} for source, target, attrs in graph.edges(data=True)],
    }
    graph_bytes = _json_bytes(graph_data)
    stats = {
        "documents": len(documents), "chunks": len(chunks),
        "nodes": len(graph), "edges": graph.number_of_edges(), "conflicts": graph.graph["conflicts"],
        "empty_chunks": sum(not result.entities and not result.relations for result in results),
        "dropped": sum(result.dropped for result in results),
        "dropped_entities": sum(len(row["diagnostics"]["rejected_entities"]) for row in rows),
        "dropped_relations": sum(len(row["diagnostics"]["rejected_relations"]) for row in rows),
        "retried_chunks": sum(row["diagnostics"]["attempts"] > 1 for row in rows),
        "llm": {key: value - before[key] for key, value in llm.stats.as_dict().items()}, "runtime": metadata,
        "generation": {"temperature": 0, "seed": SEED, "max_tokens": MAX_EXTRACTION_TOKENS},
        "fingerprints": fingerprints, "graph_sha256": hashlib.sha256(graph_bytes).hexdigest(),
    }
    _publish(settings.artifacts_dir, {
        "extraction_results.json": _json_bytes(rows), "eval_extraction.json": _json_bytes(report),
        "graph_stats.json": _json_bytes(stats), "graph.json": graph_bytes,
    })
    log(f"граф готов: документов {len(documents)}, чанков {len(chunks)}, узлов {len(graph)}, "
        f"связей {graph.number_of_edges()}, Recall связей {report['relations']['recall']}")
    return stats


def main() -> None:
    try:
        build_graph_index(Settings.from_env())
    except (IndexBuildError, LLMError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
