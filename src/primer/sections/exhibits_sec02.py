"""Section 2 required exhibit: the segment revenue table (3 FY) read from the 10-K.

The table is located deterministically (Item 7, then Item 8, then the whole 10-K) with
`text_tools.segment_table`; every cell cites the 10-K source id, every value becomes a
Claim (`segment_revenue_<slug>`, `segment_revenue_total`) in USD (the filing reports USD
millions; the note says so) and a derived share-of-total block follows.

Section 3 reuses `find_segment_table` for the canonical segment names (the table's row
labels, which are cleaner than the prose split in `segment_names_from_text`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from primer.sections import text_tools as tt
from primer.sections.common import ToolContext, claim, md_table
from primer.state import Claim


@dataclass
class SegmentTable:
    table: tt.SummingTable | None
    source_id: str | None
    columns: list[str] = field(default_factory=list)  # "FY2025", "FY2024", ...
    located_in: str = ""  # "Item 7" | "Item 8" | "10-K"
    segment_names: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.table is not None and self.source_id is not None


@dataclass
class SegmentExhibit:
    markdown: str
    claims: list[Claim]
    present: bool
    notes: list[str]
    table: SegmentTable


def slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "segment"


def strip_item_heading(text: str) -> str:
    """Drop the 'Item 1. Business' heading and short title-only lines ('Competition',
    'Human Capital') so sentence splitting starts on prose and headings do not glue onto
    the sentence that follows them."""
    text = re.sub(
        r"^\s*(?:#+\s*)?(?:\*\*)?\s*Item\s+\d{1,2}[A-C]?\s*[.:\-—–]?[^\n]*\n", "", text, count=1
    )
    return re.sub(r"(?m)^(?:#+\s*)?[A-Z][A-Za-z ,&\-]{1,60}\n", "", text)


def dropped_note(dropped: int) -> str | None:
    if dropped <= 0:
        return None
    return f"{dropped} planned web {'query' if dropped == 1 else 'queries'} dropped at the section search cap"


def year_columns(text: str, table: tt.SummingTable, fy: int) -> list[str]:
    """FY labels for the table columns: a header line just above the table that holds
    one year per column wins; then `table.columns` when it is a clean run of distinct
    years; else FY{fy}, FY{fy-1}, ... (the filing's newest year first)."""
    ncol = len(table.total.values)
    lines = text.splitlines()
    for i in range(table.start_line - 1, max(-1, table.start_line - 8), -1):
        years = [int(y) for y in tt.YEAR.findall(lines[i])]
        if len(years) >= ncol and len(set(years[:ncol])) == ncol:
            return [f"FY{y}" for y in years[:ncol]]
    cols = [c for c in table.columns if c.isdigit()]
    if len(cols) == ncol and len(set(cols)) == ncol:
        ys = [int(c) for c in cols]
        if all(abs(ys[i] - ys[i + 1]) == 1 for i in range(ncol - 1)):
            return [f"FY{y}" for y in ys]
    return [f"FY{fy - i}" for i in range(ncol)]


def find_segment_table(ctx: ToolContext) -> SegmentTable:
    """Locate the segment revenue table in the foundation 10-K: Item 7, then Item 8, then
    the whole filing. Returns an empty SegmentTable (with the prose segment names, if any)
    when none is found."""
    item1, k_sid = ctx.item_text("1")
    names = tt.segment_names_from_text(item1) if item1 else []
    tenk = ctx.foundation_sources("10-K")
    candidates: list[tuple[str, str, str | None]] = []
    for item in ("7", "8"):
        text, sid = ctx.item_text(item)
        if text:
            candidates.append((f"Item {item}", text, sid))
    if tenk:
        candidates.append(("10-K", ctx.store.text(tenk[0]), tenk[0]))
    if not names:
        # an IPO prospectus names its segments in MD&A or the notes, not in the business item
        for _where, text, _sid in candidates:
            names = tt.segment_names_from_text(text)
            if names:
                break
    for where, text, sid in candidates:
        if not sid:
            continue
        t = tt.segment_table(text, names or None)
        if t is None and names:
            t = tt.segment_table(text, None)
        if t is not None:
            return SegmentTable(
                table=t,
                source_id=sid,
                columns=year_columns(text, t, ctx.fy),
                located_in=where,
                segment_names=[r.label for r in t.rows],
            )
    return SegmentTable(
        table=None, source_id=k_sid or (tenk[0] if tenk else None), segment_names=names
    )


def segment_revenue_exhibit(ctx: ToolContext, section: int = 2) -> SegmentExhibit:
    st = find_segment_table(ctx)
    if not st.present or st.table is None:
        note = (
            "no segment revenue table found in the 10-K (Item 7, Item 8 or the full text)"
            if ctx.foundation_sources("10-K")
            else "no 10-K in the foundation pack"
        )
        return SegmentExhibit("", [], False, [note], st)
    t, sid, cols = st.table, st.source_id, st.columns
    assert sid is not None
    cite = f" [{sid}]"
    claims: list[Claim] = []
    rows: list[list[str]] = []
    note = f"reported in USD millions in the 10-K {st.located_in} segment table; value x 1e6"
    for r in t.rows:
        cells = [r.label]
        for j, col in enumerate(cols):
            v = r.values[j]
            cells.append(f"{v:,.0f}{cite}")
            c = claim(
                section,
                f"segment_revenue_{slug(r.label)}",
                v * 1e6,
                col,
                [sid],
                unit="USD",
                concept="10-K segment table",
                note=note,
            )
            if c:
                claims.append(c)
        rows.append(cells)
    total_cells = [f"**{t.total.label}**"]
    for j, col in enumerate(cols):
        v = t.total.values[j]
        total_cells.append(f"**{v:,.0f}**{cite}")
        c = claim(
            section,
            "segment_revenue_total",
            v * 1e6,
            col,
            [sid],
            unit="USD",
            concept="10-K segment table",
            note=note + "; consolidated total of the segment table",
        )
        if c:
            claims.append(c)
    rows.append(total_cells)
    for r in t.rows:
        cells = [f"{r.label} share of total — derived: {r.label} / {t.total.label}"]
        for j, col in enumerate(cols):
            tot = t.total.values[j]
            share = (r.values[j] / tot * 100) if tot else None
            cells.append("n/a" if share is None else f"{share:.1f}%")
            c = claim(
                section,
                f"segment_revenue_share_{slug(r.label)}_pct",
                share,
                col,
                [sid],
                unit="pct",
                concept="derived",
                note=f"segment_revenue_{slug(r.label)} / segment_revenue_total x 100",
            )
            if c:
                claims.append(c)
        rows.append(cells)
    md = md_table(["Segment (USD millions)", *cols], rows)
    return SegmentExhibit(md, claims, True, [], st)
