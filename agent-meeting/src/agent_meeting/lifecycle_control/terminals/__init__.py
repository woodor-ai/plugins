"""Terminal control adapters used by ``am-ctld``."""

from .base import TerminalAdapter, TerminalCapabilities
from .detection import current_terminal_handle
from .mac_iterm2 import Iterm2TerminalAdapter
from .tmux import TmuxTerminalAdapter
from .unsupported import UnsupportedTerminalAdapter
from .wrapper import WrapperTerminalAdapter


def adapter_for_handle(handle: dict) -> TerminalAdapter:
    terminal_type = (handle or {}).get("type")
    if terminal_type == "tmux":
        return TmuxTerminalAdapter()
    if terminal_type == "iterm2":
        return Iterm2TerminalAdapter()
    return UnsupportedTerminalAdapter()

__all__ = [
    "TerminalAdapter",
    "TerminalCapabilities",
    "Iterm2TerminalAdapter",
    "TmuxTerminalAdapter",
    "UnsupportedTerminalAdapter",
    "WrapperTerminalAdapter",
    "adapter_for_handle",
    "current_terminal_handle",
]
