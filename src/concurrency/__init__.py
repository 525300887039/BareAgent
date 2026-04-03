"""Concurrency modules for BareAgent."""

from src.concurrency.background import BackgroundManager
from src.concurrency.notification import inject_notifications

__all__ = ["BackgroundManager", "inject_notifications"]
