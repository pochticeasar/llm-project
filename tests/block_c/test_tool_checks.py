"""Дополнительные сценарии FR-6: проверка аргументов, приведение кодов и обёртки над поиском блока Б."""

import pytest

from assistant.agent.tools import build_tools, canonical_code, validate_arguments
from assistant.graphrag import search
from assistant.infra.erp_client import ErpUnavailable
from assistant.infra.schemas import Answer
from assistant.infra.testing import FakeLLM


def test_canonical_code_accepts_sloppy_spelling_and_checks_the_kind():
    assert canonical_code("p-1042b", "Part") == "P-1042B"
    assert canonical_code("ar 210", "Model") == "AR-210"
    assert canonical_code("AR-210", "Part") is None
    assert canonical_code("реле", "Part") is None


@pytest.mark.parametrize(
    ("name", "args", "fragment"),
    [
        ("get_price", {}, "не хватает"),
        ("check_stock", {"part_id": "P-1042B", "warehouse": "Казань", "extra": 1}, "лишние"),
        ("search_knowledge", {"question": "  "}, "пустой"),
        ("create_service_order", {"part_id": "P-1042B", "warehouse": "Казань", "qty": "2", "model": "AR-210"}, "целым"),
        ("create_service_order", {"part_id": "P-1042B", "warehouse": "Казань", "qty": 2, "model": "реле"}, "модели"),
        ("teleport", {}, "неизвестный инструмент"),
    ],
)
def test_validate_arguments_explains_what_is_wrong(name, args, fragment):
    assert fragment in validate_arguments(name, args)


def test_valid_arguments_pass():
    assert validate_arguments("check_stock", {"part_id": "p-1042b", "warehouse": "Казань"}) is None


def test_part_number_is_normalized_before_the_erp_call(kb, erp):
    tools = build_tools(kb, FakeLLM([]), erp)
    assert tools["check_stock"](part_id="p-1042b", warehouse="Казань")["qty"] == 3
    assert tools["get_price"](part_id="P 1042B")["price_rub"] == 2630
    assert erp.calls == [("get_stock", "P-1042B", "Казань"), ("get_price", "P-1042B")]


def test_not_found_and_rejected_have_different_error_texts(kb, erp):
    tools = build_tools(kb, FakeLLM([]), erp)
    missing = tools["get_price"](part_id="P-9999")
    rejected = tools["create_service_order"](part_id="P-1042", warehouse="Казань", qty=1, model="AR-210")
    assert "данных нет" in missing["error"] and "отклонила" in rejected["error"]
    assert missing["error"] != rejected["error"]


def test_unavailable_erp_is_passed_to_the_agent(kb):
    class DeadErp:
        def get_price(self, part_id):
            raise ErpUnavailable("ERP недоступна")

    with pytest.raises(ErpUnavailable):
        build_tools(kb, FakeLLM([]), DeadErp())["get_price"](part_id="P-1042B")


def test_search_tools_wrap_answers_of_block_b(kb, erp, monkeypatch):
    local = Answer("P-1042 заменена на P-1042B.", ["bulletin_SB-2025-03.md"], ["P-1042 -REPLACED_BY-> P-1042B"])
    overview = Answer("Чаще всего отказывает пусковое реле.", ["report_R-2025-001.md"])
    monkeypatch.setattr(search, "local_search", lambda question, kb, llm: local)
    monkeypatch.setattr(search, "global_search", lambda question, kb, llm: overview)
    tools = build_tools(kb, FakeLLM([]), erp)
    assert tools["search_knowledge"](question="Чем заменена P-1042?") == {
        "answer": local.text,
        "facts": local.facts,
        "sources": local.sources,
        "refused": False,
    }
    assert tools["summarize_topic"](question="Что ломается чаще всего?") == {
        "answer": overview.text,
        "sources": overview.sources,
        "refused": False,
    }


def test_empty_question_is_rejected_without_search(kb, erp, monkeypatch):
    def unexpected(question, kb, llm):
        raise AssertionError("поиск не должен вызываться")

    monkeypatch.setattr(search, "local_search", unexpected)
    assert "error" in build_tools(kb, FakeLLM([]), erp)["search_knowledge"](question="")
