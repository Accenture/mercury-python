"""Registry pins: the PreLoad route list - one function under several routes (aliases)."""

import pytest

from mercury_composable import Body, FunctionRegistry, PostOffice
from mercury_composable import registry as registry_module
from mercury_composable.registry import split_routes
from mercury_composable.trace import get_trace


def test_split_routes_takes_one_name_or_a_comma_separated_list():
    assert split_routes("hello.python") == ["hello.python"]
    assert split_routes("hello.declarative, hello.world") == ["hello.declarative", "hello.world"]
    assert split_routes("a.b,c.d") == ["a.b", "c.d"]
    assert split_routes(" a.b , c.d ,  e.f ") == ["a.b", "c.d", "e.f"]
    # the Java engine splits on "[, ]": a space alone separates, empty segments are dropped
    assert split_routes("a.b c.d") == ["a.b", "c.d"]
    assert split_routes("a.b,,c.d,") == ["a.b", "c.d"]
    # a repeated name is one registration
    assert split_routes("a.b, a.b") == ["a.b"]


@pytest.mark.parametrize("value", ["", "   ", ",", " , "])
def test_split_routes_refuses_a_list_without_a_name(value: str):
    with pytest.raises(ValueError, match="missing service route"):
        split_routes(value)


def test_split_routes_validates_every_name():
    with pytest.raises(ValueError, match="Invalid route name 'HelloWorld'"):
        split_routes("hello.world, HelloWorld")


def test_register_takes_one_route():
    # the list is the decorator's (the engines' PreLoad); Platform.register takes one route
    def noop(_headers: dict[str, str], _body: Body) -> None:
        return None

    with pytest.raises(ValueError, match="Invalid route name"):
        FunctionRegistry().register("a.b, c.d", noop)


async def test_preload_registers_every_route_as_the_same_function(monkeypatch: pytest.MonkeyPatch):
    fresh = FunctionRegistry()
    monkeypatch.setattr(registry_module, "default_registry", fresh)
    try:
        @registry_module.preload(route="alias.one, alias.two", instances=3, private=True)
        async def echo(_headers: dict[str, str], body: Body):
            info = get_trace()
            return {"body": body, "route": info.route if info else None}

        one, two = fresh.get("alias.one"), fresh.get("alias.two")
        assert one is not None and two is not None
        assert one.handler is echo and two.handler is echo
        assert (one.instances, one.private, one.is_async) == (3, True, True)
        assert (two.instances, two.private, two.is_async) == (3, True, True)
        assert set(fresh.routes()) == {"alias.one", "alias.two"}

        po = PostOffice(registry=fresh)
        for route in ("alias.one", "alias.two"):
            reply = await po.request(route, body={"n": 1}, timeout_ms=5000)
            assert reply.get_status() == 200
            # the function sees the alias it was called as (the engines' my_route)
            assert reply.body == {"body": {"n": 1}, "route": route}
            assert reply.sender == route
    finally:
        await fresh.bus.close()


def test_preload_refuses_an_empty_route_list_at_decoration(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(registry_module, "default_registry", FunctionRegistry())
    with pytest.raises(ValueError, match="missing service route"):
        registry_module.preload(route=" , ")
