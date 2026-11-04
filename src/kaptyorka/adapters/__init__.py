from typing import Callable, List

from kaptyorka.adapters.base import Adapter
from kaptyorka.adapters.claude import ClaudeAdapter
from kaptyorka.adapters.codex import CodexAdapter
from kaptyorka.adapters.cursor import CursorAdapter
from kaptyorka.adapters.opencode import OpenCodeAdapter


def all_adapters(log: Callable[[str], None] = print) -> List[Adapter]:
    """Every known adapter, each constructed with log."""
    return [ClaudeAdapter(log=log), OpenCodeAdapter(log=log), CodexAdapter(log=log),
            CursorAdapter(log=log)]
