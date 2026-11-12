"""Дополнительные сценарии FR-7 – FR-10: приведение аргументов, лимит шагов, дописывание фактов в ответ."""

from assistant.agent.agent import run_agent
from assistant.infra.erp_client import ErpUnavailable
from assistant.infra.llm import LLMResponse, ToolCall
from assistant.infra.testing import FakeLLM

SEARCH = {
    "answer": "E-17 означает отказ пускового реле. В AR-210 это P-1042, она заменена на P-1042B.",
    "facts": ["Part:P-1042 -REPLACED_BY-> Part:P-1042B"],
    "sources": ["manual_AR-210.md", "bulletin_SB-2025-03.md"],
    "refused": False,
}
RESULTS = {
    "search_knowledge": SEARCH,
    "summarize_topic": {"answer": "Чаще всего отказывает реле.", "sources": ["manual_AR-210.md"], "refused": False},
    "check_stock": {"part_id": "P-1042B", "warehouse": "Казань", "qty": 3, "discontinued": False},
    "get_price": {"part_id": "P-1042B", "name": "пусковое реле", "price_rub": 2630, "discontinued": False},
    "create_service_order": {"order_id": "SO-000001", "status": "created"},
}
ORDER = {"part_id": "P-1042B", "warehouse": "Казань", "qty": 2, "model": "AR-210"}


def call(name: str, **arguments) -> LLMResponse:
    return LLMResponse("", [ToolCall(f"call-{name}", name, arguments)])


class Tools:
    """Подставные инструменты: запоминают вызовы и отдают заранее заданные результаты."""

    def __init__(self, **overrides) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.results = {**RESULTS, **overrides}

    def as_dict(self) -> dict:
        return {name: self.make(name) for name in RESULTS}

    def make(self, name: str):
        def tool(**arguments) -> dict:
            self.calls.append((name, arguments))
            result = self.results[name]
            return result(**arguments) if callable(result) else result

        return tool


def test_arguments_are_normalized_before_checks_and_recording():
    tools = Tools()
    llm = FakeLLM([call("check_stock", part_id="p-1042b", warehouse="в Казани"), "В Казани 3 шт."])
    result = run_agent("Есть ли P-1042B на складе в Казани?", llm, tools.as_dict())
    assert tools.calls == [("check_stock", {"part_id": "P-1042B", "warehouse": "Казань"})]
    assert result.steps[0].args == {"part_id": "P-1042B", "warehouse": "Казань"} and result.steps[0].error is None


def test_quantity_named_in_words_counts():
    tools = Tools()
    llm = FakeLLM([call("create_service_order", **{**ORDER, "qty": "2"}), "Жду подтверждения."])
    result = run_agent("Закажи две штуки P-1042B для AR-210, склад Казань.", llm, tools.as_dict())
    assert result.needs_confirmation and result.pending_action["args"] == ORDER


def test_model_code_outside_question_and_results_is_rejected():
    tools = Tools()
    llm = FakeLLM([call("create_service_order", **{**ORDER, "model": "GS-999"}), "Не знаю модель."])
    result = run_agent("Закажи 2 штуки P-1042B на склад Казань.", llm, tools.as_dict(), confirm=True)
    assert tools.calls == [] and "GS-999" in result.steps[0].error and not result.needs_confirmation


def test_missing_required_argument_is_an_error():
    tools = Tools()
    llm = FakeLLM([call("check_stock", part_id="P-1042B"), "Не хватает склада."])
    result = run_agent("Есть ли P-1042B?", llm, tools.as_dict())
    assert tools.calls == [] and "warehouse" in result.steps[0].error


def test_part_number_becomes_allowed_after_search_reveals_it():
    tools = Tools()
    llm = FakeLLM(
        [
            call("get_price", part_id="P-1042B"),
            call("search_knowledge", question="Какая деталь пускового реле в AR-210?"),
            call("get_price", part_id="P-1042B"),
            "P-1042B стоит 2630 руб.",
        ]
    )
    result = run_agent("Сколько стоит пусковое реле для AR-210?", llm, tools.as_dict())
    assert [step.tool for step in result.steps] == ["get_price", "search_knowledge", "get_price"]
    assert result.steps[0].error and result.steps[2].error is None and result.answer == "P-1042B стоит 2630 руб."


def test_interrupted_answer_lists_what_was_learned():
    tools = Tools()
    llm = FakeLLM([call("check_stock", part_id="P-1042B", warehouse="Казань"), call("get_price", part_id="P-1042B")])
    result = run_agent("Где есть P-1042B и почём?", llm, tools.as_dict(), max_steps=1)
    assert tools.calls == [("check_stock", {"part_id": "P-1042B", "warehouse": "Казань"})]
    assert "прервана" in result.answer and "qty: 3" in result.answer and not result.refused


def test_order_number_is_added_when_the_model_omits_it():
    tools = Tools()
    llm = FakeLLM([call("create_service_order", **ORDER), "Заказ создан."])
    result = run_agent("Оформи заказ: 2 штуки P-1042B для AR-210, склад Казань.", llm, tools.as_dict(), confirm=True)
    assert result.answer == "Заказ создан. Номер заказа: SO-000001."


def test_erp_failure_is_mentioned_when_the_model_stays_silent():
    def dead(**arguments) -> dict:
        raise ErpUnavailable("ERP недоступна")

    tools = Tools(get_price=dead)
    llm = FakeLLM([call("get_price", part_id="P-1042B"), "Не знаю."])
    result = run_agent("Сколько стоит P-1042B?", llm, tools.as_dict())
    assert result.steps[0].error.startswith("ERP не ответила") and "ERP" in result.answer and result.refused


def test_erp_error_is_recorded_as_step_error_and_shown_to_the_model():
    tools = Tools(get_price={"error": "данных нет: деталь P-9999 не найдена"})
    llm = FakeLLM([call("get_price", part_id="P-9999"), "Деталь P-9999 в ERP не найдена."])
    result = run_agent("Сколько стоит P-9999?", llm, tools.as_dict())
    assert result.steps[0].result is None and "данных нет" in result.steps[0].error
    assert "данных нет" in llm.requests[1]["messages"][-1]["content"]
    assert result.refused and result.answer == "Деталь P-9999 в ERP не найдена."


def test_answer_without_any_tool_data_is_a_refusal():
    tools = Tools()
    result = run_agent("Сколько стоит P-1042B?", FakeLLM(["P-1042B стоит недорого."]), tools.as_dict())
    assert result.refused and result.steps == []


def test_empty_answer_is_sent_back_once():
    tools = Tools()
    llm = FakeLLM([call("get_price", part_id="P-1042B"), "", "Цена 2630 руб."])
    result = run_agent("Сколько стоит P-1042B?", llm, tools.as_dict())
    assert len(llm.requests) == 3 and result.answer == "Цена 2630 руб." and not result.refused


def test_sources_are_collected_from_every_search_without_duplicates():
    tools = Tools()
    llm = FakeLLM(
        [
            call("search_knowledge", question="Какая деталь при E-17 на AR-210?"),
            call("summarize_topic", question="Что отказывает чаще всего?"),
            "P-1042B.",
        ]
    )
    result = run_agent("Какая деталь при E-17 на AR-210 и что ломается чаще всего?", llm, tools.as_dict())
    assert result.sources == ["manual_AR-210.md", "bulletin_SB-2025-03.md"]


def test_price_with_thousands_separator_matches_the_tool_result():
    tools = Tools()
    llm = FakeLLM([call("get_price", part_id="P-1042B"), "Цена 2 630 руб."])
    result = run_agent("Сколько стоит P-1042B?", llm, tools.as_dict())
    assert len(llm.requests) == 2 and result.answer == "Цена 2 630 руб."
