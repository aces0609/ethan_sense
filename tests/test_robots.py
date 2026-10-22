"""PLAN 3.6: robots.txt policy (allow, deny, 404, fetch error, skipped hosts, offline replay)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import Response

from primer.clients import robots
from primer.clients.cache import DiskCache
from primer.clients.robots import record_policy, robots_allowed
from primer.config import output_root
from primer.runlog import RunLog

HOST = "https://news.example.com"
ROBOTS = "User-agent: *\nDisallow: /private/\nDisallow: /subscribers\n"


@pytest.fixture(autouse=True)
def _fresh_robots_memo():
    robots.reset_memo()
    yield
    robots.reset_memo()


def test_allowed_and_disallowed_paths():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=ROBOTS))
        ok, reason = robots_allowed(f"{HOST}/2026/10/21/story.html")
        assert ok and "allowed" in reason
        ok, reason = robots_allowed(f"{HOST}/private/draft")
        assert not ok and "disallowed" in reason and "robots.txt" in reason
        ok, _ = robots_allowed(f"{HOST}/subscribers?page=2")
        assert not ok
        # three URLs on one host, one robots.txt request
        assert route.call_count == 1


def test_fetch_identifies_as_ethan_sense_with_contact_email():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=ROBOTS))
        robots_allowed(f"{HOST}/a")
        ua = route.calls.last.request.headers["user-agent"]
    assert ua.startswith("ethan_sense/0.1 (+") and "tests@example.com" in ua


def test_specific_token_group_beats_wildcard():
    text = "User-agent: *\nDisallow: /\n\nUser-agent: ethan_sense\nDisallow: /nope/\nAllow: /\n"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=text))
        assert robots_allowed(f"{HOST}/page")[0]
        assert not robots_allowed(f"{HOST}/nope/x")[0]


def test_404_means_allow():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{HOST}/robots.txt").mock(return_value=Response(404))
        ok, reason = robots_allowed(f"{HOST}/anything")
    assert ok and "no robots.txt" in reason


@pytest.mark.parametrize("failure", ["connect", "server_error"])
def test_fetch_error_allows_and_counts_a_warning(tmp_path, failure):
    log = RunLog(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        if failure == "connect":
            mock.get(f"{HOST}/robots.txt").mock(side_effect=httpx.ConnectError("boom"))
        else:
            mock.get(f"{HOST}/robots.txt").mock(return_value=Response(503))
        ok, reason = robots_allowed(f"{HOST}/private/x", runlog=log)
        assert ok and "fetch failed" in reason
        # remembered in-process: a second URL on the dead host does not retry or recount
        robots_allowed(f"{HOST}/private/y", runlog=log)
    assert log.counters["robots_fetch_errors"] == 1


def test_tier1_and_skip_hosts_are_not_checked(monkeypatch):
    with respx.mock(assert_all_called=False) as mock:
        sec = mock.get("https://www.sec.gov/robots.txt").mock(
            return_value=Response(200, text=ROBOTS)
        )
        gao = mock.get("https://www.gao.gov/robots.txt").mock(
            return_value=Response(200, text=ROBOTS)
        )
        ok, reason = robots_allowed("https://www.sec.gov/private/x")
        assert ok and "tier-1" in reason
        assert robots_allowed("https://www.gao.gov/private/x")[0]
        assert sec.call_count == 0 and gao.call_count == 0

        real = robots.sources_config()
        monkeypatch.setattr(
            robots,
            "sources_config",
            lambda: {**real, "robots": {**real["robots"], "skip_hosts": ["example.com"]}},
        )
        other = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=ROBOTS))
        ok, reason = robots_allowed(f"{HOST}/private/x")
        assert ok and "skip_hosts" in reason and other.call_count == 0


def test_respect_false_turns_the_check_off(monkeypatch):
    monkeypatch.setattr(
        robots, "sources_config", lambda: {"robots": {"respect": False}, "tier1_hosts": []}
    )
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=ROBOTS))
        assert robots_allowed(f"{HOST}/private/x")[0]
        assert route.call_count == 0


def test_offline_uses_the_cache_and_never_the_network():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=ROBOTS))
        # online run records the robots.txt
        assert not robots_allowed(f"{HOST}/private/x")[0]
        assert route.call_count == 1
    robots.reset_memo()  # a new process: only the disk cache remains
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(500))
        ok, _ = robots_allowed(f"{HOST}/private/x", offline=True)
        assert not ok  # the recorded block is replayed
        assert robots_allowed(f"{HOST}/public", offline=True)[0]
        assert route.call_count == 0


def test_offline_without_a_cached_robots_txt_allows_without_a_request():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=ROBOTS))
        ok, reason = robots_allowed(f"{HOST}/private/x", offline=True)
        assert ok and "offline" in reason
        assert route.call_count == 0
        # and the miss is not memoised: going online later still fetches
        assert not robots_allowed(f"{HOST}/private/x")[0]
        assert route.call_count == 1


def test_cache_expires_after_24_hours(monkeypatch):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{HOST}/robots.txt").mock(return_value=Response(200, text=ROBOTS))
        robots_allowed(f"{HOST}/a")
        robots.reset_memo()
        robots_allowed(f"{HOST}/a")
        assert route.call_count == 1  # fresh on disk
        robots.reset_memo()
        real = time.time
        monkeypatch.setattr(
            "primer.clients.cache.time", SimpleNamespace(time=lambda: real() + 25 * 3600)
        )
        robots_allowed(f"{HOST}/a")
        assert route.call_count == 2  # stale, refetched
    cache = DiskCache(output_root(), "robots", 24 * 3600)
    assert json.loads(next(cache.dir.glob("*.json")).read_text())["body"]["status"] == 200


def test_record_policy_counts_distinct_urls_and_lists_blocked(tmp_path):
    log = RunLog(tmp_path)
    record_policy(log, "https://a.example/1", "allowed")
    record_policy(log, "https://a.example/1", "allowed")
    record_policy(log, "https://a.example/2", "limited")
    record_policy(log, "https://b.example/x", "denied")
    record_policy(log, "https://a.example/3", "allowed")
    record_policy(log, "https://a.example/3", "robots_blocked", "disallowed by robots.txt")
    # a later search listing does not weaken a block
    record_policy(log, "https://a.example/3", "allowed")
    assert log.meta["policy"] == {"allowed": 1, "limited": 1, "denied": 1, "robots_blocked": 1}
    assert log.meta["blocked"] == [
        {"url": "https://a.example/3", "reason": "disallowed by robots.txt", "host": "a.example"}
    ]
    with pytest.raises(ValueError):
        record_policy(log, "https://a.example/9", "maybe")
    assert json.loads(json.dumps(log.to_dict()))["meta"]["policy"]["robots_blocked"] == 1


def test_policy_counts_carry_over_when_a_run_resumes(tmp_path):
    """RunLog.load_previous restores meta; the new process adds to it, never restarts at zero."""
    log = RunLog(tmp_path)
    log.meta["policy"] = {"allowed": 5, "limited": 1, "denied": 0, "robots_blocked": 1}
    log.meta["blocked"] = [{"url": "https://a.example/x", "reason": "r", "host": "a.example"}]
    record_policy(log, "https://a.example/x", "robots_blocked", "r")  # same URL again
    record_policy(log, "https://b.example/y", "allowed")
    assert log.meta["policy"] == {"allowed": 6, "limited": 1, "denied": 0, "robots_blocked": 1}
    assert [b["url"] for b in log.meta["blocked"]] == ["https://a.example/x"]
