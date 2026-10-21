"""EvidenceStore: register every fetched page or API response before any model sees it.

Layout under the run folder:
  evidence/{source_id}.md     raw markdown or JSON text of the fetch
  evidence/index.json         list of SourceRecord dicts, rewritten on every register
  sources.json                written by the assembler from the cited subset

Registration is idempotent on (url, as_of) within a run, so a page fetched by two
researchers gets one source_id. The index is loaded on construction, which is what makes
offline replay and checkpoint resume see the same ids.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from primer.config import tier_for_url
from primer.state import SourceRecord

_REGISTRY: dict[str, EvidenceStore] = {}
_REG_LOCK = threading.Lock()


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
        key = dedupe_key or f"{url}|{self.as_of.isoformat()}"
        with self._lock:
            if key in self._by_key:
                return self._records[self._by_key[key]]
            sid = self._next_id()
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            path = self.dir / f"{sid}.md"
            path.write_text(content, encoding="utf-8")
            rec = SourceRecord(
                source_id=sid,
                type=type,
                tier=tier or tier_for_url(url),  # type: ignore[arg-type]
                publisher=publisher,
                title=title,
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
