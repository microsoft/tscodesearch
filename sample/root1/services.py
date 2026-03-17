"""
services.py -- sample Python services for E2E testing.

Covers Python query modes: classes, methods, fields, calls, implements,
uses, ident, imports, decorators/attrs.
"""
from __future__ import annotations

import os
import json
import logging
from abc import ABC, abstractmethod
from typing import Generic, TypeVar, Iterator, Optional

T = TypeVar("T")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Interfaces / abstract base classes
# ---------------------------------------------------------------------------

class IProcessor(ABC, Generic[T]):
    """Abstract processor interface."""

    @abstractmethod
    def process(self, item: T) -> T:
        """Process a single item."""
        ...

    @abstractmethod
    def reset(self) -> None:
        """Reset internal state."""
        ...


class ILogger(ABC):
    """Logging interface."""

    @abstractmethod
    def log(self, message: str) -> None: ...

    @abstractmethod
    def warn(self, message: str) -> None: ...


# ---------------------------------------------------------------------------
# Decorators (attrs mode)
# ---------------------------------------------------------------------------

def cacheable(ttl: int = 60):
    """Mark a class as cacheable with a given TTL."""
    def decorator(cls):
        cls._cacheable_ttl = ttl
        return cls
    return decorator


def retryable(attempts: int = 3):
    """Mark a method as retryable."""
    def decorator(fn):
        fn._retry_attempts = attempts
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Base processor
# ---------------------------------------------------------------------------

class BaseProcessor(IProcessor[T]):
    """Base implementation with logging."""

    def __init__(self, logger: ILogger) -> None:
        self._logger = logger

    def process(self, item: T) -> T:
        raise NotImplementedError

    def reset(self) -> None:
        self._logger.log("reset")

    def process_batch(self, items: list[T]) -> list[T]:
        return [self.process(i) for i in items]


# ---------------------------------------------------------------------------
# Concrete processor
# ---------------------------------------------------------------------------

@cacheable(ttl=120)
class TextProcessor(BaseProcessor[str]):
    """Processes text by applying a prefix."""

    prefix: str

    def __init__(self, prefix: str, logger: ILogger) -> None:
        super().__init__(logger)
        self.prefix = prefix

    def process(self, item: str) -> str:
        return self.prefix + item

    def format(self, item: str) -> str:
        result = "formatted"
        return result + item


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class ProcessorFactory:
    """Creates processor instances."""

    @staticmethod
    def create(prefix: str, logger: ILogger) -> TextProcessor:
        return TextProcessor(prefix, logger)

    @staticmethod
    def run(processor: IProcessor[str], input_: str) -> dict:
        output = processor.process(input_)
        return {"success": True, "output": output, "error_code": 0}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class ProcessingService:
    """Orchestrates processing tasks."""

    def __init__(self, processor: IProcessor[str], logger: ILogger) -> None:
        self._processor = processor
        self._logger = logger

    def do_work(self, input_: str) -> None:
        self._processor.process(input_)
        result = ProcessorFactory.run(self._processor, input_)

    def make_processor(self, prefix: str) -> TextProcessor:
        return ProcessorFactory.create(prefix, self._logger)

    @retryable(attempts=5)
    def transform(self, input_: str, max_length: int, trim: bool = False) -> str:
        s = input_
        if trim:
            s = s.strip()
        return s[:max_length] if len(s) > max_length else s

    def try_process(self, proc: IProcessor[str], value: str) -> Optional[str]:
        output = proc.process(value)
        return output if output else None
