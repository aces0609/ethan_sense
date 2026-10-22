"""EvidenceStore: register every fetched page or API response before any model sees it.

Layout under the run folder:
  evidence/{source_id}.md     raw markdown or JSON text of the fetch
  evidence/index.json         list of SourceRecord dicts, rewritten on every register
  sources.json                written by the assembler from the cited subset

Registration is idempotent on (url, as_of) within a run, so a page fetched by two
researchers gets one source_id; a second registration of the same URL under another type
or dedupe key reuses the first id rather than creating a duplicate record. The index is
loaded on construction, which is what makes offline replay and checkpoint resume see the
same ids.

Source hygiene applied on registration (review 2026-10-21 §7):
- a missing `published_date` is filled from the URL (`/2026/10/20/`, `2026-10-20`,
  `-26-06-2026`, `lmt-20250416`) or from page metadata the fetcher captured;
- anything under sec.gov/Archives is typed by its form (EX-99 exhibits are press or
  earnings releases, primary documents 8-K / 10-K / 10-Q / DEF14A), never `news`;
- titles and publishers lose embedded newlines, tabs and runs of spaces.
Listing and index pages are refused by the fetcher (`is_index_page`) before they reach
the store.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from primer.config import tier_for_url
from primer.state import SourceRecord

_REGISTRY: dict[str, EvidenceStore] = {}
_REG_LOCK = threading.Lock()

# ---- source hygiene helpers ----------------------------------------------------------------

MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_URL_YMD_SLASH = re.compile(r"(?<!\d)(19[89]\d|20\d{2})/(\d{1,2})/(\d{1,2})(?!\d)")
_URL_ISO = re.compile(r"(?<!\d)(19[89]\d|20\d{2})-(\d{2})-(\d{2})(?!\d)")
_URL_DMY = re.compile(r"(?<!\d)(\d{2})-(\d{2})-(19[89]\d|20\d{2})(?!\d)")
_URL_COMPACT = re.compile(r"(?<!\d)(19[89]\d|20\d{2})(\d{2})(\d{2})(?!\d)")
_META_DATE_KEYS = (
    "publishedTime",
    "publishedDate",
    "published_time",
    "article:published_time",
    "datePublished",
    "dc.date",
    "dc.date.issued",
    "date",
    "pubdate",
    "og:published_time",
    "parsely-pub-date",
    "sailthru.date",
    "modifiedTime",
    "article:modified_time",
    "dateModified",
    "og:updated_time",
)
_TEXT_DATE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(19[89]\d|20\d{2})\b"
    r"|\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(19[89]\d|20\d{2})\b"
    r"|\b(19[89]\d|20\d{2})-(\d{2})-(\d{2})\b"
)


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        out = date(y, m, d)
    except ValueError:
        return None
    if not (1990 <= y <= 2100):
        return None
    return out


def date_from_url(url: str) -> date | None:
    """A publication date embedded in the URL path: `/2026/10/20/`, `2026-10-20`,
    `-26-06-2026` (day-month-year), `lmt-20250416`. None when no segment parses."""
    path = urlparse(url).path
    m = _URL_YMD_SLASH.search(path)
    if m:
        d = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    m = _URL_ISO.search(path)
    if m:
        d = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    m = _URL_DMY.search(path)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d = _valid(y, b, a) or _valid(y, a, b)  # day-month-year first, then month-day-year
        if d:
            return d
    m = _URL_COMPACT.search(path)
    if m:
        d = _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return d
    return None


def parse_loose_date(s: Any) -> date | None:
    """ISO timestamps, 'July 23, 2026', '23 July 2026', '2026-07-23' -> date."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    text = str(s).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    m = _TEXT_DATE.search(text)
    if not m:
        return None
    if m.group(1):
        return _valid(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    if m.group(4):
        return _valid(int(m.group(6)), MONTHS[m.group(5)[:3].lower()], int(m.group(4)))
    return _valid(int(m.group(7)), int(m.group(8)), int(m.group(9)))


def date_from_metadata(meta: dict[str, Any] | None) -> date | None:
    """The first parseable publication date in page metadata (publishedTime,
    article:published_time, datePublished, ...; modified dates last)."""
    if not meta:
        return None
    for key in _META_DATE_KEYS:
        d = parse_loose_date(meta.get(key))
        if d:
            return d
    return None


def date_from_text(content: str, window: int = 800) -> date | None:
    """A dateline in the first lines of a page ('BETHESDA, Md., July 23, 2026 –',
    'Published: Sep 3, 2025', 'GAO-25-107632 (2025-09-03)')."""
    head = content[:window]
    m = _TEXT_DATE.search(head)
    if not m:
        return None
    return parse_loose_date(m.group(0))


def clean_title(s: Any, limit: int = 200) -> str:
    """Collapse embedded newlines, tabs and runs of spaces to one space."""
    return " ".join(str(s or "").split())[:limit].strip()


_GENERIC_TYPES = {"news", "web", "trade_press", "market_data", "rating_agency", "presentation"}
_FORM_IN_TEXT = (
    (re.compile(r"\bFORM\s+10-K(?:/A|T)?\b", re.I), "10-K"),
    (re.compile(r"\bFORM\s+10-Q(?:/A)?\b", re.I), "10-Q"),
    (re.compile(r"\bFORM\s+8-K(?:/A)?\b", re.I), "8-K"),
    (re.compile(r"\bSCHEDULE\s+14A\b|\bPROXY\s+STATEMENT\b|\bDEF\s*14A\b", re.I), "DEF14A"),
    (re.compile(r"\bFORM\s+20-F\b", re.I), "20-F"),
    (re.compile(r"\bSC\s+13[DG](?:/A)?\b|\bSCHEDULE\s+13[DG]\b", re.I), "SC13"),
    (re.compile(r"\bFORM\s+4\b", re.I), "4"),
)
_FORM_META = {
    "10-K": "10-K",
    "10-K/A": "10-K",
    "10-Q": "10-Q",
    "10-Q/A": "10-Q",
    "8-K": "8-K",
    "8-K/A": "8-K",
    "DEF 14A": "DEF14A",
    "DEF14A": "DEF14A",
    "20-F": "20-F",
}
_EARNINGS_WORDS = re.compile(
    r"\b(?:(?:first|second|third|fourth|Q[1-4])[- ](?:quarter|Q)|quarterly|full[- ]year|fiscal[- ]year)\b[^.\n]{0,80}\b(?:results|earnings)\b"
    r"|\breports?\s+(?:first|second|third|fourth)[- ]quarter\b"
    r"|\bearnings\s+release\b",
    re.I,
)


def is_sec_archive(url: str) -> bool:
    p = urlparse(url)
    host = p.netloc.lower()
    return (host == "sec.gov" or host.endswith(".sec.gov")) and "/archives/" in p.path.lower()


def sec_archive_type(
    url: str, given: str, content: str = "", meta: dict[str, Any] | None = None
) -> str:
    """The type for a sec.gov/Archives document: an EX-99 exhibit is `earnings_release`
    when its text reads like a quarterly results release, else `press_release`; a primary
    document is typed by the form named in `meta["form"]` or in its first page; anything
    else is `sec_filing`. The caller's type is kept when it is already a filing type."""
    if given not in _GENERIC_TYPES and given != "press_release":
        return given
    name = urlparse(url).path.rsplit("/", 1)[-1].lower()
    head = content[:6000]
    if re.search(r"ex[-_]?99", name):
        if given == "earnings_release" or _EARNINGS_WORDS.search(head):
            return "earnings_release"
        return "press_release"
    if given == "press_release":
        return given
    form = str((meta or {}).get("form") or "").upper().strip()
    if form in _FORM_META:
        return _FORM_META[form]
    if form.startswith("8-K EX") or re.search(r"ex[-_]?\d{1,3}", name):
        return "press_release" if re.search(r"ex[-_]?99", name) else "sec_exhibit"
    for rx, label in _FORM_IN_TEXT:
        if rx.search(head):
            return label
    return "sec_filing"


_INDEX_PATHS = re.compile(
    r"/(?:(?:news(?:-|_)?releases?(?:-|_)?(?:and-)?(?:statements)?|press(?:-|_)?releases?|"
    r"newsroom|news|media(?:-center|room)?|presentations|events(?:-and-presentations)?|"
    r"investors?|sec-filings)(?:\.aspx|\.html?)?|default\.aspx)/?$",
    re.I,
)
_MD_LINK = re.compile(r"\[[^\]]{1,200}\]\([^)]+\)")


def _norm_url(url: str) -> str:
    """URL identity for duplicate detection: scheme and trailing slash ignored, host lowered."""
    p = urlparse(url.strip())
    host = p.netloc.lower()
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def is_index_page(url: str, markdown: str) -> bool:
    """A listing or index page: the URL is a newsroom / presentations / investors landing
    path with no article segment, or the markdown is mostly links with no article body
    (no paragraph of 300+ characters and links on most non-empty lines)."""
    p = urlparse(url)
    if _INDEX_PATHS.search(p.path.rstrip("/") or "/") and not p.query:
        return True
    lines = [ln.strip() for ln in markdown.splitlines() if ln.strip()]
    if len(lines) < 12:
        return False
    link_lines = sum(1 for ln in lines if _MD_LINK.search(ln))
    n_links = len(_MD_LINK.findall(markdown))
    prose = [
        ln for ln in lines if not _MD_LINK.search(ln) and not ln.startswith(("#", "|", "-", "*"))
    ]
    long_prose = any(len(ln) >= 300 for ln in prose)
    return n_links >= 15 and link_lines >= 0.6 * len(lines) and not long_prose


class EvidenceStore:
    """Use `EvidenceStore.for_run(run_dir)` inside the graph: thirteen researcher threads share
    one instance per run, so source ids are assigned under one lock and never collide."""

    @classmethod
    def for_run(cls, run_dir: str | Path, as_of: date | None = None) -> EvidenceStore:
        key = str(Path(run_dir).resolve())
        with _REG_LOCK:
            store = _REGISTRY.get(key)
            if store is None:
                store = cls(run_dir, as_of=as_of)
                _REGISTRY[key] = store
            elif as_of and store.as_of != as_of:
                store.as_of = as_of
            return store

    def __init__(self, run_dir: str | Path, as_of: date | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.dir = self.run_dir / "evidence"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "index.json"
        self.as_of = as_of or date.today()
        self._records: dict[str, SourceRecord] = {}
        self._by_key: dict[str, str] = {}
        self._by_url: dict[str, str] = {}
        self._lock = threading.RLock()
        self._load()

    # ---- persistence -----------------------------------------------------------------

    def _load(self) -> None:
        if not self.index_path.exists():
            return
        for raw in json.loads(self.index_path.read_text(encoding="utf-8")):
            rec = SourceRecord.model_validate(raw)
            self._records[rec.source_id] = rec
            self._by_key[rec.meta.get("dedupe_key", rec.url)] = rec.source_id
            self._by_url.setdefault(_norm_url(rec.url), rec.source_id)

    def _save(self) -> None:
        data = [r.model_dump(mode="json") for r in self._records.values()]
        self.index_path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    # ---- registration ----------------------------------------------------------------

    def _next_id(self) -> str:
        return f"src_{len(self._records) + 1:04d}"

    def register(
        self,
        content: str,
        *,
        type: str,
        publisher: str,
        title: str,
        url: str,
        published_date: date | None = None,
        tier: int | None = None,
        access: str = "full",
        gov_id: str | None = None,
        meta: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
    ) -> SourceRecord:
        default_key = f"{url}|{self.as_of.isoformat()}"
        key = dedupe_key or default_key
        with self._lock:
            if key in self._by_key:
                return self._records[self._by_key[key]]
            # the same URL registered again under another type or dedupe key reuses the
            # first id (a 10-K fetched by the foundation pack and later by a web search is one
            # source, not a "10-K" and a "news" record). Two explicit keys on one URL stay
            # distinct: API pulls with different filters share an endpoint URL.
            prior = self._by_url.get(_norm_url(url))
            if prior is not None:
                prior_rec = self._records[prior]
                prior_default = prior_rec.meta.get("dedupe_key") == (
                    f"{prior_rec.url}|{self.as_of.isoformat()}"
                )
                if dedupe_key is None or prior_default:
                    return prior_rec
            sid = self._next_id()
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            path = self.dir / f"{sid}.md"
            path.write_text(content, encoding="utf-8")
            if is_sec_archive(url):
                type = sec_archive_type(url, type, content, meta)
            if published_date is None:
                published_date = date_from_metadata(meta) or date_from_url(url)
                if published_date is None and type != "web" and not type.startswith("api:"):
                    published_date = date_from_text(content)
            rec = SourceRecord(
                source_id=sid,
                type=type,
                tier=tier or tier_for_url(url),  # type: ignore[arg-type]
                publisher=clean_title(publisher, 120) or urlparse(url).netloc,
                title=clean_title(title) or url,
                url=url,
                published_date=published_date,
                fetched_at=datetime.now(UTC),
                content_hash=digest,
                evidence_path=str(path.relative_to(self.run_dir)).replace("\\", "/"),
                access=access,  # type: ignore[arg-type]
                gov_id=gov_id,
                meta={**(meta or {}), "dedupe_key": key},
            )
            self._records[sid] = rec
            self._by_key[key] = sid
            self._by_url.setdefault(_norm_url(url), sid)
            self._save()
            return rec

    # ---- access ----------------------------------------------------------------------

    def get(self, source_id: str) -> SourceRecord:
        return self._records[source_id]

    def has(self, source_id: str) -> bool:
        return source_id in self._records

    def text(self, source_id: str) -> str:
        rec = self.get(source_id)
        return (self.run_dir / rec.evidence_path).read_text(encoding="utf-8")

    def records(self) -> list[SourceRecord]:
        with self._lock:
            return list(self._records.values())

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._records.keys())

    def search_text(self, needle: str, source_ids: list[str] | None = None) -> list[str]:
        """Source ids whose evidence text contains `needle` (case-insensitive)."""
        needle = needle.lower()
        out = []
        for sid in source_ids or self.ids():
            if self.has(sid) and needle in self.text(sid).lower():
                out.append(sid)
        return out

    def find(self, **filters: Any) -> list[SourceRecord]:
        out = []
        for r in self._records.values():
            if all(getattr(r, k, None) == v or r.meta.get(k) == v for k, v in filters.items()):
                out.append(r)
        return out

    # ---- sources.json ----------------------------------------------------------------

    def write_sources_json(
        self, cited_by: dict[str, list[str]], extra: dict[str, Any] | None = None
    ) -> Path:
        """Write the cited subset (requirements §7: unused sources are dropped from the appendix)."""
        rows = []
        for sid, sections in cited_by.items():
            if sid not in self._records:
                continue
            rec = self._records[sid].model_copy(update={"cited_by": sorted(set(sections))})
            rows.append(rec.model_dump(mode="json"))
        payload = {"as_of": self.as_of.isoformat(), **(extra or {}), "sources": rows}
        path = self.run_dir / "sources.json"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
