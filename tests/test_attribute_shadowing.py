"""Guard against instance attributes shadowing class methods.

Setting `self.<name>` in __init__ where <name> is also a method makes every
`self.<name>(...)` call resolve to the attribute instead — `TypeError:
'NoneType' object is not callable` at best. The LocalClient._cli_logged_in
bug is the canonical example.
"""

import inspect

import pytest

from app.clients import DevinClient, TelegramClient
from app.local import LocalClient


def _clients() -> list[object]:
    return [
        LocalClient(),
        DevinClient(api_key="k", base_url="https://x", max_acu_limit=10),
        TelegramClient(bot_token="t"),
    ]


@pytest.mark.parametrize("index", range(3), ids=["local", "devin", "telegram"])
def test_no_instance_attribute_shadows_a_method(index: int) -> None:
    instance = _clients()[index]
    methods = {
        name
        for name, member in inspect.getmembers(type(instance))
        if inspect.isfunction(member) or inspect.ismethod(member)
    }
    shadowed = methods & set(vars(instance))
    assert not shadowed, f"instance attributes shadow methods: {shadowed}"
