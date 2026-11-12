"""Цикл агента. Требования FR-7, FR-8, FR-9 и FR-10."""

import json
import re
from collections.abc import Callable

from assistant.agent.tools import TOOL_SCHEMAS, WAREHOUSES, canonical_code, validate_arguments
from assistant.infra.erp_client import ErpUnavailable
from assistant.infra.grounding import find_codes
from assistant.infra.llm import LLMClient, tool_message
from assistant.infra.schemas import AgentResult, ToolStep

SYSTEM_PROMPT = """Ты ассистент сервисной службы «ПолярТех». Диспетчер называет модель оборудования и код ошибки,
а ты выясняешь, какую деталь заказать, есть ли она на складе и сколько она стоит.
Своих знаний о деталях у тебя нет: отвечай только по результатам инструментов.

Порядок работы:
1. Вопрос о детали, узле, ошибке, замене, поставщике или регламенте: сначала вызови search_knowledge
   и передай ему вопрос пользователя целиком, вместе с кодами модели и ошибки.
2. Обзорный вопрос по всей базе (что отказывает чаще всего, какие детали сняты с производства): summarize_topic.
3. Остатки и цены есть только в ERP: check_stock и get_price. Номер детали бери точно из результата
   search_knowledge или из вопроса. Если деталь заменена (REPLACED_BY), нужна последняя деталь цепочки замен.
4. create_service_order вызывай, только когда пользователь просит оформить заказ. Количество бери из его слов.

Правила ответа:
- Если search_knowledge вернул refused=true, данных по вопросу нет: ответь «В базе знаний не найдено»
  и не подставляй другие коды.
- «Данных нет» в ERP и остаток 0 различаются: 0 значит, что деталь заведена в ERP, но на складе закончилась.
- Если ERP не ответила, так и напиши: данные ERP получить не удалось. Цены и остатки по памяти не называй.
- Деталь с discontinued=true не заказывай, назови её замену из базы знаний.
- Пиши кратко и по-русски. Номера деталей, цены (руб.) и остатки (шт.) бери только из результатов инструментов.
"""

REFUSAL = "В базе знаний не найдено."
ERP_ATTEMPTS = 3
ERP_DOWN = "ERP не ответила"
NUMBER_WORDS = {
    "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}

# Число, стоящее отдельно от кодов и слов. Разряды могут быть разделены пробелом: «2 630».
_NUMBER = r"(?<![\w-])(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?![\w-])"
_PRICE_OR_QTY = re.compile(rf"({_NUMBER})\s*(?:руб|₽|шт)")


def run_agent(
    question: str,
    llm: LLMClient,
    tools: dict[str, Callable[..., dict]],
    *,
    max_steps: int = 6,
    confirm: bool = False,
) -> AgentResult:
    """Ведёт диалог с моделью в цикле «рассуждение, вызов инструмента, наблюдение».

    Модель получает `TOOL_SCHEMAS` через `llm.chat(..., tools=...)`. Каждый вызов записывается
    в `steps` как `ToolStep`. Агент сам проверяет номер детали в аргументах (он должен встречаться
    в вопросе или в результатах прошлых инструментов), количество в заказе (должно быть названо
    пользователем) и повторные вызовы. При `ErpUnavailable` вызов повторяется до двух раз.
    Заказ создаётся только при `confirm=True`, иначе агент возвращает `needs_confirmation=True`
    и `pending_action`. Перед выдачей ответа коды, цены и остатки сверяются с вопросом
    и результатами инструментов.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": question}]
    steps: list[ToolStep] = []
    corrections_left = 1

    while True:
        response = llm.chat(messages, tools=TOOL_SCHEMAS)
        messages.append(response.as_message())

        if not response.tool_calls:
            problem = _answer_problem(response.content, question, steps)
            if problem is None:
                return _final(response.content, steps)
            if corrections_left == 0:
                text = f"{REFUSAL} Ответ модели не подтверждён результатами инструментов."
                return AgentResult(text, steps, _sources(steps), refused=True)
            corrections_left -= 1
            messages.append({"role": "user", "content": _correction_request(problem)})
            continue

        for call in response.tool_calls:
            if len(steps) >= max_steps:
                return _interrupted(steps, max_steps)
            args = _clean_arguments(call.arguments)
            error = _check_call(call.name, args, question, steps, tools)
            if error:
                steps.append(ToolStep(call.name, args, error=error))
                messages.append(tool_message(call, {"error": error}))
                continue
            if call.name == "create_service_order":
                stop = _order_stop(args, question, steps, confirm)
                if stop is not None:
                    return stop
            result = _execute(call.name, args, tools, steps)
            if "error" in result:
                steps.append(ToolStep(call.name, args, error=result["error"]))
            else:
                steps.append(ToolStep(call.name, args, result=result))
            messages.append(tool_message(call, result))


def _clean_arguments(arguments: dict) -> dict:
    """Приводит аргументы модели к виду, который ждут инструменты: коды и склад канонически, количество числом."""
    args = dict(arguments)
    for key, kind in (("part_id", "Part"), ("model", "Model")):
        code = canonical_code(args.get(key, ""), kind)
        if code is not None:
            args[key] = code
    warehouse = _canonical_warehouse(args.get("warehouse", ""))
    if warehouse is not None:
        args["warehouse"] = warehouse
    if isinstance(args.get("qty"), str) and args["qty"].isdigit():
        args["qty"] = int(args["qty"])
    return args


def _canonical_warehouse(raw) -> str | None:
    """Склад из списка по записи в любом падеже: «в Казани» -> «Казань»."""
    text = str(raw).lower()
    for warehouse in WAREHOUSES:
        if warehouse[:-1].lower() in text:
            return warehouse
    return None


def _check_call(name: str, args: dict, question: str, steps: list[ToolStep], tools: dict) -> str | None:
    """Проверки до выполнения: инструмент известен, аргументы разобраны и верны, коды деталей и моделей не выдуманы."""
    if name not in tools:
        return f"неизвестный инструмент {name}"
    if "_raw" in args:
        return "аргументы не удалось разобрать как JSON"
    error = validate_arguments(name, args)
    if error:
        return error
    known = _known_codes(question, steps)
    for key in ("part_id", "model"):
        if key in args and args[key] not in known:
            return (
                f"код {args[key]} не встречается ни в вопросе, ни в результатах инструментов. "
                "Сначала найдите нужную деталь через search_knowledge"
            )
    return None


def _order_stop(args: dict, question: str, steps: list[ToolStep], confirm: bool) -> AgentResult | None:
    """Остановка перед заказом: уточняющий вопрос, если количество не названо, или ожидание подтверждения."""
    if not _quantity_named(args["qty"], question):
        steps.append(ToolStep("create_service_order", args, error="количество не названо в вопросе"))
        text = f"Уточните, сколько штук детали {args['part_id']} нужно заказать: в вопросе количество не названо."
        return AgentResult(text, steps, _sources(steps))
    if not confirm:
        text = (
            f"Заказ подготовлен и ждёт подтверждения: деталь {args['part_id']}, количество {args['qty']}, "
            f"склад {args['warehouse']}, модель {args['model']}."
        )
        pending = {"tool": "create_service_order", "args": args}
        return AgentResult(text, steps, _sources(steps), needs_confirmation=True, pending_action=pending)
    return None


def _quantity_named(qty: int, question: str) -> bool:
    """Количество считается названным, если оно стоит в вопросе числом или словом, отдельно от кодов."""
    named = {int(_digits(number)) for number in re.findall(_NUMBER, question)}
    named |= {NUMBER_WORDS[word] for word in re.findall(r"[а-яё]+", question.lower()) if word in NUMBER_WORDS}
    return qty in named


def _execute(name: str, args: dict, tools: dict, steps: list[ToolStep]) -> dict:
    """Выполняет инструмент. Повторный вызов с теми же аргументами получает прежний успешный результат."""
    for step in steps:
        if step.tool == name and step.args == args and step.result is not None:
            return step.result
    return _with_retries(tools[name], args)


def _with_retries(tool: Callable[..., dict], args: dict) -> dict:
    """До трёх попыток, если ERP недоступна. После третьей неудачи возвращает словарь с error."""
    reason = ""
    for _ in range(ERP_ATTEMPTS):
        try:
            return tool(**args)
        except ErpUnavailable as exc:
            reason = str(exc)
    return {"error": f"{ERP_DOWN} после {ERP_ATTEMPTS} попыток: {reason}"}


def _known_codes(question: str, steps: list[ToolStep]) -> set[str]:
    """Коды из вопроса и результатов инструментов: только их можно передавать инструментам и называть в ответе."""
    return set(find_codes(question + " " + _results_text(steps)))


def _results_text(steps: list[ToolStep]) -> str:
    return " ".join(json.dumps(step.result, ensure_ascii=False) for step in steps if step.result is not None)


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw)


def _answer_problem(answer: str, question: str, steps: list[ToolStep]) -> str | None:
    """Почему ответ нельзя выдать: он пустой или в нём есть коды, цены и остатки, которых нет в источниках."""
    if not answer.strip():
        return "он пустой"
    known_codes = _known_codes(question, steps)
    known_numbers = {_digits(number) for number in re.findall(_NUMBER, question + " " + _results_text(steps))}
    invented = [code for code in find_codes(answer) if code not in known_codes]
    invented += [number for number in _PRICE_OR_QTY.findall(answer) if _digits(number) not in known_numbers]
    if invented:
        return "в нём есть данные, которых нет ни в вопросе, ни в результатах инструментов: " + ", ".join(invented)
    return None


def _correction_request(problem: str) -> str:
    return (
        f"Ответ не принят: {problem}. Перепиши его, используя только коды, цены и остатки "
        f"из результатов инструментов. Если нужных данных нет, напиши «{REFUSAL}»"
    )


def _final(answer: str, steps: list[ToolStep]) -> AgentResult:
    """Проверенный ответ. Номер созданного заказа и отказ ERP дописываются, если модель их не упомянула."""
    for step in steps:
        if step.tool == "create_service_order" and step.result is not None:
            order_id = step.result.get("order_id", "")
            if order_id and order_id not in answer:
                answer += f" Номер заказа: {order_id}."
    erp_down = any(step.error is not None and step.error.startswith(ERP_DOWN) for step in steps)
    if erp_down and "ERP" not in answer:
        answer += " Данные ERP получить не удалось."
    has_data = any(step.result is not None and not step.result.get("refused") for step in steps)
    refused = "не найден" in answer.lower() or not has_data
    return AgentResult(answer, steps, _sources(steps), refused=refused)


def _interrupted(steps: list[ToolStep], max_steps: int) -> AgentResult:
    """Лимит шагов исчерпан: агент возвращает то, что успел узнать, и сообщает, что работа прервана."""
    learned = []
    for step in steps:
        if step.result is not None:
            shown = step.result.get("answer") or ", ".join(f"{key}: {value}" for key, value in step.result.items())
            learned.append(f"{step.tool}: {shown}")
    text = f"Работа прервана: исчерпан лимит в {max_steps} вызовов инструментов."
    if learned:
        text += " Что удалось узнать: " + "; ".join(learned)
    else:
        text += " Данных получить не удалось."
    return AgentResult(text, steps, _sources(steps), refused=not learned)


def _sources(steps: list[ToolStep]) -> list[str]:
    """Документы-источники из результатов поиска, без повторов и в порядке появления."""
    sources: list[str] = []
    for step in steps:
        if step.result is None:
            continue
        for source in step.result.get("sources", []):
            if source not in sources:
                sources.append(source)
    return sources
