"""
notifier.py -- sample Python notification service for E2E testing.

Covers Python query modes: classes, methods, calls, implements, declarations,
decorators, imports.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class NotificationChannel(Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------

class IEventSink(ABC):
    """Receives and dispatches a single notification event."""

    @abstractmethod
    def send(self, recipient: str, subject: str, body: str) -> bool: ...

    @abstractmethod
    def channel(self) -> NotificationChannel: ...


class INotificationRouter(ABC):
    """Routes events to the correct sink."""

    @abstractmethod
    def route(self, channel: NotificationChannel, recipient: str,
              subject: str, body: str) -> bool: ...


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def audited(fn):
    """Mark a dispatch method for audit logging."""
    fn._audited = True
    return fn


def throttled(limit: int = 100):
    """Rate-limit a method to at most `limit` calls per minute."""
    def decorator(fn):
        fn._throttle_limit = limit
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Sink implementations
# ---------------------------------------------------------------------------

class EmailSink(IEventSink):
    """Delivers notifications via email."""

    def __init__(self, smtp_host: str, port: int = 587) -> None:
        self._smtp_host = smtp_host
        self._port = port

    @throttled(limit=200)
    def send(self, recipient: str, subject: str, body: str) -> bool:
        logger.info("email -> %s: %s", recipient, subject)
        return True

    def channel(self) -> NotificationChannel:
        return NotificationChannel.EMAIL


class SmsSink(IEventSink):
    """Delivers notifications via SMS."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    @throttled(limit=50)
    def send(self, recipient: str, subject: str, body: str) -> bool:
        logger.info("sms -> %s", recipient)
        return True

    def channel(self) -> NotificationChannel:
        return NotificationChannel.SMS


# ---------------------------------------------------------------------------
# Router / service
# ---------------------------------------------------------------------------

class NotificationService(INotificationRouter):
    """Dispatches events to registered sinks."""

    def __init__(self) -> None:
        self._sinks: dict[NotificationChannel, IEventSink] = {}

    def register(self, sink: IEventSink) -> None:
        self._sinks[sink.channel()] = sink

    @audited
    def route(self, channel: NotificationChannel, recipient: str,
              subject: str, body: str) -> bool:
        sink = self._sinks.get(channel)
        if sink is None:
            logger.warning("no sink for channel %s", channel)
            return False
        return sink.send(recipient, subject, body)

    def broadcast(self, subject: str, body: str,
                  recipients: list[tuple[NotificationChannel, str]]) -> dict:
        results = {}
        for ch, addr in recipients:
            results[addr] = self.route(ch, addr, subject, body)
        return results


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class SinkFactory:
    """Creates pre-configured IEventSink instances."""

    @staticmethod
    def email(smtp_host: str) -> EmailSink:
        return EmailSink(smtp_host)

    @staticmethod
    def sms(api_key: str) -> SmsSink:
        return SmsSink(api_key)

    @staticmethod
    def default_service(smtp_host: str, sms_key: str) -> NotificationService:
        svc = NotificationService()
        svc.register(SinkFactory.email(smtp_host))
        svc.register(SinkFactory.sms(sms_key))
        return svc
