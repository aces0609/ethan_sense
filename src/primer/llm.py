"""Model access by role (requirements §5 "Model roles").

`chat(role)` returns a LangChain chat model built with init_chat_model from
config/models.yaml, or None when the role resolves to "template" (no provider key).
Callers must handle None by using their deterministic template path. Token usage is
recorded per role in the RunLog.
"""

from __future__ import annotations

from typing import Any

from primer.config import role_model
from primer.runlog import RunLog


def chat(role: str) -> Any | None:
    model, temperature = role_model(role)
    if model == "template":
        return None
    from langchain.chat_models import init_chat_model

    return init_chat_model(model, temperature=temperature)


def invoke_text(role: str, system: str, user: str, runlog: RunLog | None = None) -> str | None:
    """One-shot text completion for a role. Returns None when no LLM is configured."""
    llm = chat(role)
    if llm is None:
        return None
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    usage = getattr(resp, "usage_metadata", None) or {}
    if runlog and usage:
        runlog.add_tokens(
            role, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        )
    content = resp.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return str(content)


def invoke_structured(
    role: str, schema: type, system: str, user: str, runlog: RunLog | None = None
):
    llm = chat(role)
    if llm is None:
        return None
    from langchain_core.messages import HumanMessage, SystemMessage

    structured = llm.with_structured_output(schema, include_raw=True)
    out = structured.invoke([SystemMessage(content=system), HumanMessage(content=user)])
    raw = out.get("raw") if isinstance(out, dict) else None
    usage = getattr(raw, "usage_metadata", None) or {}
    if runlog and usage:
        runlog.add_tokens(
            role, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        )
    return out.get("parsed") if isinstance(out, dict) else out
