"""Model access by role (requirements §5 "Model roles").

`chat(role)` returns a LangChain chat model built with init_chat_model from
config/models.yaml, or None when the role resolves to "template" (no provider key).
Callers must handle None by using their deterministic template path. Token usage is
recorded per role in the RunLog. The endpoint comes from the active provider's
base_url_env (OPENAI_BASE_URL / ANTHROPIC_BASE_URL) so a proxy is a .env change.
"""

from __future__ import annotations

from typing import Any

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


def invoke_text(role: str, system: str, user: str, runlog: RunLog | None = None) -> str | None:
    """One-shot text completion for a role. Returns None when no LLM is configured or the
    per-primer token cap is spent."""
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
    if not budget_allows(role, runlog):
        return None
    llm = chat(role)
    if llm is None:
        return None
    if runlog:
        runlog.count(f"llm_calls_{role}")
    from langchain_core.messages import HumanMessage, SystemMessage

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
