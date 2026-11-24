"""Проверяемый вывод нескольких остатков и цен из структурированных результатов ERP."""

import operator
import re

from assistant.infra.schemas import ToolStep

_NUMBER_FORMS = {
    0: "ноль нуля нулю нулём нулем нуле",
    1: "один одна одно одну одного одной одному одним",
    2: "два две двух двум двумя",
    3: "три трёх трех трём трем тремя",
    4: "четыре четырёх четырех четырём четырем четырьмя",
    5: "пять пяти пятью",
    6: "шесть шести шестью",
    7: "семь семи семью",
    8: "восемь восьми восемью восьмью",
    9: "девять девяти девятью",
    10: "десять десяти десятью",
    11: "одиннадцать одиннадцати одиннадцатью",
    12: "двенадцать двенадцати двенадцатью",
    13: "тринадцать тринадцати тринадцатью",
    14: "четырнадцать четырнадцати четырнадцатью",
    15: "пятнадцать пятнадцати пятнадцатью",
    16: "шестнадцать шестнадцати шестнадцатью",
    17: "семнадцать семнадцати семнадцатью",
    18: "восемнадцать восемнадцати восемнадцатью",
    19: "девятнадцать девятнадцати девятнадцатью",
    20: "двадцать двадцати двадцатью",
    30: "тридцать тридцати тридцатью",
    40: "сорок сорока",
    50: "пятьдесят пятидесяти пятьюдесятью",
    60: "шестьдесят шестидесяти шестьюдесятью",
    70: "семьдесят семидесяти семьюдесятью",
    80: "восемьдесят восьмидесяти восемьюдесятью",
    90: "девяносто девяноста",
}
_WORD_VALUES = {word: value for value, forms in _NUMBER_FORMS.items() for word in forms.split()}
_WORD = "(?:" + "|".join(sorted(_WORD_VALUES, key=len, reverse=True)) + ")"
_WORDS = re.compile(rf"\b{_WORD}(?:\s+{_WORD})*\b", re.IGNORECASE)
_COMPARISONS = {
    "не меньше": operator.ge, "не менее": operator.ge, "хотя бы": operator.ge, "минимум": operator.ge,
    "не больше": operator.le, "не более": operator.le,
    "меньше": operator.lt, "менее": operator.lt, "больше": operator.gt, "более": operator.gt,
}
_THRESHOLD = re.compile(r"\b(" + "|".join(_COMPARISONS) + r")\s+(\d+)\b")


def normalize_number_words(text: str) -> str:
    """Числительные до 99, включая падежи; составное «двадцати трёх» превращается в 23."""
    def replace(match: re.Match) -> str:
        values = [_WORD_VALUES[word.lower()] for word in match.group().split()]
        if len(values) == 1:
            return str(values[0])
        if len(values) == 2 and values[0] >= 20 and 0 < values[1] < 10:
            return str(sum(values))
        return match.group()

    return _WORDS.sub(replace, text)


def verified_erp_answer(question: str, steps: list[ToolStep]) -> str | None:
    """Для нескольких записей ERP выводит сами записи: пересказ модели не используется."""
    stock: dict[tuple[str, str], int] = {}
    prices: dict[str, int] = {}
    discontinued: set[str] = set()
    errors = []
    for step in steps:
        if step.tool not in {"check_stock", "get_price"}:
            continue
        if step.error:
            label = ", ".join(str(step.args[key]) for key in ("part_id", "warehouse") if key in step.args)
            errors.append(f"{label}: {step.error}")
            continue
        data = step.result
        if not data:
            continue
        part = data.get("part_id", step.args.get("part_id"))
        if data.get("discontinued"):
            discontinued.add(part)
        if step.tool == "check_stock" and type(data.get("qty")) is int:
            stock[(part, data.get("warehouse", step.args.get("warehouse")))] = data["qty"]
        if step.tool == "get_price" and type(data.get("price_rub")) is int:
            prices[part] = data["price_rub"]
    parts = {part for part, _ in stock} | set(prices)
    if len(stock) < 2 and len(prices) < 2 and len(parts) < 2:
        return None
    lines = ["Данные ERP по проверенным деталям и складам:"]
    lines += [f"{part}, {warehouse}: {qty} шт." for (part, warehouse), qty in stock.items()]
    lines += [f"{part}: цена {price} руб." for part, price in prices.items()]
    lines += [f"{part}: снята с производства." for part in sorted(discontinued)]
    text = normalize_number_words(question.lower())
    thresholds = list(_THRESHOLD.finditer(text))
    if len(thresholds) == 1 and len({part for part, _ in stock}) == 1:
        match = thresholds[0]
        comparison, raw_limit = match.groups()
        # A monetary constraint or a range is not a stock threshold.
        suffix = text[match.end():].lstrip()
        if not re.match(r"(?:руб|₽|до\b)", suffix):
            selected = [warehouse for (_, warehouse), qty in stock.items()
                        if _COMPARISONS[comparison](qty, int(raw_limit))]
            lines.append(f"Условию «{comparison} {raw_limit} шт.» среди проверенных складов соответствуют: "
                         + (", ".join(selected) if selected else "ни один") + ".")
    lines += ["Не удалось получить данные ERP: " + error for error in errors]
    return "\n".join(lines)
