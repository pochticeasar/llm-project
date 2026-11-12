"""Инструменты агента. Требование FR-6."""

from collections.abc import Callable

from assistant.graphrag import search
from assistant.infra.erp_client import ErpClient, ErpNotFound, ErpRejected
from assistant.infra.grounding import code_type, normalize_code
from assistant.infra.llm import LLMClient
from assistant.infra.schemas import KnowledgeBase

WAREHOUSES = ["Москва", "Санкт-Петербург", "Казань", "Новосибирск"]

_QUESTION = {"type": "string", "description": "Вопрос на русском языке с кодами модели, ошибки или детали"}
_PART_ID = {"type": "string", "description": "Номер детали вида P-1042 или P-1042B"}
_WAREHOUSE = {"type": "string", "enum": WAREHOUSES, "description": "Склад из списка"}


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """Описание одного инструмента в формате function calling."""
    parameters = {"type": "object", "properties": properties, "required": required}
    return {"type": "function", "function": {"name": name, "description": description, "parameters": parameters}}


TOOL_SCHEMAS: list[dict] = [
    _function(
        "search_knowledge",
        "Поиск по базе знаний из документов: какой узел отказал по коду ошибки, какая деталь стоит в модели, "
        "чем заменена снятая с производства деталь, кто поставщик, по какому регламенту чинить. "
        "Остатков и цен здесь нет.",
        {"question": _QUESTION},
        ["question"],
    ),
    _function(
        "summarize_topic",
        "Обзор по всей базе знаний: что отказывает чаще всего, какие детали сняты с производства в линейке.",
        {"question": _QUESTION},
        ["question"],
    ),
    _function(
        "check_stock",
        "Остаток детали на складе по данным ERP и признак discontinued (деталь снята с производства).",
        {"part_id": _PART_ID, "warehouse": _WAREHOUSE},
        ["part_id", "warehouse"],
    ),
    _function(
        "get_price",
        "Цена детали в рублях по данным ERP и признак discontinued (деталь снята с производства).",
        {"part_id": _PART_ID},
        ["part_id"],
    ),
    _function(
        "create_service_order",
        "Создать заказ детали на склад. Только по явной просьбе пользователя и с количеством, которое он назвал.",
        {
            "part_id": _PART_ID,
            "warehouse": _WAREHOUSE,
            "qty": {"type": "integer", "minimum": 1, "description": "Количество штук, названное пользователем"},
            "model": {"type": "string", "description": "Код модели оборудования вида AR-210"},
        },
        ["part_id", "warehouse", "qty", "model"],
    ),
]

SCHEMAS_BY_NAME = {schema["function"]["name"]: schema for schema in TOOL_SCHEMAS}


def canonical_code(raw, kind: str) -> str | None:
    """Код в каноническом виде, если запись является кодом типа kind (Part, Model и т. д.), иначе None."""
    code = normalize_code(str(raw))
    if code is not None and code_type(code) == kind:
        return code
    return None


def validate_arguments(name: str, args: dict) -> str | None:
    """Текст ошибки, если аргументы инструмента не проходят проверку, иначе None.

    Сначала сверяется набор аргументов со схемой, затем значения: номер детали вида P-1042 или P-1042B,
    склад из списка, количество больше нуля, код модели вида AR-210.
    """
    schema = SCHEMAS_BY_NAME.get(name)
    if schema is None:
        return f"неизвестный инструмент {name}"
    parameters = schema["function"]["parameters"]
    missing = [key for key in parameters["required"] if key not in args]
    unknown = [key for key in args if key not in parameters["properties"]]
    if missing:
        return f"не хватает аргументов: {', '.join(missing)}"
    if unknown:
        return f"лишние аргументы: {', '.join(unknown)}"
    if "question" in args and not str(args["question"]).strip():
        return "вопрос пустой"
    if "part_id" in args and canonical_code(args["part_id"], "Part") is None:
        return f"неверный номер детали {args['part_id']!r}: нужен вид P-1042 или P-1042B"
    if "warehouse" in args and args["warehouse"] not in WAREHOUSES:
        return f"неизвестный склад {args['warehouse']!r}, допустимые: {', '.join(WAREHOUSES)}"
    if "qty" in args and not (isinstance(args["qty"], int) and args["qty"] > 0):
        return f"количество {args['qty']!r} должно быть целым числом больше нуля"
    if "model" in args and canonical_code(args["model"], "Model") is None:
        return f"неверный код модели {args['model']!r}: нужен вид AR-210"
    return None


def build_tools(kb: KnowledgeBase, llm: LLMClient, erp: ErpClient) -> dict[str, Callable[..., dict]]:
    """Словарь «имя инструмента -> функция». Каждая функция возвращает словарь.

    Функция проверяет свои аргументы до обращения к ERP: склад из списка, номер детали вида P-1042
    или P-1042B, количество больше нуля. При ошибке возвращает {"error": "..."} и ничего не выполняет.
    `ErpNotFound` и `ErpRejected` превращаются в {"error": "..."} с разным текстом, чтобы исходы
    оставались различимыми. `ErpUnavailable` передаётся дальше: повторы делает агент (FR-8).
    """

    def search_knowledge(question: str) -> dict:
        error = validate_arguments("search_knowledge", {"question": question})
        if error:
            return {"error": error}
        answer = search.local_search(question, kb, llm)
        return {"answer": answer.text, "facts": answer.facts, "sources": answer.sources, "refused": answer.refused}

    def summarize_topic(question: str) -> dict:
        error = validate_arguments("summarize_topic", {"question": question})
        if error:
            return {"error": error}
        answer = search.global_search(question, kb, llm)
        return {"answer": answer.text, "sources": answer.sources, "refused": answer.refused}

    def check_stock(part_id: str, warehouse: str) -> dict:
        error = validate_arguments("check_stock", {"part_id": part_id, "warehouse": warehouse})
        if error:
            return {"error": error}
        return _ask_erp(erp.get_stock, canonical_code(part_id, "Part"), warehouse)

    def get_price(part_id: str) -> dict:
        error = validate_arguments("get_price", {"part_id": part_id})
        if error:
            return {"error": error}
        return _ask_erp(erp.get_price, canonical_code(part_id, "Part"))

    def create_service_order(part_id: str, warehouse: str, qty: int, model: str) -> dict:
        args = {"part_id": part_id, "warehouse": warehouse, "qty": qty, "model": model}
        error = validate_arguments("create_service_order", args)
        if error:
            return {"error": error}
        part, model = canonical_code(part_id, "Part"), canonical_code(model, "Model")
        return _ask_erp(erp.create_order, part, warehouse, qty, model)

    return {
        "search_knowledge": search_knowledge,
        "summarize_topic": summarize_topic,
        "check_stock": check_stock,
        "get_price": get_price,
        "create_service_order": create_service_order,
    }


def _ask_erp(method: Callable[..., dict], *args) -> dict:
    """Запрос к ERP. «Данных нет» и отказ ERP возвращаются как error с разным текстом, ErpUnavailable идёт к агенту."""
    try:
        return method(*args)
    except ErpNotFound as exc:
        return {"error": f"данных нет: {exc}"}
    except ErpRejected as exc:
        return {"error": f"ERP отклонила запрос: {exc}"}
