"""Section 3 required exhibit: product/program table with status and key metric.

Rows come from the 10-K Item 1 segment paragraphs (the lists after "include", "including"
and "such as", plus military designations such as F-35 or PAC-3). Status is the production /
development / sustainment language of the sentence or paragraph that names the product,
else "described in 10-K". The key metric is a counted quantity in that paragraph, the latest
earnings release or the latest transcript's prepared remarks (cited when used). Every row
cites the 10-K; numeric metrics become Claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from primer.sections import text_tools as tt
from primer.sections.common import claim, md_table
from primer.state import Claim

LIST_LEAD = re.compile(r"\b(?:includes?|including|such as)\b\s+(?:the\s+)?([^.;]+)", re.I)
SPLIT = re.compile(r",\s*|\s+and\s+|\s+as well as\s+|\s+or\s+")
STATUS_WORDS = ("production", "development", "sustainment", "modernization", "upgrade")
GENERIC = {
    "major",
    "our",
    "the",
    "these",
    "other",
    "certain",
    "various",
    "u.s.",
    "united",
    "department",
    "government",
    "item",
    "note",
    "lorem",
}
METRIC_RE = re.compile(
    r"\b(\d[\d,]*)\s+((?:[A-Z][\w\-]*\s+){0,2}(?:aircraft|deliveries|units|missiles|interceptors|"
    r"satellites|helicopters|rockets|launches|vehicles|ships)\b|per year|a year)",
    re.I,
)
ABBR = re.compile(r"\(([A-Z]{2,6})\)")


@dataclass
class ProductRow:
    segment: str
    product: str
    status: str
    metric: str
    metric_value: float | None
    source_ids: list[str] = field(default_factory=list)


@dataclass
class ProductExhibit:
    markdown: str
    claims: list[Claim]
    rows: list[ProductRow]
    present: bool
    notes: list[str]


def _paragraphs(text: str, min_len: int = 80) -> list[str]:
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if len(p.strip()) >= min_len]


def segment_abbreviations(item1: str, segments: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for s in segments:
        m = re.search(re.escape(s) + r"\s*\(([A-Z]{2,6})\)", item1)
        out[s] = m.group(1) if m else None
    return out


def segment_paragraphs(item1: str, segments: list[str]) -> dict[str, str]:
    """The Item 1 paragraph describing each segment: one that opens with the segment name
    or its abbreviation, else the first paragraph naming it outside the segment-list sentence."""
    paras = _paragraphs(item1)
    abbr = segment_abbreviations(item1, segments)
    out: dict[str, str] = {}
    for s in segments:
        keys = [s] + ([abbr[s]] if abbr.get(s) else [])
        pat = r"^(?:The\s+|Our\s+)?(?:" + "|".join(re.escape(k) for k in keys) + r")\b"
        chosen = next((p for p in paras if re.match(pat, p)), None)
        if chosen is None:
            chosen = next(
                (
                    p
                    for p in paras
                    if any(re.search(r"\b" + re.escape(k) + r"\b", p) for k in keys)
                    and not re.search(r"segments?\s*[:\-–—]", p)
                ),
                None,
            )
        if chosen:
            out[s] = chosen
    return out


def _clean_item(raw: str) -> str | None:
    s = raw.strip(" .,;:()")
    # a nested lead inside one list item: "precision strike weapons such as GMLRS" -> "GMLRS"
    s = re.split(r"\b(?:such as|including|includes?)\b\s+", s, flags=re.I)[-1]
    s = re.sub(r"^(?:the|our|its|a|an)\s+", "", s, flags=re.I)
    toks = s.split()
    keep: list[str] = []
    for t in toks:
        if re.match(r"^[A-Z0-9]", t) or re.fullmatch(r"(?:II|III|IV|V|VI|D5|MSE)", t):
            keep.append(t.strip(",."))
        else:
            break
        if len(keep) >= 5:
            break
    if not keep or keep[0].lower() in GENERIC:
        return None
    name = " ".join(keep)
    if len(name) < 3 or not re.search(r"[A-Za-z]", name):
        return None
    if all(w.lower() in tt.STOP for w in name.lower().split()):
        return None
    return name


def products_in(paragraph: str) -> list[str]:
    """Capitalized product/program names from the lists in a segment paragraph."""
    out: list[str] = []
    for m in LIST_LEAD.finditer(paragraph):
        for raw in SPLIT.split(m.group(1)):
            name = _clean_item(raw)
            if name and name not in out:
                out.append(name)
    for d in tt.program_designations(paragraph):
        if not any(d in o for o in out):
            out.append(d)
    return out[:8]


def _status(paragraph: str, product: str) -> str:
    sent = next((s for s in tt.sentences(paragraph) if product in s), paragraph)
    for scope in (sent, paragraph):
        found = [w for w in STATUS_WORDS if w in scope.lower()]
        if found:
            return " / ".join(found)
    return "described in 10-K"


def _keys(product: str) -> list[str]:
    """Strings that identify the product in a sentence: the full name, then the designation
    (a first token with a digit or all caps, e.g. F-35, THAAD), else the first two words."""
    toks = product.split()
    keys = [product]
    first = toks[0]
    if re.search(r"\d", first) or (first.isupper() and len(first) >= 3):
        keys.append(first)
    elif len(toks) >= 2:
        keys.append(" ".join(toks[:2]))
    return keys


def _metric(product: str, texts: list[tuple[str, str]]) -> tuple[str, float | None, str | None]:
    """(metric text, numeric value, source id) from the first text whose sentence names the
    product and carries a counted quantity."""
    keys = _keys(product)
    for text, sid in texts:
        if not text:
            continue
        for s in tt.sentences(text):
            if not any(k in s for k in keys):
                continue
            m = METRIC_RE.search(s)
            if m:
                val = float(m.group(1).replace(",", ""))
                return f"{m.group(0).strip()} ({s[:140].rstrip('.')}...)", val, sid
    return "", None, None


def product_table(
    item1: str,
    k_sid: str,
    segments: list[str],
    release: tuple[str, str | None] = ("", None),
    remarks: tuple[str, str | None] = ("", None),
    fy_label: str = "",
    section: int = 3,
) -> ProductExhibit:
    if not item1 or not k_sid:
        return ProductExhibit("", [], [], False, ["no 10-K Item 1 text"])
    paras = segment_paragraphs(item1, segments)
    rows: list[ProductRow] = []
    seen: set[str] = set()
    for seg in segments:
        p = paras.get(seg)
        if not p:
            continue
        for prod in products_in(p):
            if prod.lower() in seen:
                continue
            seen.add(prod.lower())
            metric, val, msid = _metric(prod, [(p, k_sid), release, remarks])
            sids = [k_sid] + ([msid] if msid and msid != k_sid else [])
            rows.append(ProductRow(seg, prod, _status(p, prod), metric, val, sids))
    # designations named in Item 1 outside the segment paragraphs
    for d in tt.program_designations(item1):
        if d.lower() in seen or any(d in r.product for r in rows):
            continue
        seg = next((s for s, p in paras.items() if d in p), "n/a")
        para = paras.get(seg, "")
        metric, val, msid = _metric(d, [(para, k_sid), release, remarks])
        sids = [k_sid] + ([msid] if msid and msid != k_sid else [])
        rows.append(
            ProductRow(seg, d, _status(para, d) if para else "described in 10-K", metric, val, sids)
        )
        seen.add(d.lower())
    rows = rows[:24]
    if not rows:
        return ProductExhibit("", [], [], False, ["no products or programs parsed from Item 1"])
    claims: list[Claim] = []
    table_rows: list[list[str]] = []
    for r in rows:
        cites = "".join(f" [{s}]" for s in r.source_ids)
        metric_cell = (
            f"{r.metric}{' [' + r.source_ids[-1] + ']' if len(r.source_ids) > 1 else ''}"
            if r.metric
            else "n/a"
        )
        table_rows.append([r.segment, r.product, r.status, metric_cell, cites.strip()])
        if r.metric_value is not None:
            c = claim(
                section,
                f"product_key_metric_{re.sub(r'[^a-z0-9]+', '_', r.product.lower()).strip('_')}",
                r.metric_value,
                fy_label,
                r.source_ids[-1:],
                unit="count",
                concept="key metric named in the text",
                note=r.metric[:160],
            )
            if c:
                claims.append(c)
    md = md_table(
        ["Segment", "Product or program", "Status / role (10-K language)", "Key metric", "Source"],
        table_rows,
        align_right_from=5,
    )
    return ProductExhibit(md, claims, rows, True, [])
