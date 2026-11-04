from typing import Callable, List

from kaptyorka.adapters.base import Adapter
from kaptyorka.adapters.claude import ClaudeAdapter


def all_adapters(log: Callable[[str], None] = print) -> List[Adapter]:
    """Every known adapter, each constructed with log."""
    return [ClaudeAdapter(log=log)]
