"""Stable terminal capability contract for lifecycle actions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalCapabilities:
    can_send_text: bool = False
    can_interrupt: bool = False
    can_restart_in_place: bool = False
    can_resolve_window: bool = False
    requires_user_permission: bool = False


class TerminalAdapter(ABC):
    @abstractmethod
    def capabilities(self, handle: dict) -> TerminalCapabilities:
        raise NotImplementedError

    def send_text(self, handle: dict, text: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def send_interrupt(self, handle: dict, count: int = 2) -> bool:
        raise NotImplementedError

    @abstractmethod
    def restart_in_place(self, handle: dict) -> bool:
        raise NotImplementedError
