"""
models.py -- sample Python domain models for E2E testing.

Covers Python query modes: classes, methods, fields, usings/imports,
decorators, calls, implements, ident.
"""
from __future__ import annotations

import json
import dataclasses
from abc import ABC, abstractmethod
from typing import Optional, List
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OrderStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class InventoryAction(Enum):
    ADD = "add"
    REMOVE = "remove"
    AUDIT = "audit"


# ---------------------------------------------------------------------------
# Data classes / models
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Product:
    id: int
    sku: str
    name: str
    quantity: int = 0

    def is_available(self) -> bool:
        return self.quantity > 0


@dataclasses.dataclass
class Order:
    id: str
    status: OrderStatus
    name: str
    items: List[Product] = dataclasses.field(default_factory=list)

    def process(self) -> None:
        self.status = OrderStatus.PROCESSING

    def cancel(self) -> None:
        self.status = OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

class IOrderRepository(ABC):
    """Repository interface for orders."""

    @abstractmethod
    def get_by_id(self, order_id: str) -> Optional[Order]: ...

    @abstractmethod
    def save(self, order: Order) -> None: ...

    @abstractmethod
    def delete(self, order_id: str) -> None: ...


class IInventoryService(ABC):
    """Service interface for inventory management."""

    @abstractmethod
    def add_item(self, sku: str) -> None: ...

    @abstractmethod
    def remove_item(self, sku: str) -> None: ...

    @abstractmethod
    def get_count(self, sku: str) -> int: ...


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

class InMemoryOrderRepository(IOrderRepository):
    """In-memory implementation for testing."""

    def __init__(self) -> None:
        self._store: dict[str, Order] = {}

    def get_by_id(self, order_id: str) -> Optional[Order]:
        return self._store.get(order_id)

    def save(self, order: Order) -> None:
        self._store[order.id] = order

    def delete(self, order_id: str) -> None:
        self._store.pop(order_id, None)


class InventoryManager(IInventoryService):
    """Manages product inventory."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def add_item(self, sku: str) -> None:
        self._counts[sku] = self._counts.get(sku, 0) + 1

    def remove_item(self, sku: str) -> None:
        count = self._counts.get(sku, 0)
        if count > 0:
            self._counts[sku] = count - 1

    def get_count(self, sku: str) -> int:
        return self._counts.get(sku, 0)

    def process_inventory(self, skus: List[str]) -> dict:
        return {sku: self.get_count(sku) for sku in skus}


# ---------------------------------------------------------------------------
# Order processing service
# ---------------------------------------------------------------------------

class OrderProcessingService:
    """Orchestrates order lifecycle."""

    def __init__(self, repo: IOrderRepository, inventory: IInventoryService) -> None:
        self._repo = repo
        self._inventory = inventory

    def place_order(self, order: Order) -> None:
        for item in order.items:
            count = self._inventory.get_count(item.sku)
            if count < item.quantity:
                raise ValueError(f"Insufficient stock for {item.sku}")
        order.process()
        self._repo.save(order)

    def cancel_order(self, order_id: str) -> bool:
        order = self._repo.get_by_id(order_id)
        if order is None:
            return False
        order.cancel()
        self._repo.save(order)
        return True

    def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        order = self._repo.get_by_id(order_id)
        return order.status if order else None
