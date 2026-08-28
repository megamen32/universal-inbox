"""Read-only adapter seams for dependency-free source ingestion."""

from .gmail import GmailPreview, GmailReadAdapter
from .overpod_telegram import OverpodTelegramIpcClient, OverpodTelegramIpcReader
from .telegram_mcp import TelegramMcpPreview, TelegramMcpReadAdapter

__all__ = [
    "GmailPreview",
    "GmailReadAdapter",
    "OverpodTelegramIpcClient",
    "OverpodTelegramIpcReader",
    "TelegramMcpPreview",
    "TelegramMcpReadAdapter",
]
