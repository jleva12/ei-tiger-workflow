from forge_codegraph import connection_sentence, system_summary
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
