"""Model access by role (requirements §5 "Model roles").

`chat(role)` returns a LangChain chat model built with init_chat_model from
config/models.yaml, or None when the role resolves to "template" (no provider key).
Callers must handle None by using their deterministic template path. Token usage is
recorded per role in the RunLog. The endpoint comes from the active provider's
base_url_env (OPENAI_BASE_URL / ANTHROPIC_BASE_URL) so a proxy is a .env change.

Record / replay (requirements §8 Reproducibility): while a run is being recorded
(`primer.clients.cache.activate(run_dir, "record")`) every call's outcome is written to
`run_dir/replay/llm/{sha256(role + system + user + schema name)}.json` as `{text}`, the parsed
model's `model_dump()`, or `{none: true}` for a call that returned None (template path, token
cap, provider error). In replay mode the recorded outcome is returned without touching a
provider: no tokens are spent, a call with no recording returns None (the template path) and
is counted as `replay_llm_misses`.
"""

from __future__ import annotations

import hashlib
from typing import Any

from primer.clients import cache as _rec
from primer.config import budgets_config, provider_base_url, role_model
from primer.runlog import RunLog

_DISABLED: dict[str, str] = {}  # provider circuit breaker: reason once a call failed hard


def _disabled(runlog: RunLog | None) -> bool:
    return bool(_DISABLED)


def _call(role: str, runlog: RunLog | None, fn):
    """Run one model call; a provider error (quota, auth, network) is logged, counted and
    returned as None so the caller takes the template path. A quota or auth error disables
    further calls for the process, since every later one would fail the same way."""
    if _DISABLED:
        if runlog:
            runlog.count("llm_calls_skipped_provider_down")
        return None
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - any provider failure degrades, never crashes
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        if runlog:
            runlog.error(f"llm {role}: {msg}")
            runlog.count("llm_errors")
        low = msg.lower()
        if any(
            k in low
            for k in (
                "insufficient_quota",
                "credit_balance",
                "no credits",
                "authentication",
                "invalid api key",
                "incorrect api key",
            )
        ):
            _DISABLED["reason"] = msg
            if runlog:
                runlog.meta["llm_disabled"] = msg
        return None


def tokens_used(runlog: RunLog | None) -> int:
    if not runlog:
        return 0
    return sum(v["input"] + v["output"] for v in runlog.tokens.values())


def tokens_fraction(runlog: RunLog | None) -> float:
    """Share of the per-primer token cap already spent (0 when no cap)."""
    cap = int(budgets_config().get("per_primer", {}).get("tokens", 0) or 0)
    return tokens_used(runlog) / cap if cap and runlog else 0.0


def budget_allows(role: str, runlog: RunLog | None) -> bool:
    """Per-primer token cap (budgets.yaml per_primer.tokens, requirements §8): once the run
    has spent it, every further model call is skipped and the template path takes over; the
    skip is counted in run.json so the gap is visible."""
    cap = int(budgets_config().get("per_primer", {}).get("tokens", 0) or 0)
    if not cap or runlog is None:
        return True
    if role in ("synthesizer", "judge"):
        return True  # one small call each; the researcher loop is what the cap bounds
    if tokens_used(runlog) >= cap:
        runlog.count("llm_calls_skipped_token_cap")
        runlog.count(f"llm_skipped_{role}")
        return False
    return True


def chat(role: str) -> Any | None:
    model, temperature = role_model(role)
    if model == "template":
        return None
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    base_url = provider_base_url()
    if base_url:
        kwargs["base_url"] = base_url
    return init_chat_model(model, **kwargs)


def _record_usage(resp: Any, role: str, runlog: RunLog | None) -> None:
    usage = getattr(resp, "usage_metadata", None) or {}
    if runlog and usage:
        runlog.add_tokens(
            role, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        )


def llm_key(role: str, system: str, user: str, schema_name: str = "") -> str:
    """Recording key: sha256 over role, system prompt, user prompt and schema name."""
    h = hashlib.sha256()
    for part in (role, system, user, schema_name):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _dump_miss(role: str, key: str, system: str, user: str) -> None:
    """On a replay miss write the full missed prompt, and a unified diff against the closest
    recorded prompt of the same role (recordings keep their prompt), to
    `{replay run}/replay_misses/` so the cause of the divergence can be read, not guessed."""
    import difflib
    import json

    sess = _rec.session()
    if sess is None:
        return
    try:
        out = sess.log_dir / "replay_misses"
        out.mkdir(parents=True, exist_ok=True)
        text = "\n".join(
            [f"ROLE: {role}", f"KEY: {key}", "", "--- SYSTEM ---", system, "", "--- USER ---", user]
        )
        (out / f"{role}-{key[:12]}.txt").write_text(text, encoding="utf-8")
        best, best_ratio = None, 0.0
        for f in (sess.run_dir / "replay" / "llm").glob("*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            body = rec.get("body", rec)
            pr = body.get("prompt") if isinstance(body, dict) else None
            if not pr or body.get("role") != role:
                continue
            r = difflib.SequenceMatcher(None, user[:6000], pr["user"][:6000]).quick_ratio()
            if r > best_ratio:
                best, best_ratio = pr, r
        if best is not None:
            a = best["user"].splitlines()
            b = user.splitlines()
            diff = list(difflib.unified_diff(a, b, "recorded", "replayed", n=1, lineterm=""))[:400]
            (out / f"{role}-{key[:12]}.diff").write_text("\n".join(diff), encoding="utf-8")
    except Exception:  # noqa: BLE001 - diagnostics must never break a replay
        pass


def _replayed(
    role: str, key: str, runlog: RunLog | None, system: str = "", user: str = ""
) -> tuple[bool, dict[str, Any]]:
    """(found, record) for a replay lookup; a missing entry is counted and returns (False, {})."""
    rec = _rec.replay_json("llm", key)
    if rec is None:
        _rec.count_llm_miss(f"{role} {key[:12]} {user[:100]!r}")
        _dump_miss(role, key, system, user)
        return False, {}
    if runlog:
        runlog.count("replay_llm_hits")
    return True, rec


def invoke_text(role: str, system: str, user: str, runlog: RunLog | None = None) -> str | None:
    """One-shot text completion for a role. Returns None when no LLM is configured or the
    per-primer token cap is spent."""
    key = llm_key(role, system, user)
    if _rec.mode() == "replay":
        found, rec = _replayed(role, key, runlog, system, user)
        return rec.get("text") if found and not rec.get("none") else None
    text = _invoke_text(role, system, user, runlog)
    payload: dict[str, Any] = {
        "role": role,
        "kind": "text",
        "prompt": {"system": system, "user": user},
    }
    payload.update({"text": text} if text is not None else {"none": True})
    _rec.record_json("llm", key, payload)
    return text


def _invoke_text(role: str, system: str, user: str, runlog: RunLog | None) -> str | None:
    if not budget_allows(role, runlog):
        return None
    llm = chat(role)
    if llm is None:
        return None
    if runlog:
        runlog.count(f"llm_calls_{role}")
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = _call(
        role,
        runlog,
        lambda: llm.invoke([SystemMessage(content=system), HumanMessage(content=user)]),
    )
    if resp is None:
        return None
    _record_usage(resp, role, runlog)
    content = resp.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


def invoke_structured(
    role: str, schema: type, system: str, user: str, runlog: RunLog | None = None
):
    key = llm_key(role, system, user, getattr(schema, "__name__", str(schema)))
    if _rec.mode() == "replay":
        found, rec = _replayed(role, key, runlog, system, user)
        if not found or rec.get("none"):
            return None
        try:
            value = rec["value"]
            return schema.model_validate(value) if hasattr(schema, "model_validate") else value
        except Exception:  # noqa: BLE001 - a recording the schema no longer accepts is a miss
            _rec.count_llm_miss(f"{role} {key[:16]} (schema mismatch)")
            return None
    parsed = _invoke_structured(role, schema, system, user, runlog)
    payload: dict[str, Any] = {
        "role": role,
        "kind": "structured",
        "schema": getattr(schema, "__name__", ""),
        "prompt": {"system": system, "user": user},
    }
    if parsed is None:
        payload["none"] = True
    else:
        payload["value"] = (
            parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else parsed
        )
    _rec.record_json("llm", key, payload)
    return parsed


def _invoke_structured(role: str, schema: type, system: str, user: str, runlog: RunLog | None):
    if not budget_allows(role, runlog):
        return None
    llm = chat(role)
    if llm is None:
        return None
    if runlog:
        runlog.count(f"llm_calls_{role}")
    from langchain_core.messages import HumanMessage, SystemMessage

    # Claude 5.5 models reject forced tool calling for structured output (the default method),
    # which silently sent every structured call to the template path; json_schema works on both
    # providers that support it, and the default stays the fallback for the rest.
    try:
        structured = llm.with_structured_output(schema, method="json_schema", include_raw=True)
    except (TypeError, ValueError, NotImplementedError):
        structured = llm.with_structured_output(schema, include_raw=True)
    out = _call(
        role,
        runlog,
        lambda: structured.invoke([SystemMessage(content=system), HumanMessage(content=user)]),
    )
    if out is None:
        return None
    raw = out.get("raw") if isinstance(out, dict) else None
    _record_usage(raw, role, runlog)
    return out.get("parsed") if isinstance(out, dict) else out
