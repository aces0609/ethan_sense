"""PLAN 3.6: domain policy in the Firecrawl client (deny, limited, robots) and its run.json summary."""

from __future__ import annotations

import json
from datetime import date

import pytest
import respx
from httpx import Response

from primer.clients import robots
from primer.clients.edgar import EdgarClient
from primer.clients.firecrawl import FirecrawlClient, FirecrawlError
from primer.config import domain_policy, output_root
from primer.evidence.store import EvidenceStore
from primer.render.markdown import source_line
from primer.runlog import RunLog
from primer.sections.common import SectionBudget, ToolContext, evidence_digest, web_research
from primer.state import Entity
from tests.conftest import FirecrawlRouter, _hit

PUBLIC = "https://www.defensenews.com/industry/2026/10/20/lockheed-wins-award/"
PRIVATE = "https://www.defensenews.com/members/2026/10/20/lockheed-inside-story/"
LIMITED = "https://www.reuters.com/business/aerospace-defense/lockheed-wins-award-2026-10-20/"
DENIED = "https://www.bloomberg.com/news/articles/2026-10-20/lockheed-wins-award"
BODY = "SECRET PAYWALLED BODY: Lockheed Martin will receive $9.9 billion over five years."
ROBOTS_TXT = "User-agent: *\nDisallow: /members/\n"


@pytest.fixture(autouse=True)
def _fresh_robots_memo():
    robots.reset_memo()
    yield
    robots.reset_memo()


def _world(router: FirecrawlRouter):
    """A respx world: Firecrawl answered by `router`, defensenews.com publishing ROBOTS_TXT.
    Any other host (reuters, bloomberg) has no route: a robots or page request there would raise."""
    mock = respx.mock(assert_all_called=False, assert_all_mocked=True)
    mock.route(host="api.firecrawl.dev").mock(side_effect=router)
    robots_route = mock.get("https://www.defensenews.com/robots.txt").mock(
        return_value=Response(200, text=ROBOTS_TXT)
    )
    return mock, robots_route


def _client(tmp_path, **kw):
    store = EvidenceStore(tmp_path / "run", as_of=date(2026, 10, 21))
    log = RunLog(tmp_path / "run")
    return FirecrawlClient(store, runlog=log, api_key="fc-test", **kw), store, log


def _page(title: str, body: str = "Lockheed Martin news. " * 20) -> str:
    return f"# {title}\n\n{body}\n"


# ---- robots ----------------------------------------------------------------------------------


def test_disallowed_url_is_not_requested_and_is_not_an_error(tmp_path):
    router = FirecrawlRouter(pages={"defensenews.com": _page("Lockheed wins award")})
    mock, robots_route = _world(router)
    fc, store, log = _client(tmp_path)
    with mock:
        blocked = fc.scrape(PRIVATE, type="news")
        ok = fc.scrape(PUBLIC, type="news")
        assert robots_route.call_count == 1  # one robots.txt fetch for the host
    # the disallowed page never reached Firecrawl; the allowed one did
    assert [c for c in router.calls if c[0] == "scrape"] == [("scrape", PUBLIC)]
    assert blocked["source_id"] is None and blocked["markdown"] == ""
    assert "disallowed" in blocked["blocked"]
    assert ok["source_id"] and store.get(ok["source_id"]).url == PUBLIC
    assert log.counters["firecrawl_robots_blocked"] == 1
    assert log.errors == []
    run = json.loads(log.write().read_text(encoding="utf-8"))
    assert run["meta"]["blocked"] == [
        {"url": PRIVATE, "reason": blocked["blocked"], "host": "www.defensenews.com"}
    ]
    assert run["meta"]["policy"] == {
        "allowed": 1,
        "limited": 0,
        "denied": 0,
        "robots_blocked": 1,
    }
    assert [r.url for r in store.records()] == [PUBLIC]


def test_robots_blocked_page_does_not_fail_the_section_helpers(tmp_path):
    """scrape_url / web_research treat a blocked page as 'no source', not as an error."""
    router = FirecrawlRouter(pages={"defensenews.com": _page("x")})
    mock, _ = _world(router)
    fc, store, log = _client(tmp_path)
    ctx = _ctx(tmp_path, fc, store, log)
    with mock:
        from primer.sections.common import scrape_url

        assert scrape_url(ctx, PRIVATE) is None
    assert ctx.budget.exhausted is None
    assert log.counters["firecrawl_robots_blocked"] == 1


def test_search_with_scrape_drops_disallowed_and_denied_hits(tmp_path):
    router = FirecrawlRouter(
        search={
            "lockheed award": [
                {**_hit(PUBLIC, "Public", "d1"), "markdown": _page("Public")},
                {**_hit(PRIVATE, "Members only", "d2"), "markdown": _page("Members only")},
                {**_hit(DENIED, "Bloomberg", "d3"), "markdown": _page("Bloomberg", BODY)},
            ]
        }
    )
    mock, _ = _world(router)
    fc, store, log = _client(tmp_path)
    with mock:
        rows = fc.search("lockheed award", scrape=True)
    assert [r["url"] for r in rows] == [PUBLIC]
    assert rows[0]["source_id"]
    assert [r.url for r in store.records()] == [PUBLIC]
    assert log.counters["firecrawl_robots_blocked"] == 1
    assert log.meta["policy"] == {"allowed": 1, "limited": 0, "denied": 1, "robots_blocked": 1}


def test_offline_replay_reproduces_the_block_from_the_cached_robots_txt(tmp_path):
    router = FirecrawlRouter(pages={"defensenews.com": _page("x")})
    mock, _ = _world(router)
    fc, _, _ = _client(tmp_path)
    with mock:
        assert fc.scrape(PRIVATE)["blocked"]
    robots.reset_memo()
    fc2, _, log2 = _client(tmp_path / "again", offline=True)
    with respx.mock(assert_all_called=False, assert_all_mocked=True):  # any request would raise
        res = fc2.scrape(PRIVATE)
    assert res["source_id"] is None and "disallowed" in res["blocked"]
    assert log2.counters["firecrawl_robots_blocked"] == 1


# ---- deny ------------------------------------------------------------------------------------


def test_denied_hosts_are_never_requested(tmp_path):
    router = FirecrawlRouter(pages={"bloomberg.com": _page("Bloomberg", BODY)})
    mock, _ = _world(router)
    fc, store, log = _client(tmp_path)
    with mock, pytest.raises(FirecrawlError, match="denied by domain policy"):
        fc.scrape(DENIED)
    assert router.calls == []
    assert store.records() == []
    assert domain_policy("https://markets.ft.com/data") == "deny"  # subdomains too
    assert log.meta["policy"]["denied"] == 1


def test_denied_search_hits_are_dropped_from_web_research(tmp_path):
    router = FirecrawlRouter(
        search={
            "lockheed award": [
                _hit(DENIED, "Bloomberg: Lockheed wins award", "bloomberg excerpt"),
                _hit("https://www.ft.com/content/abc", "FT", "ft excerpt"),
                _hit("https://seekingalpha.com/article/1", "SA", "sa excerpt"),
                _hit(PUBLIC, "Public", "public excerpt"),
            ]
        },
        pages={"defensenews.com": _page("Public")},
    )
    mock, _ = _world(router)
    fc, store, log = _client(tmp_path)
    ctx = _ctx(tmp_path, fc, store, log)
    with mock:
        rows = web_research(ctx, ["lockheed award"])
    assert [r["url"] for r in rows] == [PUBLIC]
    scraped = [c[1] for c in router.calls if c[0] == "scrape"]
    assert scraped == [PUBLIC]
    assert log.meta["policy"]["denied"] == 3


# ---- limited ---------------------------------------------------------------------------------


def _limited_router() -> FirecrawlRouter:
    return FirecrawlRouter(
        pages={"reuters.com": _page("Lockheed wins award", BODY)},
        search={
            "lockheed award": [
                {
                    **_hit(LIMITED, "Lockheed wins award", f"{BODY} excerpt"),
                    "markdown": _page("Lockheed wins award", BODY),
                }
            ]
        },
    )


def _allow_reuters_robots(mock):
    mock.get("https://www.reuters.com/robots.txt").mock(
        return_value=Response(200, text="User-agent: *\nAllow: /\n")
    )


def test_limited_page_is_fetched_but_only_headline_and_url_are_kept(tmp_path):
    router = _limited_router()
    mock, _ = _world(router)
    _allow_reuters_robots(mock)
    fc, store, log = _client(tmp_path)
    with mock:
        res = fc.scrape(LIMITED, type="news")
    assert any(c == ("scrape", LIMITED) for c in router.calls)  # fetched
    rec = store.get(res["source_id"])
    assert rec.access == "limited" and rec.url == LIMITED
    assert "Lockheed wins award" in rec.title
    assert BODY not in store.text(rec.source_id)
    assert "access: limited" in store.text(rec.source_id)
    # the body does not come back to the caller either
    assert res["markdown"] == "" and res["json"] is None and res["access"] == "limited"
    assert BODY not in json.dumps(res)
    # nor is it written to the Firecrawl disk cache
    cached = "".join(
        p.read_text(encoding="utf-8") for p in (output_root() / ".cache").rglob("*.json")
    )
    assert "SECRET PAYWALLED BODY" not in cached
    # the Sources appendix flags it
    assert source_line(1, rec).endswith("(access: limited)") and LIMITED in source_line(1, rec)
    assert log.meta["policy"]["limited"] == 1


def test_limited_search_hit_has_no_excerpt_and_no_body(tmp_path):
    router = _limited_router()
    mock, _ = _world(router)
    _allow_reuters_robots(mock)
    fc, store, log = _client(tmp_path)
    with mock:
        rows = fc.search("lockheed award", scrape=True)
        again = fc.search("lockheed award", scrape=True)  # served from the (scrubbed) cache
    for out in (rows, again):
        assert len(out) == 1 and out[0]["title"] == "Lockheed wins award"
        assert out[0]["description"] == ""
        assert BODY not in json.dumps(out)
        rec = store.get(out[0]["source_id"])
        assert rec.access == "limited" and BODY not in store.text(rec.source_id)
    cached = "".join(
        p.read_text(encoding="utf-8") for p in (output_root() / ".cache").rglob("*.json")
    )
    assert "SECRET PAYWALLED BODY" not in cached


def test_limited_content_never_reaches_evidence_digest(tmp_path):
    router = _limited_router()
    mock, _ = _world(router)
    _allow_reuters_robots(mock)
    fc, store, log = _client(tmp_path)
    ctx = _ctx(tmp_path, fc, store, log)
    with mock:
        rows = web_research(ctx, ["lockheed award"], prefer_domains=("reuters.com",))
    sid = rows[0]["source_id"]
    digest = evidence_digest(ctx, [sid], keywords=["billion", "five years"])
    assert f"[{sid}] Lockheed wins award" in digest  # the headline is citable
    assert "SECRET PAYWALLED BODY" not in digest and "$9.9 billion" not in digest
    assert "access: limited" in digest


# ---- summary ---------------------------------------------------------------------------------


def test_policy_summary_counts_each_url_once(tmp_path):
    router = _limited_router()
    router.pages["defensenews.com"] = _page("Public")
    mock, _ = _world(router)
    _allow_reuters_robots(mock)
    fc, store, log = _client(tmp_path)
    with mock:
        fc.scrape(PUBLIC)
        fc.scrape(PUBLIC)
        fc.scrape(LIMITED)
        fc.scrape(PRIVATE)
        with pytest.raises(FirecrawlError):
            fc.scrape(DENIED)
    assert log.meta["policy"] == {"allowed": 1, "limited": 1, "denied": 1, "robots_blocked": 1}
    assert [b["url"] for b in log.meta["blocked"]] == [PRIVATE]


def test_agent_urls_are_filtered_before_the_request(tmp_path):
    seen: list[dict] = []

    router = FirecrawlRouter(agents={"list deals": {"ok": True}})

    def agent(request):
        seen.append(json.loads(request.content))
        return router(request)

    mock, _ = _world(router)
    mock.route(host="api.firecrawl.dev").mock(side_effect=agent)  # replaces the plain router
    fc, _, log = _client(tmp_path)
    with mock:
        assert fc.agent("list deals", {"type": "object"}, urls=[PUBLIC, PRIVATE, DENIED]) == {
            "ok": True
        }
        assert fc.agent("list deals again", {"type": "object"}, urls=[PRIVATE, DENIED]) == {}
    assert seen[0]["urls"] == [PUBLIC] and len(seen) == 1
    assert log.counters["firecrawl_robots_blocked"] == 2


def _ctx(tmp_path, fc, store, log) -> ToolContext:
    return ToolContext(
        section=3,
        run_dir=tmp_path / "run",
        as_of=date(2026, 10, 21),
        entity=Entity(name="LOCKHEED MARTIN CORP", cik="0000936468", aliases=["Lockheed Martin"]),
        foundation={},
        store=store,
        edgar=EdgarClient(runlog=log, offline=True),
        runlog=log,
        budget=SectionBudget.for_section(3),
        firecrawl=fc,
    )
