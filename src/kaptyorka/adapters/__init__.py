from typing import Callable, List

from kaptyorka.adapters.base import Adapter


def all_adapters(log: Callable[[str], None] = print) -> List[Adapter]:
    """Every known adapter, each constructed with log."""
    return []
