from forge_codegraph import (
    connection_sentence,
    design_summary,
    design_view,
    members_of,
    system_summary,
)
from forge_codegraph.system import MAX_DESCRIBED


def test_a_connection_reads_as_a_sentence() -> None:
    assert (
        connection_sentence("acme/web", "calls", "acme/orders", " POST\n/v1/orders ")
        == "acme/web calls the API of acme/orders (POST /v1/orders)"
    )
    assert connection_sentence("a", "events", "b") == "a sends events to b"


def test_the_summary_names_the_code_and_how_it_connects() -> None:
    assert system_summary("", [], []) == "code repositories"
    assert (
        system_summary(
            "Checkout",
            ["acme/web", "acme/orders"],
            [{"source": "acme/web", "kind": "calls", "target": "acme/orders"}],
        )
        == "Checkout; code of acme/web, acme/orders; how they connect: "
        "acme/web calls the API of acme/orders"
    )


def test_a_long_map_is_cut_short() -> None:
    many = [
        {"source": f"s{i}", "kind": "depends_on", "target": "t"}
        for i in range(MAX_DESCRIBED + 3)
    ]
    assert system_summary("", ["t"], many).endswith("; and 3 more")


DESIGN = {
    "groups": [
        {"name": "Payments", "description": "Charges customers\nand refunds them"},
        {"name": "Edge", "description": ""},
    ],
    "applications": [
        {"name": "acme/web", "url": "https://github.com/acme/web", "group": ""},
        {
            "name": "acme/billing",
            "url": "https://github.com/acme/billing",
            "group": "Payments",
        },
    ],
    "components": [
        {
            "name": "payments-db",
            "kind": "database",
            "technology": "PostgreSQL 16",
            "description": "Charges and refunds",
            "group": "Payments",
        },
        {
            "name": "Stripe",
            "kind": "external",
            "technology": "",
            "description": "",
            "group": "",
        },
    ],
    "connections": [
        {
            "source": "acme/web",
            "kind": "calls",
            "target": "acme/billing",
            "description": "POST /charges",
        },
        {
            "source": "acme/billing",
            "kind": "writes_to",
            "target": "payments-db",
            "description": "",
        },
        {
            "source": "acme/billing",
            "kind": "calls",
            "target": "Stripe",
            "description": "",
        },
    ],
}


def test_the_summary_names_the_other_parts_and_the_groups() -> None:
    assert design_summary("Checkout", DESIGN) == (
        "Checkout; code of acme/web, acme/billing; other parts: payments-db "
        "(database, PostgreSQL 16); Stripe (external system); groups: Payments "
        "(acme/billing, payments-db): Charges customers and refunds them; Edge; "
        "how they connect: acme/web calls the API of acme/billing (POST /charges); "
        "acme/billing writes to payments-db; acme/billing calls the API of Stripe"
    )
    assert members_of(DESIGN) == {
        "Payments": ["acme/billing", "payments-db"],
        "Edge": [],
    }


def test_the_design_is_read_whole() -> None:
    view = design_view([("Checkout", "The shop", DESIGN)])
    (system,) = view["systems"]
    assert (system["knowledge_base"], system["description"]) == ("Checkout", "The shop")
    assert system["groups"][0] == {
        "name": "Payments",
        "description": "Charges customers and refunds them",
        "members": ["acme/billing", "payments-db"],
    }
    assert system["components"][0]["kind"] == "database"
    assert system["components"][1]["kind"] == "external system"
    assert system["connections"][1] == {
        "source": "acme/billing",
        "relation": "writes to",
        "target": "payments-db",
        "description": "",
    }


def test_a_focus_reads_one_part_and_what_it_connects_to() -> None:
    (system,) = design_view([("Checkout", "", DESIGN)], "STRIPE")["systems"]
    assert [c["target"] for c in system["connections"]] == ["Stripe"]
    assert [a["name"] for a in system["applications"]] == ["acme/billing"]
    assert [c["name"] for c in system["components"]] == ["Stripe"]
    assert [g["name"] for g in system["groups"]] == ["Payments"]
    # A group: what's in it, and what that connects to.
    (system,) = design_view([("Checkout", "", DESIGN)], "payments")["systems"]
    assert {c["target"] for c in system["connections"]} == {
        "acme/billing",
        "payments-db",
        "Stripe",
    }
    # An empty group is still found.
    (system,) = design_view([("Checkout", "", DESIGN)], "Edge")["systems"]
    assert [g["name"] for g in system["groups"]] == ["Edge"]
    assert system["connections"] == []


def test_a_focus_that_picks_nothing_lists_the_names() -> None:
    view = design_view([("Checkout", "", DESIGN)], "kafka")
    assert view["systems"] == []
    assert "acme/billing, acme/web, Edge, Payments, payments-db, Stripe" in view["note"]
