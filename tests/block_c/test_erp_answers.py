"""Ответы по нескольким записям ERP сохраняют принадлежность чисел детали и складу."""

import pytest

from assistant.agent.agent import _answer_problem, run_agent
from assistant.agent.erp_answers import normalize_number_words, verified_erp_answer
from assistant.infra.llm import LLMResponse, ToolCall
from assistant.infra.schemas import ToolStep
from assistant.infra.testing import FakeLLM

STOCK = {"Москва": 0, "Санкт-Петербург": 8, "Казань": 15, "Новосибирск": 6}


def stock_step(warehouse: str, qty: int, part: str = "P-2122") -> ToolStep:
    args = {"part_id": part, "warehouse": warehouse}
    return ToolStep("check_stock", args, result={**args, "qty": qty, "discontinued": False})


@pytest.mark.parametrize("word,number", [("трех", "3"), ("трёх", "3"), ("десяти", "10"),
                                       ("двумя", "2"), ("двадцати трёх", "23"), ("пятидесяти", "50")])
def test_number_words_are_normalized_in_context(word, number):
    assert normalize_number_words(f"Не меньше {word} штук P-2122") == f"Не меньше {number} штук P-2122"


def test_an_ambiguous_sequence_of_numbers_is_not_summed():
    assert normalize_number_words("две три штуки") == "две три штуки"


@pytest.mark.parametrize("bad_answer", [
    "В Москве и Новосибирске остаток 0, в Санкт-Петербурге — 8 шт., в Казани — 15 шт.",
    "В Новосибирске P-2122: 0 шт., в Москве: 6 шт.",
    "В Санкт-Петербурге — 15 шт., в Казани — 8 шт.",
])
def test_multiwarehouse_answer_is_rendered_from_erp_even_if_model_swaps_quantities(bad_answer):
    calls = []

    def check_stock(part_id, warehouse):
        calls.append((part_id, warehouse))
        return {"part_id": part_id, "warehouse": warehouse, "qty": STOCK[warehouse], "discontinued": False}

    llm = FakeLLM([
        *[LLMResponse("", [ToolCall(str(i), "check_stock", {"part_id": "P-2122", "warehouse": warehouse})])
          for i, warehouse in enumerate(STOCK)],
        bad_answer,
    ])
    result = run_agent(
        "На каких складах можно получить не меньше трех P-2122? Проверь все склады, заказ не создавай.",
        llm, {"check_stock": check_stock},
    )
    assert not result.refused and not result.needs_confirmation
    assert calls == [("P-2122", warehouse) for warehouse in STOCK]
    assert "P-2122, Новосибирск: 6 шт." in result.answer
    assert "P-2122, Москва: 0 шт." in result.answer
    assert "соответствуют: Санкт-Петербург, Казань, Новосибирск." in result.answer
    assert len(llm.requests) == 5


@pytest.mark.parametrize("words,expected", [
    ("десяти", "Казань"), ("трех", "Санкт-Петербург, Казань, Новосибирск"), ("двадцати", "ни один"),
])
def test_minimum_stock_filters_actual_values_including_thresholds_written_as_words(words, expected):
    steps = [stock_step(warehouse, qty) for warehouse, qty in STOCK.items()]
    question = f"Где есть не меньше {words} P-2122?"
    answer = verified_erp_answer(question, steps)
    assert answer.endswith(f"соответствуют: {expected}.")
    assert _answer_problem(answer, question, steps) is None


def test_threshold_from_question_is_allowed_when_model_writes_it_with_digits():
    assert _answer_problem(
        "В Казани не меньше 10 шт. P-2122: доступно 15 шт.",
        "Где есть не меньше десяти P-2122?", [stock_step("Казань", 15)],
    ) is None


def test_same_warehouse_for_two_parts_does_not_mix_their_quantities():
    answer = verified_erp_answer("Проверь обе детали в Москве.", [
        stock_step("Москва", 0, "P-2122"), stock_step("Москва", 9, "P-3018"),
    ])
    assert "P-2122, Москва: 0 шт." in answer
    assert "P-3018, Москва: 9 шт." in answer


def test_prices_are_bound_to_their_parts():
    steps = [ToolStep("get_price", {"part_id": part}, result={"part_id": part, "price_rub": price})
             for part, price in [("P-2114", 10020), ("P-3014", 8130)]]
    answer = verified_erp_answer("Сравни цены обеих деталей.", steps)
    assert "P-2114: цена 10020 руб." in answer
    assert "P-3014: цена 8130 руб." in answer


def test_failed_warehouse_lookup_is_reported_instead_of_turning_into_zero_stock():
    answer = verified_erp_answer("Где есть не меньше трех P-2122?", [
        stock_step("Москва", 0), stock_step("Казань", 15),
        ToolStep("check_stock", {"part_id": "P-2122", "warehouse": "Новосибирск"}, error="ERP не ответила"),
    ])
    assert "среди проверенных складов соответствуют: Казань." in answer
    assert "Не удалось получить данные ERP" in answer and "Новосибирск" in answer
    assert "Новосибирск: 0" not in answer


def test_repeated_observation_uses_the_latest_result_for_the_same_part_and_warehouse():
    answer = verified_erp_answer("Проверь остатки.", [
        stock_step("Москва", 4), stock_step("Казань", 7), stock_step("Москва", 2),
    ])
    assert "P-2122, Москва: 2 шт." in answer and "Москва: 4" not in answer


def test_monetary_threshold_is_not_used_to_filter_stock():
    answer = verified_erp_answer("С бюджетом не менее 100 рублей проверь остатки.", [
        stock_step("Москва", 4), stock_step("Казань", 7),
    ])
    assert "Условию" not in answer
