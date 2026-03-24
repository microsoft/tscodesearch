"""
pipeline.py -- sample Python transform pipeline for E2E testing.

Covers Python query modes: classes, methods, calls, implements, declarations,
decorators, imports.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

class ITransformer(ABC):
    """Abstract transformer interface."""

    @abstractmethod
    def transform(self, value: Any) -> Any:
        """Apply transformation to a value."""
        ...

    @abstractmethod
    def supports(self, value: Any) -> bool:
        """Return True if this transformer handles the given value."""
        ...


class IValidator(ABC):
    """Validates values before transformation."""

    @abstractmethod
    def validate(self, value: Any) -> bool: ...


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def loggable(fn):
    """Mark a method for automatic logging."""
    fn._loggable = True
    return fn


def idempotent(fn):
    """Mark a transformation as safe to apply multiple times."""
    fn._idempotent = True
    return fn


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

class JsonTransformer(ITransformer):
    """Serialises values to JSON strings."""

    def __init__(self, indent: Optional[int] = None) -> None:
        self._indent = indent

    @loggable
    def transform(self, value: Any) -> str:
        return json.dumps(value, indent=self._indent)

    def supports(self, value: Any) -> bool:
        return True


class UpperTransformer(ITransformer):
    """Converts string values to upper case."""

    @idempotent
    def transform(self, value: Any) -> Any:
        return str(value).upper() if isinstance(value, str) else value

    def supports(self, value: Any) -> bool:
        return isinstance(value, str)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class TransformPipeline:
    """Chains multiple transformers in sequence."""

    def __init__(self) -> None:
        self._steps: list[ITransformer] = []

    def add_step(self, transformer: ITransformer) -> "TransformPipeline":
        self._steps.append(transformer)
        return self

    @loggable
    def run(self, value: Any) -> Any:
        result = value
        for step in self._steps:
            if step.supports(result):
                result = step.transform(result)
        return result

    def run_all(self, values: list[Any]) -> list[Any]:
        return [self.run(v) for v in values]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class PipelineFactory:
    """Creates pre-configured TransformPipeline instances."""

    @staticmethod
    def json_pipeline(indent: int = 2) -> TransformPipeline:
        p = TransformPipeline()
        p.add_step(JsonTransformer(indent=indent))
        return p

    @staticmethod
    def upper_json_pipeline() -> TransformPipeline:
        p = TransformPipeline()
        p.add_step(UpperTransformer())
        p.add_step(JsonTransformer())
        return p
