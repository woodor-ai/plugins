"""Explicit fail-closed adapters for terminal surfaces we cannot own."""

from __future__ import annotations

from .base import TerminalAdapter, TerminalCapabilities


class UnsupportedTerminalAdapter(TerminalAdapter):
    def capabilities(self, handle: dict) -> TerminalCapabilities:
        return TerminalCapabilities()

    def send_text(self, handle: dict, text: str) -> bool:
        return False

    def send_interrupt(self, handle: dict, count: int = 2) -> bool:
        return False

    def restart_in_place(self, handle: dict) -> bool:
        return False
