"""Concurrency modules for BareAgent."""

from bareagent.concurrency.background import BackgroundManager
from bareagent.concurrency.notification import inject_notifications

__all__ = ["BackgroundManager", "inject_notifications"]
