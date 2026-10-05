from __future__ import annotations

import pytest

from forge_embeddings.tokenizers import HeuristicTokenizer
from forge_task_documents.chunking import ChunkEngine, ChunkingConfig
from forge_task_documents.models import ChunkKind, Element, ElementKind, ParsedDocument

from . import fixtures
from .conftest import source


def parse(registry, name, data):
    src = source(name, data)
    return registry.resolve(src).parse(src)


def doc_of(*elements: Element, title: str = "Doc") -> ParsedDocument:
    return ParsedDocument(
        tenant_id="t1",
        doc_id="d1",
        source_type="test",
        title=title,
        elements=list(elements),
        parser_name="test",
        parser_version="1",
    )


@pytest.mark.parametrize(
    "name",
    [
        "handbook.docx",
        "pricing.xlsx",
        "runbook.md",
        "incident.txt",
        "people.csv",
        "order_flow.vsdx",
        "refund_guide.pdf",
        "review.pptx",
    ],
)
def test_every_chunk_fits_budget(registry, engine, files, name):
    chunks = engine.chunk(parse(registry, name, files[name]))
    assert chunks
    tok = engine.tokenizer
    for c in chunks:
        assert tok.count(c.text) <= engine.config.max_tokens, (c.kind, c.text[:80])
        assert c.text.strip()
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert len({c.id for c in chunks}) == len(chunks)


def test_large_table_row_groups_and_summary(registry, engine, files):
    chunks = engine.chunk(parse(registry, "pricing.xlsx", files["pricing.xlsx"]))
    summary = [c for c in chunks if c.kind is ChunkKind.TABLE_SUMMARY]
    groups = [c for c in chunks if c.kind is ChunkKind.TABLE and c.metadata.get("row_count") == 60]
    assert len(summary) == 1
    assert "60 rows" in summary[0].text and "Region values: US, EU, APAC" in summary[0].text
    assert "Price ranges from 10 to 98.5" in summary[0].text
    assert len(groups) > 1
    covered = []
    for g in groups:
        assert g.text.startswith("Table: Q3 price list\nColumns: SKU, Product, Region, Price\nRows ")
        covered.extend(range(g.metadata["row_start"], g.metadata["row_end"] + 1))
    assert covered == list(range(1, 61))  # every row exactly once, in order
    first = groups[0]
    assert first.location.sheet == "Pricing"
    assert first.location.cell_range == f"B5:E{4 + first.metadata['row_end']}"
    # small second table stays whole
    fx = [c for c in chunks if c.kind is ChunkKind.TABLE and c.metadata.get("row_count") == 2]
    assert len(fx) == 1 and "Currency: EUR; Rate: 1.08" in fx[0].text


def test_identical_table_serialization_across_formats(engine):
    from forge_task_documents.models import TableData

    table = TableData(headers=["Region", "Limit"], rows=[["US", "500"], ["EU", "400"]])
    a = engine.chunk(doc_of(Element(kind=ElementKind.TABLE, table=table, section_path=["Limits"])))
    b = engine.chunk(doc_of(Element(kind=ElementKind.TABLE, table=table.model_copy(), section_path=["Limits"])))
    assert (
        a[0].text
        == b[0].text
        == "Table: Limits\nColumns: Region, Limit\nRegion: US; Limit: 500\nRegion: EU; Limit: 400"
    )


def test_long_paragraph_splits_on_sentences(engine):
    el = Element(kind=ElementKind.PARAGRAPH, text=fixtures.LONG_PARAGRAPH, section_path=["Settlement"])
    chunks = engine.chunk(doc_of(el))
    assert len(chunks) > 3
    for c in chunks:
        assert c.text.rstrip().endswith("."), "split mid-sentence"
        assert c.section_path == ["Settlement"]


def test_code_is_atomic_when_it_fits_and_split_on_blank_lines_otherwise():
    engine = ChunkEngine(HeuristicTokenizer(), ChunkingConfig(max_tokens=60, min_tokens=10))
    small = "def f(x):\n    return x + 1"
    big = "\n\n".join(f"def func_{i}(a, b):\n    total = a + b\n    return total * {i}" for i in range(12))
    chunks = engine.chunk(
        doc_of(
            Element(kind=ElementKind.CODE, text=small, language="python"),
            Element(kind=ElementKind.CODE, text=big, language="python"),
        )
    )
    assert chunks[0].text == small and chunks[0].metadata == {"language": "python"}
    rest = chunks[1:]
    assert len(rest) > 1
    for c in rest:
        assert c.text.startswith("def func_")  # never cut inside a function
        assert engine.tokenizer.count(c.text) <= 60


def test_graph_serialization(registry, engine, files):
    (chunk,) = engine.chunk(parse(registry, "order_flow.vsdx", files["order_flow.vsdx"]))
    assert chunk.kind is ChunkKind.GRAPH
    lines = chunk.text.splitlines()
    assert lines[0] == "Diagram: Page-1"
    assert "[Receive order] --submit--> [Validate order]" in lines
    assert "[Validate order] --approved--> [Ship order]" in lines
    # flow order: the edge out of the entry node comes before the downstream one
    assert lines.index("[Receive order] --submit--> [Validate order]") < lines.index(
        "[Validate order] --approved--> [Ship order]"
    )
    assert "[SLA is 24h]" in lines  # unconnected annotation kept
    assert any(line.startswith("Receive order (") for line in lines)  # shape data


def test_big_graph_gets_summary_and_splits():
    from forge_task_documents.models import GraphData, GraphEdge, GraphNode

    nodes = [
        GraphNode(id=str(i), label=f"Step {i} of the onboarding process", group="Ops" if i % 2 else "Sales")
        for i in range(40)
    ]
    edges = [GraphEdge(source=str(i), target=str(i + 1)) for i in range(39)]
    engine = ChunkEngine(HeuristicTokenizer(), ChunkingConfig(max_tokens=120, min_tokens=10))
    chunks = engine.chunk(
        doc_of(
            Element(
                kind=ElementKind.GRAPH,
                graph=GraphData(name="Onboarding", nodes=nodes, edges=edges),
                section_path=["Onboarding"],
            )
        )
    )
    assert chunks[0].kind is ChunkKind.GRAPH_SUMMARY and "Lanes: Ops, Sales" in chunks[0].text
    parts = chunks[1:]
    assert len(parts) > 2
    assert sum(c.text.count("-->") for c in parts) == 39  # every edge exactly once
    assert all(c.text.startswith("Diagram: Onboarding") for c in parts)


def test_small_sibling_sections_merge_but_big_ones_do_not(engine):
    els = [
        Element(kind=ElementKind.HEADING, text="Setup", level=1, section_path=["Setup"]),
        Element(kind=ElementKind.HEADING, text="Linux", level=2, section_path=["Setup", "Linux"]),
        Element(kind=ElementKind.PARAGRAPH, text="Run the installer.", section_path=["Setup", "Linux"]),
        Element(kind=ElementKind.HEADING, text="Mac", level=2, section_path=["Setup", "Mac"]),
        Element(kind=ElementKind.PARAGRAPH, text="Use brew.", section_path=["Setup", "Mac"]),
        Element(kind=ElementKind.HEADING, text="Billing", level=1, section_path=["Billing"]),
        Element(kind=ElementKind.PARAGRAPH, text=fixtures.LONG_PARAGRAPH[:600], section_path=["Billing"]),
    ]
    chunks = engine.chunk(doc_of(*els))
    first = chunks[0]
    assert "Run the installer." in first.text and "Use brew." in first.text
    assert first.section_path == ["Setup"]  # common prefix of the merged sections
    assert all("Use brew." not in c.text for c in chunks[1:])


def test_ids_are_deterministic_and_position_independent(registry, engine):
    base = parse(registry, "handbook.docx", fixtures.make_docx())
    again = parse(registry, "handbook.docx", fixtures.make_docx())
    ids_a = [c.id for c in engine.chunk(base)]
    assert ids_a == [c.id for c in engine.chunk(again)]
    edited = parse(registry, "handbook.docx", fixtures.make_docx(extra_intro="Internal draft, do not share."))
    ids_b = {c.id for c in engine.chunk(edited)}
    # the intro merges into the first section's chunk; everything else keeps its id
    assert len(set(ids_a) - ids_b) <= 1


def test_version_tracks_config(engine):
    other = ChunkEngine(HeuristicTokenizer(), engine.config.model_copy(update={"max_tokens": 999}))
    assert engine.version != other.version
    same = ChunkEngine(HeuristicTokenizer(), engine.config.model_copy())
    assert engine.version == same.version


def test_custom_strategy_can_be_registered(engine):
    from forge_task_documents.chunking import BlockKind, ChunkDraft

    class UpperCode:
        block_kind = BlockKind.CODE
        version = "x"

        def split(self, block, ctx):
            return [ChunkDraft(ChunkKind.CODE, e.text.upper(), block.section_path) for e in block.elements]

    engine.register_strategy(UpperCode())
    (c,) = engine.chunk(doc_of(Element(kind=ElementKind.CODE, text="print(1)")))
    assert c.text == "PRINT(1)"


def test_heuristic_tokenizer_split_respects_budget_and_overlap():
    tok = HeuristicTokenizer()
    text = " ".join(f"word{i}" for i in range(200))
    windows = tok.split(text, 50, overlap=10)
    assert all(tok.count(w) <= 50 for w in windows)
    assert windows[1].split()[0] in windows[0]  # overlap carried
    assert windows[-1].endswith("word199")


def test_mermaid_graph_serialization(registry, engine):
    (chunk,) = engine.chunk(parse(registry, "order-flow.mmd", fixtures.make_mermaid()))
    assert chunk.kind is ChunkKind.GRAPH
    lines = chunk.text.splitlines()
    assert lines[0] == "Diagram: Order flow"
    assert "[Valid?] --no--> [Reject order]" in lines
    assert lines.index("[Receive order] --> [Valid?]") < lines.index("[Valid?] --yes--> [Ship order]")
    assert "Lane: Payments" in lines and "[Charge card via Stripe] --> [Refund]" in lines
