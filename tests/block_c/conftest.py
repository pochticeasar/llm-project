"""Заготовки для тестов блока В: подставная ERP с одной деталью и пустая база знаний."""

import networkx as nx
import pytest

from assistant.infra.erp_client import ErpNotFound, ErpRejected
from assistant.infra.schemas import KnowledgeBase


class FakeErp:
    """Знает только деталь P-1042B. Остальные детали не найдены, их заказ ERP отклоняет."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_price(self, part_id: str) -> dict:
        self.calls.append(("get_price", part_id))
        if part_id != "P-1042B":
            raise ErpNotFound(f"деталь {part_id} не найдена")
        return {"part_id": part_id, "name": "пусковое реле", "price_rub": 2630, "discontinued": False}

    def get_stock(self, part_id: str, warehouse: str) -> dict:
        self.calls.append(("get_stock", part_id, warehouse))
        if part_id != "P-1042B":
            raise ErpNotFound(f"деталь {part_id} не найдена")
        return {"part_id": part_id, "warehouse": warehouse, "qty": 3, "discontinued": False}

    def create_order(self, part_id: str, warehouse: str, qty: int, model: str) -> dict:
        self.calls.append(("create_order", part_id, warehouse, qty, model))
        if part_id != "P-1042B":
            raise ErpRejected(f"деталь {part_id} снята с производства, заказ невозможен")
        return {"order_id": "SO-000001", "part_id": part_id, "warehouse": warehouse, "qty": qty, "model": model}


@pytest.fixture()
def erp() -> FakeErp:
    return FakeErp()


@pytest.fixture()
def kb() -> KnowledgeBase:
    return KnowledgeBase(nx.DiGraph(), [], {})
