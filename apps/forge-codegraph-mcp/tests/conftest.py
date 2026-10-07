import hashlib
from dataclasses import dataclass
from typing import Any

import pytest
from memstore import MemStore
from pydantic_settings import SettingsConfigDict

from forge_codegraph_mcp.core.settings import Environment, Settings
from forge_codegraph_mcp.graph.model import (
    EDGE_CALLS,
    EDGE_CONTAINS,
    EDGE_OVERRIDES,
    EDGE_USES_TYPE,
    graph_id,
)

#: The shop's repository, as the worker names it from its GitHub URL.
SHOP_URL = "https://github.com/acme/shop"
REPO = graph_id("repo", SHOP_URL)
CURSOR_KEY = "k" * 32

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "Mcp-Protocol-Version": "2025-06-18",
}


class IsolatedSettings(Settings):
    """Settings that ignore a developer's local .env file."""

    model_config = SettingsConfigDict(env_file=None)


def make_settings(**overrides: Any) -> Settings:
    spanner = {"cursor_signing_key": CURSOR_KEY, **overrides.pop("spanner", {})}
    return IsolatedSettings(**{"environment": Environment.TEST, "spanner": spanner, **overrides})


class FakeEmbedder:
    model = "fake"
    dimensions = 4
    max_input_bytes = 8000

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@dataclass
class Shop:
    """A small service graph: a controller calls OrderService.placeOrder,
    which calls into inventory and payment; placeOrder overrides
    OrderPort.place, which a legacy caller still calls through the interface."""

    store: MemStore
    source: str
    sha: str
    order_service: dict[str, Any]
    place_order: dict[str, Any]
    reserve: dict[str, Any]
    pay: dict[str, Any]
    checkout: dict[str, Any]
    place: dict[str, Any]
    legacy: dict[str, Any]
    cron: dict[str, Any]


def span(start: int, line: int, end: int, end_line: int) -> dict[str, Any]:
    return {
        "start": {"byte_offset": start, "line": line, "column": 1},
        "end": {"byte_offset": end, "line": end_line, "column": 2},
    }


def build_shop() -> Shop:
    store = MemStore(REPO)
    source = (
        "package shop;\n\npublic class OrderService implements OrderPort {\n"
        "    public Order placeOrder(Cart cart) {\n        inventory.reserve(cart);\n"
        "        return payments.pay(cart);\n    }\n}\n"
    )
    sha = hashlib.sha256(source.encode()).hexdigest()
    store.sources[sha] = source.encode()

    def node(
        kind: str,
        name: str,
        qualified: str,
        path: str,
        props: dict[str, str] | None = None,
        where: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        properties = {"file_path": {"string": path}, "language": {"string": "java"}}
        for key, value in (props or {}).items():
            properties[key] = {"string": value}
        record: dict[str, Any] = {
            "id": graph_id(kind, qualified),
            "kind": kind,
            "name": name,
            "qualified_name": qualified,
            "properties": properties,
        }
        if where is not None:
            record["source"] = {
                "lineage": graph_id("file", REPO, "m", "main", path),
                "content_sha256": sha,
                "span": where,
            }
        return store.add_node(record)

    place_start = source.index("public Order placeOrder")
    place_end = source.index("    }\n}\n") + len("    }")
    shop = Shop(
        store=store,
        source=source,
        sha=sha,
        order_service=node(
            "class",
            "OrderService",
            "shop.OrderService",
            "src/shop/OrderService.java",
            {
                "signature": "public class OrderService implements OrderPort",
                "docstring": "Places orders.",
            },
            span(0, 1, len(source), 8),
        ),
        place_order=node(
            "method",
            "placeOrder",
            "placeOrder(shop.Cart)",
            "src/shop/OrderService.java",
            {"signature": "public Order placeOrder(Cart cart)"},
            span(place_start, 4, place_end, 7),
        ),
        reserve=node(
            "method",
            "reserve",
            "reserve(shop.Cart)",
            "src/shop/InventoryService.java",
            {"signature": "void reserve(Cart cart)"},
        ),
        pay=node(
            "method",
            "pay",
            "pay(shop.Cart)",
            "src/shop/PaymentService.java",
            {"signature": "Order pay(Cart cart)"},
        ),
        checkout=node(
            "method", "checkout", "checkout(shop.Cart)", "src/shop/CheckoutController.java"
        ),
        place=node("method", "place", "place(shop.Cart)", "src/shop/OrderPort.java"),
        legacy=node(
            "method",
            "legacyCheckout",
            "legacyCheckout(shop.Cart)",
            "src/legacy/LegacyCheckout.java",
        ),
        cron=node("method", "nightly", "nightly()", "src/legacy/Nightly.java"),
    )
    store.add_edge(EDGE_CONTAINS, shop.order_service["id"], shop.place_order["id"])
    store.add_edge(EDGE_CALLS, shop.place_order["id"], shop.reserve["id"], line=5)
    store.add_edge(EDGE_CALLS, shop.place_order["id"], shop.pay["id"], line=6)
    store.add_edge(EDGE_CALLS, shop.checkout["id"], shop.place_order["id"])
    store.add_edge(EDGE_USES_TYPE, shop.place_order["id"], shop.order_service["id"])
    store.add_edge(EDGE_OVERRIDES, shop.place_order["id"], shop.place["id"])
    store.add_edge(EDGE_CALLS, shop.legacy["id"], shop.place["id"])
    store.add_edge(EDGE_CALLS, shop.cron["id"], shop.legacy["id"])
    store.hits = [
        store.hit(shop.place_order["id"], 1.2, True),
        store.hit(shop.order_service["id"], 0.03, False),
    ]
    return shop


@pytest.fixture
def shop() -> Shop:
    return build_shop()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()
