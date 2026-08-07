"""Read-only adapter seams for dependency-free source ingestion."""

from .gmail import GmailPreview, GmailReadAdapter
from .telegram_web import TelegramWebPreview, TelegramWebReadAdapter

__all__ = [
    "GmailPreview",
    "GmailReadAdapter",
    "TelegramWebPreview",
    "TelegramWebReadAdapter",
]
