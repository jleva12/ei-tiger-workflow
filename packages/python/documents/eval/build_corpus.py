"""Builds the ``core`` suite's documents: one fictional insurer's (Northwind
Mutual's) policies, rates, runbooks and records, one file per format.

The files are committed; rerun this after changing their content here, then
re-check the labels (``forge-async-worker docs eval check``) and record a new
baseline. Dates are pinned so a rebuild of unchanged content gives the same bytes.

    uv run python eval/build_corpus.py            # from packages/python/documents
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

CORPUS = Path(__file__).parent / "suites" / "core" / "corpus"
PINNED = datetime(2026, 10, 1, 9, 0, 0)

# ----------------------------------------------------------------------------- Word: travel and expense policy


def expense_policy_docx() -> bytes:
    import docx

    d = docx.Document()
    props = d.core_properties
    props.title = "Travel and Expense Policy"
    props.author = "Northwind Mutual Finance"
    props.created = props.modified = PINNED
    props.last_modified_by = "finance"
    props.revision = 1

    d.add_heading("Scope", level=1)
    d.add_paragraph(
        "This policy applies to all Northwind Mutual employees and contractors travelling on company business. "
        "Expenses that break this policy are not reimbursed."
    )
    d.add_heading("Air travel", level=1)
    d.add_heading("Class of travel", level=2)
    d.add_paragraph(
        "Economy class is required for flights under 6 hours. Premium economy is permitted for flights of 6 hours "
        "or more. Business class is allowed only for flights over 10 hours, with VP approval."
    )
    d.add_heading("Booking", level=2)
    d.add_paragraph(
        "Book all flights through Navan at least 14 days before departure. Bookings made less than 7 days before "
        "departure need your manager's approval in Navan."
    )
    d.add_heading("Lodging", level=1)
    d.add_paragraph("Hotel stays are reimbursed up to the nightly cap for the city's tier:")
    rows = [
        ["City tier", "Example cities", "Nightly cap (USD)"],
        ["Tier 1", "New York, London, Tokyo", "350"],
        ["Tier 2", "Chicago, Berlin, Singapore", "250"],
        ["Tier 3", "All other cities", "175"],
    ]
    table = d.add_table(rows=len(rows), cols=3)
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.rows[r].cells[c].text = value
    d.add_paragraph("Stays over the cap need a written justification attached to the expense report.")
    d.add_heading("Meals", level=1)
    d.add_paragraph(
        "The daily meal allowance (per diem) is 75 USD for domestic travel and 95 USD for international travel. "
        "Alcohol is not reimbursable, except at client dinners approved in advance by a director."
    )
    d.add_heading("Ground transport", level=1)
    d.add_paragraph(
        "Taxis, rideshare and public transport are reimbursed at cost. Driving your own car is reimbursed at "
        "0.67 USD per mile; parking and tolls are reimbursed with receipts."
    )
    d.add_heading("Approvals and submission", level=1)
    for text in (
        "Submit expense reports in Navan within 30 days of the trip ending.",
        "Reports totalling over 2,500 USD need director approval.",
        "Reports submitted more than 60 days after the trip are reimbursed only with CFO approval.",
    ):
        d.add_paragraph(text, style="List Number")
    d.add_heading("Not reimbursable", level=1)
    for text in (
        "Seat or cabin upgrades bought personally",
        "Traffic and parking fines",
        "Excess baggage fees over 50 USD a trip",
    ):
        d.add_paragraph(text, style="List Bullet")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------------- Excel: auto premium rates

STATES = {
    "Ohio": 88,
    "Texas": 104,
    "Florida": 131,
    "California": 126,
    "New York": 139,
    "Illinois": 97,
    "Georgia": 109,
    "Arizona": 101,
    "Washington": 95,
    "Colorado": 99,
    "Michigan": 142,
    "Pennsylvania": 93,
}
PLANS = {"Basic": (1.0, 1000, "No"), "Standard": (1.35, 500, "Yes"), "Premium": (1.8, 250, "Yes")}


def premium_rows() -> list[list[object]]:
    rows: list[list[object]] = []
    for state, base in STATES.items():
        for plan, (factor, deductible, roadside) in PLANS.items():
            if state == "Florida" and plan == "Basic":
                deductible = 1500  # hurricane exposure
            if state == "Florida" and plan == "Standard":
                roadside = "No"
            rows.append([state, plan, round(base * factor), deductible, roadside])
    return rows


def premium_rates_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.properties.creator = "Northwind Mutual Pricing"
    wb.properties.title = "Auto premium rates 2026"
    wb.properties.created = wb.properties.modified = PINNED
    ws = wb.active
    ws.title = "Monthly premiums"
    ws.append(["State", "Plan", "Monthly premium (USD)", "Deductible (USD)", "Roadside assistance"])
    for row in premium_rows():
        ws.append(row)
    discounts = wb.create_sheet("Discounts")
    discounts.append(["Discount", "Percent off", "Who qualifies"])
    for row in (
        ["Multi-policy", "12%", "Customers with both home and auto policies at Northwind"],
        ["Safe driver", "15%", "No at-fault claims in the last 5 years"],
        ["Good student", "8%", "Full-time students under 25 with a B average or better"],
        ["DriveWise telematics", "10%", "Drivers enrolled in the DriveWise app for at least 90 days"],
        ["Paid in full", "5%", "Annual premium paid upfront"],
    ):
        discounts.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------------- HTML: incident runbook

RUNBOOK_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Incident Response Runbook – Northwind Engineering</title>
  <meta name="author" content="Northwind Reliability Team">
</head>
<body>
  <nav><a href="/">Engineering home</a> · <a href="/runbooks">All runbooks</a></nav>
  <main>
    <h1>Incident Response Runbook</h1>
    <p>How Northwind engineering declares, runs and closes production incidents.</p>

    <h2>Severity levels</h2>
    <table>
      <thead><tr><th>Severity</th><th>Definition</th><th>Response time</th><th>Who is paged</th></tr></thead>
      <tbody>
        <tr><td>SEV1</td><td>Claims intake or payments down for all customers</td><td>15 minutes</td>
            <td>On-call engineer, incident commander and the Head of Engineering</td></tr>
        <tr><td>SEV2</td><td>A major feature down or degraded in one region</td><td>30 minutes</td>
            <td>On-call engineer and incident commander</td></tr>
        <tr><td>SEV3</td><td>A minor feature degraded, with a workaround</td><td>4 business hours</td>
            <td>On-call engineer</td></tr>
        <tr><td>SEV4</td><td>Cosmetic issue or internal tooling</td><td>Next business day</td>
            <td>The owning team's queue</td></tr>
      </tbody>
    </table>

    <h2>Declaring an incident</h2>
    <ol>
      <li>Open an incident in PagerDuty against the affected service
        <ol>
          <li>Pick the severity from the table above</li>
          <li>Name an incident commander; it is never the person fixing the problem</li>
        </ol>
      </li>
      <li>Create a Slack channel named <code>#inc-YYYYMMDD-short-name</code> and invite the responders</li>
      <li>Post a status update every 30 minutes for SEV1 and every hour for SEV2</li>
    </ol>

    <h2>Rolling back a deployment</h2>
    <p>When an incident starts within 30 minutes of a deploy, roll back first and investigate later.</p>
    <pre><code class="language-bash">kubectl rollout undo deployment/claims-api -n claims-prod
kubectl rollout status deployment/claims-api -n claims-prod</code></pre>

    <h2>After the incident</h2>
    <ul>
      <li>Write a postmortem within 5 business days for SEV1 and SEV2
        <ul>
          <li>Use the blameless template in Confluence</li>
          <li>Review it in the Thursday reliability meeting</li>
        </ul>
      </li>
      <li>File follow-up tickets in the RELY Jira project</li>
    </ul>
  </main>
  <footer>© Northwind Mutual · Internal use only</footer>
</body>
</html>
"""

# ----------------------------------------------------------------------------- PowerPoint: business review


def business_review_pptx() -> bytes:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    prs = Presentation()
    prs.core_properties.author = "Northwind Mutual Strategy"
    prs.core_properties.title = "Q3 2026 Business Review"
    prs.core_properties.created = prs.core_properties.modified = PINNED

    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Q3 2026 Business Review"
    slide.placeholders[1].text = "Claims and Underwriting"

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Key metrics"
    body = slide.placeholders[1].text_frame
    body.text = "Loss ratio: 61.4% (target 63%)"
    for text, level in (
        ("Net promoter score: 48, up from 41 in Q2", 0),
        ("Average claim cycle time: 9.2 days", 0),
        ("Fast-track claims: 2.1 days", 1),
    ):
        paragraph = body.add_paragraph()
        paragraph.text = text
        paragraph.level = level
    slide.notes_slide.notes_text_frame.text = "The NPS gain came mostly from the new self-service claims portal."

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Claims volume by region"
    rows = [
        ["Region", "Q2 claims", "Q3 claims", "Change"],
        ["North America", "18,240", "19,880", "+9%"],
        ["EMEA", "11,050", "10,410", "-6%"],
        ["APAC", "7,320", "8,900", "+22%"],
        ["LATAM", "4,110", "4,370", "+6%"],
    ]
    table = slide.shapes.add_table(len(rows), 4, Inches(0.5), Inches(1.5), Inches(5), Inches(2)).table
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    data = CategoryChartData()
    data.categories = ["Auto", "Home", "Commercial"]
    data.add_series("Q3 claims", (23100, 14200, 6260))
    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5.7), Inches(1.5), Inches(4), Inches(3), data
    ).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "Q3 claims by line of business"

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Q4 priorities"
    body = slide.placeholders[1].text_frame
    body.text = "Launch DriveWise telematics in EMEA"
    for text, level in (
        ("Pilot in Germany and the Netherlands from November", 1),
        ("Cut fast-track cycle time to under 2 days", 0),
        ("Hire 40 field adjusters in APAC", 0),
    ):
        paragraph = body.add_paragraph()
        paragraph.text = text
        paragraph.level = level
    slide.notes_slide.notes_text_frame.text = "The APAC hiring budget was approved by the board on 14 September."

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ----------------------------------------------------------------------------- PDF: vendor service agreement


def vendor_agreement_pdf() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    width, height = letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=1)
    c.setTitle("Service Agreement - Northwind Mutual and Acme Auto Repairs")
    c.setAuthor("Northwind Mutual Legal")
    y = height - 72
    page = 1

    def chrome() -> None:
        c.setFont("Helvetica", 9)
        c.drawString(72, height - 40, "Northwind Mutual - Confidential")
        c.drawCentredString(width / 2, 30, f"Page {page}")

    def new_page() -> None:
        nonlocal y, page
        c.showPage()
        page += 1
        chrome()
        y = height - 72

    def write(text: str, font: str = "Helvetica", size: float = 11, gap: float = 15) -> None:
        nonlocal y
        line = ""
        for word in text.split():
            candidate = f"{line} {word}".strip()
            if line and stringWidth(candidate, font, size) > width - 144:
                if y < 90:
                    new_page()
                c.setFont(font, size)
                c.drawString(72, y, line)
                y -= gap
                line = word
            else:
                line = candidate
        if line:
            if y < 90:
                new_page()
            c.setFont(font, size)
            c.drawString(72, y, line)
            y -= gap

    def heading(text: str) -> None:
        nonlocal y
        y -= 8
        write(text, "Helvetica-Bold", 14, 20)

    chrome()
    write("Service Agreement", "Helvetica-Bold", 22, 28)
    write("Between Northwind Mutual Insurance Company and Acme Auto Repairs Ltd.")
    heading("1. Term")
    write(
        "This agreement starts on 1 March 2026 and runs for 24 months. It renews automatically for further "
        "12-month periods unless either party gives notice under clause 5."
    )
    heading("2. Services")
    write(
        "Acme repairs insured vehicles referred by Northwind adjusters at its 38 certified body shops, and "
        "provides courtesy cars and towing on request."
    )
    heading("3. Fees")
    write("Northwind pays Acme the following fees, excluding sales tax:")
    rows = [
        ["Service", "Fee (USD)"],
        ["Diagnostic inspection", "95"],
        ["Labour, per hour", "112"],
        ["Courtesy car, per day", "38"],
        ["Towing within 25 miles", "140"],
    ]
    top, row_h, cols = y - 4, 18, [72, 312, 472]
    for i in range(len(rows) + 1):
        c.line(cols[0], top - i * row_h, cols[-1], top - i * row_h)
    for x in cols:
        c.line(x, top, x, top - len(rows) * row_h)
    c.setFont("Helvetica", 10)
    for r, row in enumerate(rows):
        for col, value in enumerate(row):
            c.drawString(cols[col] + 4, top - r * row_h - 13, value)
    y = top - len(rows) * row_h - 24
    heading("4. Service levels")
    write(
        "Acme acknowledges each referral within 4 business hours and gives a written repair estimate within "
        "2 business days of seeing the vehicle. Every repair carries a 36-month workmanship warranty."
    )
    new_page()
    heading("5. Termination")
    write(
        "Either party may terminate this agreement for convenience with 90 days' written notice. Northwind may "
        "terminate immediately if Acme loses its state repair licence."
    )
    heading("6. Liability")
    write(
        "Each party's total liability under this agreement is capped at 2,000,000 USD a year, except for fraud "
        "or wilful misconduct, which are not capped."
    )
    heading("7. Notices")
    write(
        "Notices to Northwind go to legal@northwindmutual.example; notices to Acme go to contracts@acmerepairs.example."
    )
    c.showPage()
    c.save()
    return buf.getvalue()


# ----------------------------------------------------------------------------- CSV: adjuster roster

ROSTER = [
    ["ADJ-1042", "Maria Okafor", "North America", "Senior Adjuster", "NY, NJ, CT", "English, Igbo", 45],
    ["ADJ-1107", "Daniel Reyes", "North America", "Field Adjuster", "TX, OK", "English, Spanish", 60],
    ["ADJ-1188", "Priya Natarajan", "North America", "Claims Manager", "CA, NV, AZ", "English, Tamil", 30],
    ["ADJ-1402", "Grace Whitfield", "North America", "Field Adjuster", "FL, GA", "English", 62],
    ["ADJ-1455", "Omar Siddiqui", "North America", "Senior Adjuster", "IL, WI, MN", "English, Urdu, Arabic", 38],
    ["ADJ-1499", "Rosa Delgado", "North America", "Claims Associate", "AZ, NM", "Spanish, English", 70],
    ["ADJ-1215", "Thomas Becker", "EMEA", "Field Adjuster", "Germany, Austria", "German, English", 55],
    ["ADJ-1290", "Chloé Martin", "EMEA", "Senior Adjuster", "France, Belgium", "French, English, Dutch", 40],
    ["ADJ-1333", "Sven Lindqvist", "EMEA", "Claims Associate", "Sweden", "Swedish, English", 80],
    ["ADJ-1376", "Fatima Haddad", "EMEA", "Field Adjuster", "UAE", "Arabic, English, French", 50],
    ["ADJ-2104", "Kenji Watanabe", "APAC", "Senior Adjuster", "Japan", "Japanese, English", 42],
    ["ADJ-2330", "Yuki Sato", "APAC", "Field Adjuster", "Japan", "Japanese", 55],
    ["ADJ-2151", "Mei Lin Tan", "APAC", "Field Adjuster", "Singapore, Malaysia", "English, Mandarin, Malay", 58],
    ["ADJ-2217", "Arjun Mehta", "APAC", "Claims Manager", "India", "Hindi, English, Gujarati", 28],
    ["ADJ-2263", "Siosaia Tupou", "APAC", "Field Adjuster", "New Zealand, Tonga", "English, Tongan", 52],
    ["ADJ-2298", "Hana Kim", "APAC", "Claims Associate", "South Korea", "Korean, English", 75],
    ["ADJ-3011", "Lucas Ferreira", "LATAM", "Senior Adjuster", "Brazil", "Portuguese, Spanish, English", 44],
    ["ADJ-3058", "Valentina Rojas", "LATAM", "Field Adjuster", "Chile, Peru", "Spanish, English", 57],
    ["ADJ-3092", "Diego Hernández", "LATAM", "Claims Manager", "Mexico", "Spanish, English", 26],
    ["ADJ-3127", "Ana Souza", "LATAM", "Claims Associate", "Brazil", "Portuguese", 78],
]


def roster_csv() -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["Adjuster ID", "Name", "Region", "Role", "Licensed in", "Languages", "Max open claims"])
    writer.writerows(ROSTER)
    return buf.getvalue().encode()


# ----------------------------------------------------------------------------- text: release notes

RELEASE_NOTES = """ClaimsCenter release notes

Version 8.4.0 - released 2026-09-22
- CC-4821: Duplicate payments could be issued when an adjuster double-clicked "Approve". Fixed.
- CC-4877: The DriveWise telematics score now shows on the auto claim summary.
- CC-4902: The FNOL web form now accepts photos up to 25 MB each (the limit was 10 MB).

Version 8.3.2 - released 2026-08-30
- CC-4790: Fixed a timeout when exporting more than 10,000 claims to CSV.
- CC-4795: The SIU referral button is hidden on closed claims.

Version 8.3.0 - released 2026-08-04
- CC-4702: Every claim shows a fraud score from the Shift model; a score above 80 triggers an automatic SIU review.
- CC-4733: Claims Managers can bulk-reassign claims between adjusters.

Known issues
- CC-4911: The reserve history chart does not render in Safari 17. Workaround: use Chrome or Edge.
"""

# ----------------------------------------------------------------------------- Mermaid: FNOL intake flow

FNOL_FLOW = """---
title: FNOL intake flow
---
flowchart TD
    A[Customer reports a loss] --> B{Anyone injured?}
    B -- Yes --> C[Route to the Bodily Injury team]
    B -- No --> D{Estimated damage under 2,000 USD?}
    D -- Yes --> E[Fast-track: photo estimate, paid within 48 hours]
    D -- No --> F[Assign a field adjuster]
    F --> G{Fraud score above 80?}
    G -- Yes --> H[Refer to SIU]
    G -- No --> I[Inspect and settle]
"""

# ----------------------------------------------------------------------------- Visio: payment approval


def payment_approval_vsdx() -> bytes:
    import vsdx
    from vsdx import VisioFile
    from vsdx.connectors import Connect

    media = os.path.join(os.path.dirname(vsdx.__file__), "media", "media.vsdx")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "flow.vsdx")
        out = os.path.join(tmp, "flow_out.vsdx")
        shutil.copy(media, src)
        with contextlib.redirect_stdout(io.StringIO()), VisioFile(src) as vis:
            page = vis.pages[0]
            by = {s.ID: s for s in page.all_shapes}
            by["1"].text = "Payment request raised"
            by["2"].text = "Finance review"
            by["3"].text = "submit"
            by["5"].text = ""
            by["7"].text = "Release payment"
            by["8"].text = "Payments over 50,000 USD need CFO sign-off"
            Connect.create(page, by["2"], by["7"]).text = "approved"
            vis.save_vsdx(out)
        return Path(out).read_bytes()


# ----------------------------------------------------------------------------- build

BUILDERS = {
    "travel_expense_policy.docx": expense_policy_docx,
    "auto_premium_rates_2026.xlsx": premium_rates_xlsx,
    "incident_response_runbook.html": lambda: RUNBOOK_HTML.encode(),
    "q3_2026_business_review.pptx": business_review_pptx,
    "acme_repairs_service_agreement.pdf": vendor_agreement_pdf,
    "adjuster_roster.csv": roster_csv,
    "claimscenter_release_notes.txt": lambda: RELEASE_NOTES.encode(),
    "fnol_intake_flow.mmd": lambda: FNOL_FLOW.encode(),
    "payment_approval.vsdx": payment_approval_vsdx,
}


def main() -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    for name, build in BUILDERS.items():
        (CORPUS / name).write_bytes(build())
        print(f"wrote {CORPUS / name}")


if __name__ == "__main__":
    main()
