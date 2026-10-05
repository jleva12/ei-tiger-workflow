from __future__ import annotations

import pytest

from forge_task_documents.errors import ParseError, UnsupportedFormatError
from forge_task_documents.models import ElementKind
from forge_task_documents.parsers import sniff_extension

from . import fixtures
from .conftest import source


def parse(registry, name: str, data: bytes):
    src = source(name, data)
    return registry.resolve(src).parse(src)


def kinds(doc):
    return [e.kind for e in doc.elements]


def test_docx_structure(registry, files):
    doc = parse(registry, "handbook.docx", files["handbook.docx"])
    assert doc.title == "Refund Operations Handbook"
    assert doc.metadata["author"] == "Payments Team"
    headings = [(e.text, e.level, e.section_path) for e in doc.elements if e.kind is ElementKind.HEADING]
    assert headings == [
        ("Refund policy", 1, ["Refund policy"]),
        ("Approval limits", 2, ["Refund policy", "Approval limits"]),
        ("Settlement", 1, ["Settlement"]),
    ]
    table = next(e.table for e in doc.elements if e.kind is ElementKind.TABLE)
    assert table.headers == ["Role", "Region", "Max refund"]
    # merged cell keeps column alignment: 400 stays under "Max refund"
    assert table.rows[-1] == ["Agent EU", "", "400"]
    items = [(e.text, e.level) for e in doc.elements if e.kind is ElementKind.LIST_ITEM]
    assert items == [("Escalate first", 0), ("Then document the case", 1)]
    code = [e for e in doc.elements if e.kind is ElementKind.CODE]
    assert len(code) == 1 and code[0].text.startswith("def issue_refund") and "payments.refund" in code[0].text


def test_xlsx_regions_captions_and_hidden_sheets(registry, files):
    doc = parse(registry, "pricing.xlsx", files["pricing.xlsx"])
    assert doc.metadata["sheets"] == ["Pricing"]  # hidden sheet skipped
    tables = [e for e in doc.elements if e.kind is ElementKind.TABLE]
    assert len(tables) == 2
    main, fx = tables
    assert main.table.caption == "Q3 price list"
    assert main.table.headers == ["SKU", "Product", "Region", "Price"]
    assert len(main.table.rows) == 60
    assert main.location.cell_range == "B4:E64"
    assert main.table.rows[1] == ["SKU10001", "Widget model 1", "EU", "11.5"]
    assert fx.table.headers == ["Currency", "Rate"] and fx.location.cell_range == "B66:C68"


def test_markdown(registry, files):
    doc = parse(registry, "runbook.md", files["runbook.md"])
    assert doc.title == "Billing runbook"
    assert doc.metadata == {"tags": ["billing", "ops"]}
    assert ElementKind.HEADING in kinds(doc) and ElementKind.TABLE in kinds(doc)
    code = next(e for e in doc.elements if e.kind is ElementKind.CODE)
    assert code.language == "python"
    para = next(e for e in doc.elements if "BILL-1042" in e.text)
    assert "issue_refund()" in para.text and "`" not in para.text  # inline markup stripped
    assert para.section_path == ["Refunds"]
    assert para.location.line_start == 11  # front matter offset accounted for
    nested = [e for e in doc.elements if e.kind is ElementKind.LIST_ITEM and e.level == 1]
    assert [e.text for e in nested] == ["Check fraud score"]


def test_text_encoding_and_headings(registry, files):
    doc = parse(registry, "incident.txt", files["incident.txt"])
    text = " ".join(e.text for e in doc.elements)
    assert "Café" in text and "Zürich" in text
    headings = [e.section_path for e in doc.elements if e.kind is ElementKind.HEADING]
    assert headings == [["Incident notes"], ["Incident notes", "ROOT CAUSE"]]
    bullets = [e for e in doc.elements if e.kind is ElementKind.LIST_ITEM]
    assert [b.location.line_start for b in bullets] == [11, 12]


def test_csv(registry, files):
    doc = parse(registry, "people.csv", files["people.csv"])
    (el,) = doc.elements
    assert el.table.headers == ["Employee", "Team", "Location"] and len(el.table.rows) == 2


def test_visio_graph(registry, files):
    doc = parse(registry, "order_flow.vsdx", files["order_flow.vsdx"])
    (el,) = doc.elements
    graph = el.graph
    labels = {n.id: n.label for n in graph.nodes}
    assert set(labels.values()) == {"Receive order", "Validate order", "Ship order", "SLA is 24h"}
    edges = {(labels[e.source], labels[e.target], e.label) for e in graph.edges}
    assert ("Validate order", "Ship order", "approved") in edges
    assert ("Receive order", "Validate order", "submit") in edges
    node = next(n for n in graph.nodes if n.label == "Receive order")
    assert node.properties  # Shape Data carried through
    assert el.location.page == 1 and el.section_path == ["Page-1"]


def test_pdf_structure(registry, files):
    doc = parse(registry, "refund_guide.pdf", files["refund_guide.pdf"])
    assert doc.title == "Refund Operations Guide"  # the largest line on page 1, not the junk export title
    assert doc.metadata["author"] == "Payments Team" and doc.metadata["pages"] == 3
    text = " ".join(e.text for e in doc.elements)
    assert "ACME Corp" not in text and "Page 1 of 3" not in text  # running header and footer dropped
    headings = [
        (e.text, e.level, e.section_path, e.location.page) for e in doc.elements if e.kind is ElementKind.HEADING
    ]
    assert headings == [
        ("Refund policy", 1, ["Refund policy"], 1),
        ("Approval limits", 2, ["Refund policy", "Approval limits"], 1),
        ("Settlement", 1, ["Settlement"], 2),
        ("Appendix", 1, ["Appendix"], 3),
    ]
    para = next(e for e in doc.elements if e.text.startswith("Customers may request"))
    assert "nightly reconciliation job" in para.text  # hyphenated line end rejoined
    assert para.section_path == ["Refund policy"]
    items = [(e.text, e.level) for e in doc.elements if e.kind is ElementKind.LIST_ITEM]
    assert items == [("Escalate first", 0), ("Then document the case in BILL-1042", 1)]
    table = next(e for e in doc.elements if e.kind is ElementKind.TABLE)
    assert table.table.caption == "Table 1: Refund limits" and table.location.page == 1
    assert table.table.headers == ["Role", "Region", "Max refund"]
    assert table.table.rows[-1] == ["Agent", "EU", "400"]
    assert "Max refund" not in text  # table text is not repeated in the prose
    carried = next(e for e in doc.elements if e.text.startswith("Settlement batches"))
    assert carried.text.endswith("general ledger before the bank feed is compared.")  # across the page break
    assert carried.location.page == 1
    (code,) = [e for e in doc.elements if e.kind is ElementKind.CODE]
    assert code.text == "def reconcile(batch_id):\n    return ledger.match(batch_id)"
    assert code.location.page == 2


def test_pdf_reads_two_columns_in_order(registry, files):
    doc = parse(registry, "refund_guide.pdf", files["refund_guide.pdf"])
    page3 = [e.text for e in doc.elements if e.location.page == 3 and e.kind is ElementKind.PARAGRAPH]
    intro, left, right = page3
    assert intro.startswith("This appendix lists")  # full width, above the columns
    assert "Left column" in left and "Right column" not in left
    assert "Right column" in right and "Left column" not in right


def test_pdf_outline_names_headings(registry):
    doc = parse(registry, "runbook.pdf", fixtures.make_pdf_outline())
    headings = [(e.text, e.level) for e in doc.elements if e.kind is ElementKind.HEADING]
    assert headings == [("1 Overview", 1), ("1.1 Scope", 2), ("2 Contacts", 1)]
    scope = next(e for e in doc.elements if e.text.startswith("Only card payments"))
    assert scope.section_path == ["1 Overview", "1.1 Scope"]


def test_pdf_clauses(registry):
    doc = parse(registry, "terms.pdf", fixtures.make_pdf_clauses())
    headings = [e.text for e in doc.elements if e.kind is ElementKind.HEADING]
    assert headings == ["1. General.", "2. Updates."]  # overprinted "fake bold" read once
    clauses = [e.text for e in doc.elements if e.kind is ElementKind.LIST_ITEM]
    assert len(clauses) == 2
    assert clauses[0].startswith("A. The software") and clauses[0].endswith("which govern every update.")
    (para,) = [e for e in doc.elements if e.kind is ElementKind.PARAGRAPH]
    assert para.section_path == ["2. Updates."]
    assert "latest release and WHEN AN UPDATE" in para.text and para.text.endswith("before it installs.")


def test_pdf_without_text_fails(registry):
    with pytest.raises(ParseError, match="no text"):
        parse(registry, "scan.pdf", fixtures.make_pdf_scan())
    with pytest.raises(ParseError, match="cannot open pdf"):
        parse(registry, "broken.pdf", b"%PDF-1.7 not really")


def test_pptx_structure(registry, files):
    doc = parse(registry, "review.pptx", files["review.pptx"])
    assert doc.title == "Refund Operations Review"  # the title slide's
    assert doc.metadata["author"] == "Payments Team"
    assert doc.metadata["slides"] == ["Refund Operations Review", "Approval limits", "Limits by region", "Slide 4"]
    assert "Draft ideas" not in " ".join(e.text for e in doc.elements)  # hidden slide skipped
    headings = [(e.text, e.location.page) for e in doc.elements if e.kind is ElementKind.HEADING]
    assert headings == [
        ("Refund Operations Review", 1),
        ("Approval limits", 2),
        ("Limits by region", 3),
        ("Slide 4", 4),
    ]
    items = [(e.text, e.level) for e in doc.elements if e.kind is ElementKind.LIST_ITEM]
    assert items == [
        ("Agents approve refunds up to 500", 0),
        ("Team leads approve up to 5000", 1),
        ("See ticket BILL-1042", 0),
    ]
    notes = next(e for e in doc.elements if "EU limit" in e.text)
    assert notes.kind is ElementKind.PARAGRAPH and notes.section_path == ["Approval limits", "Notes"]
    table, chart = [e for e in doc.elements if e.kind is ElementKind.TABLE]
    assert table.table.headers == ["Role", "Region", "Max refund"]
    assert table.table.rows == [["Agent", "US", "500"], ["Agent EU", "", "400"]]  # merged cell keeps alignment
    assert chart.table.caption == "Refunds per quarter"
    assert chart.table.headers == ["Category", "Refunds"] and chart.table.rows == [["Q1", "120"], ["Q2", "95"]]
    assert chart.location.page == 3 and chart.location.page_name == "Limits by region"
    slide4 = [e.text for e in doc.elements if e.location.page == 4 and e.kind is ElementKind.PARAGRAPH]
    assert slide4 == ["Settlement runs nightly.", "Ledger", "Bank feed"]  # group read left to right


def test_pptx_slide_show(registry, files):
    import io
    import zipfile

    data = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(files["review.pptx"])) as zin, zipfile.ZipFile(data, "w") as zout:
        for item in zin.infolist():
            body = zin.read(item.filename)
            if item.filename == "[Content_Types].xml":
                body = body.replace(b"presentationml.presentation.main+xml", b"presentationml.slideshow.main+xml")
            zout.writestr(item, body)
    doc = parse(registry, "review.ppsx", data.getvalue())
    assert doc.source_type == "pptx" and doc.title == "Refund Operations Review"


def test_smartart_node_text():
    from forge_task_documents.parsers.pptx import diagram_texts

    blob = b"""<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
        xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><dgm:ptLst>
      <dgm:pt modelId="0" type="doc"><dgm:t><a:p><a:r><a:t>ignored</a:t></a:r></a:p></dgm:t></dgm:pt>
      <dgm:pt modelId="1"><dgm:t><a:p><a:r><a:t>Receive </a:t></a:r><a:r><a:t>order</a:t></a:r></a:p></dgm:t></dgm:pt>
      <dgm:pt modelId="2" type="parTrans"/>
      <dgm:pt modelId="3" type="node"><dgm:t><a:p><a:r><a:t>Ship order</a:t></a:r></a:p></dgm:t></dgm:pt>
    </dgm:ptLst></dgm:dataModel>"""
    assert diagram_texts(blob) == ["Receive order", "Ship order"]


def test_registry_resolution(registry, files):
    # by media type when the name has no extension
    src = source("blob", files["runbook.md"], media_type="text/markdown")
    assert registry.resolve(src).name == "markdown"
    # by sniffing OOXML zip parts
    src = source("upload.bin", files["handbook.docx"])
    assert registry.resolve(src).name == "docx"
    assert sniff_extension(files["pricing.xlsx"]) == "xlsx"
    assert sniff_extension(files["order_flow.vsdx"]) == "vsdx"
    assert sniff_extension(files["review.pptx"]) == "pptx"
    assert sniff_extension(files["refund_guide.pdf"]) == "pdf"
    assert registry.resolve(source("upload.bin", files["refund_guide.pdf"])).name == "pdf"
    assert registry.resolve(source("scan", b"", media_type="application/pdf")).name == "pdf"
    assert registry.resolve(source("deck.pptm", b"")).name == "pptx"
    with pytest.raises(UnsupportedFormatError):
        registry.resolve(source("image.png", b"\x89PNG\r\n\x1a\n\x00\x00"))


def test_registry_override(registry):
    class MyDocx:
        name = "my-docx"
        version = "1"
        extensions = frozenset({"docx"})
        media_types = frozenset()

        def parse(self, source):  # pragma: no cover
            raise NotImplementedError

    registry.register(MyDocx())
    assert registry.resolve(source("a.docx", b"")).name == "my-docx"


def test_docling_adapter_maps_to_ir():
    from docling_core.types.doc import DocItemLabel, DoclingDocument, TableCell, TableData

    from forge_task_documents.parsers.docling_adapter import docling_to_ir

    dl = DoclingDocument(name="report")
    dl.add_title(text="Quarterly Report")
    dl.add_heading(text="Revenue", level=1)
    dl.add_text(label=DocItemLabel.TEXT, text="Revenue grew 12% year over year.")
    cells = [
        TableCell(
            text=t,
            start_row_offset_idx=r,
            end_row_offset_idx=r + 1,
            start_col_offset_idx=c,
            end_col_offset_idx=c + 1,
            column_header=(r == 0),
        )
        for r, row in enumerate([["Region", "Revenue"], ["US", "10"], ["EU", "7"]])
        for c, t in enumerate(row)
    ]
    dl.add_table(data=TableData(num_rows=3, num_cols=2, table_cells=cells))
    doc = docling_to_ir(dl, source=source("r.docx", b""), source_type="docx", parser_name="docling", parser_version="1")
    assert doc.title == "Quarterly Report"
    assert [e.kind for e in doc.elements] == [ElementKind.HEADING, ElementKind.PARAGRAPH, ElementKind.TABLE]
    table = doc.elements[-1].table
    assert table.headers == ["Region", "Revenue"] and table.rows == [["US", "10"], ["EU", "7"]]
    assert doc.elements[1].section_path == ["Revenue"]


@pytest.mark.parametrize(
    ("docx", "pdf", "docling"),
    [
        ("python-docx", "pdfplumber", set()),
        ("python-docx", "docling", {"pdf"}),
        ("docling", "docling", {"docx", "pdf"}),
    ],
)
def test_docling_takes_the_formats_configured(docx, pdf, docling):
    from forge_task_documents.config import DocumentsSettings
    from forge_task_documents.task import DocumentsTaskFactory

    registry = DocumentsTaskFactory._registry(DocumentsSettings(docx_parser=docx, pdf_parser=pdf))
    for ext in ("docx", "pdf"):
        name = registry.resolve(source(f"a.{ext}", b"")).name
        assert (name == "docling") is (ext in docling), ext
    if "pdf" in docling:
        assert registry.resolve(source("blob", b"", media_type="application/pdf")).name == "docling"


def graph_of(doc):
    (graph,) = [e.graph for e in doc.elements if e.kind is ElementKind.GRAPH]
    labels = {n.id: n.label for n in graph.nodes}
    edges = {(labels[e.source], labels[e.target], e.label, e.directed) for e in graph.edges}
    return graph, labels, edges


def test_mermaid_flowchart_is_a_graph(registry):
    doc = parse(registry, "order-flow.mmd", fixtures.MERMAID_FLOWCHART.encode())
    assert doc.source_type == "mermaid" and doc.title == "Order flow"
    assert doc.metadata == {"diagram": "flowchart"}
    graph, labels, edges = graph_of(doc)
    assert graph.name == "Order flow"
    assert ("Receive order", "Valid?", None, True) in edges
    # a label inside the link, and fan-out to both targets
    assert {("Valid?", "Ship order", "yes", True), ("Valid?", "Orders DB", "yes", True)} <= edges
    assert ("Valid?", "Reject order", "no", True) in edges
    # the subgraph groups its nodes and, pointed at, is named by its title
    assert ("Ship order", "Payments", None, True) in edges
    nodes = {n.label: n for n in graph.nodes}
    assert nodes["Charge card via Stripe"].group == "Payments"  # <br/> and emphasis gone
    assert nodes["Orders DB"].shape_type == "database" and nodes["Valid?"].shape_type == "decision"
    # ~~~ only lays nodes out; styling statements aren't nodes
    assert not any({"X", "Y"} & {a, b} for a, b, _, _ in edges)
    assert "red" not in labels and "classDef" not in labels


def test_mermaid_links(registry):
    doc = parse(
        registry,
        "links.mmd",
        b"graph LR; a --- b; c <--> d; e -. maybe .-> f; g == must ==> h; i --o j; k-->l-->m",
    )
    _, _, edges = graph_of(doc)
    assert ("a", "b", None, False) in edges  # no arrowhead
    assert {("c", "d", None, True), ("d", "c", None, True)} <= edges  # both ways
    assert ("e", "f", "maybe", True) in edges and ("g", "h", "must", True) in edges
    assert ("i", "j", None, True) in edges
    assert {("k", "l", None, True), ("l", "m", None, True)} <= edges  # a chain


def test_mermaid_sequence_keeps_its_order(registry):
    doc = parse(registry, "checkout.mmd", fixtures.MERMAID_SEQUENCE.encode())
    assert doc.title == "Checkout" and doc.metadata["diagram"] == "sequence"
    assert [(e.kind, e.text, e.section_path) for e in doc.elements] == [
        (ElementKind.PARAGRAPH, "Participants: Shopper (actor), Web app, API", []),
        (ElementKind.LIST_ITEM, "Shopper → Web app: Click pay", []),
        (ElementKind.LIST_ITEM, "Web app → API: POST /orders", []),
        (ElementKind.LIST_ITEM, "API → Web app: 201 Created", ["alt card ok"]),
        (ElementKind.LIST_ITEM, "API → Web app: 402 Payment Required", ["else declined"]),
        (ElementKind.LIST_ITEM, "Web app → API (async): poll status", ["loop every 5s"]),
        (ElementKind.PARAGRAPH, "Note over Web app, API: retries are idempotent", []),
    ]
    assert doc.elements[1].location.line_start == 6


def test_mermaid_state_class_and_er_diagrams(registry):
    state = parse(
        registry,
        "car.mmd",
        b"stateDiagram-v2\n[*] --> Still\nStill --> Moving : push\nMoving --> [*]\n"
        b"state Moving {\n  [*] --> Slow\n  Slow --> Fast : accelerate\n}\nStill : Standing still\n",
    )
    graph, _, edges = graph_of(state)
    assert {("start", "Standing still", None, True), ("Standing still", "Moving", "push", True)} <= edges
    assert ("Slow", "Fast", "accelerate", True) in edges
    assert next(n for n in graph.nodes if n.label == "Fast").group == "Moving"

    classes = parse(
        registry,
        "zoo.mmd",
        b"classDiagram\nclass Animal {\n  <<abstract>>\n  +eat() void\n}\nAnimal <|-- Duck\n"
        b'Car *-- Wheel\nCustomer "1" --> "*" Ticket : buys\nService ..> Repo\n',
    )
    graph, _, edges = graph_of(classes)
    assert ("Duck", "Animal", "inherits from", True) in edges
    assert ("Car", "Wheel", "composed of", True) in edges
    assert ("Customer", "Ticket", "buys (1 to *)", True) in edges
    assert ("Service", "Repo", "depends on", True) in edges
    animal = next(n for n in graph.nodes if n.label == "Animal")
    assert animal.properties == {"stereotype": "abstract", "members": "+eat() void"}

    er = parse(
        registry,
        "shop.mmd",
        b'erDiagram\nCUSTOMER ||--o{ ORDER : places\nCUSTOMER {\n  string name PK "full name"\n}\n',
    )
    graph, _, edges = graph_of(er)
    assert ("CUSTOMER", "ORDER", "places (exactly one to zero or more)", True) in edges
    assert graph.nodes[0].properties == {"attributes": "name (string, PK): full name"}


def test_mermaid_without_edges_stays_code(registry):
    doc = parse(registry, "plan.mermaid", b"gantt\n    title Release plan\n    section Build\n    Parser :a1, 3d\n")
    assert doc.title == "Release plan" and doc.metadata["diagram"] == "gantt"
    (code,) = doc.elements
    assert code.kind is ElementKind.CODE and code.language == "mermaid" and "section Build" in code.text
    # a flowchart with nothing readable falls back the same way
    (empty,) = parse(registry, "odd.mmd", b"flowchart LR\n    ((((\n").elements
    assert empty.kind is ElementKind.CODE


def test_markdown_mermaid_fences_are_diagrams(registry):
    text = "# Design\n\n## Ordering\n\n```mermaid\n" + fixtures.MERMAID_SEQUENCE + "```\n\n```python\nx = 1\n```\n"
    doc = parse(registry, "design.md", text.encode())
    items = [e for e in doc.elements if e.kind is ElementKind.LIST_ITEM]
    assert items[0].text == "Shopper → Web app: Click pay"
    assert items[0].section_path == ["Ordering"] and items[0].location.line_start == 11
    assert items[2].section_path == ["Ordering", "alt card ok"]
    (code,) = [e for e in doc.elements if e.kind is ElementKind.CODE]
    assert code.language == "python"


def test_mermaid_resolves_by_extension_and_media_type(registry):
    assert registry.resolve(source("a.mmd", b"")).name == "mermaid"
    assert registry.resolve(source("a.mermaid", b"")).name == "mermaid"
    assert registry.resolve(source("diagram", b"graph TD", media_type="text/vnd.mermaid")).name == "mermaid"
