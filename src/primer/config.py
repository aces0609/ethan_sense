"""Configuration: YAML files under config/ plus environment variables from .env.

Nothing here reads secrets into config objects; secrets stay in os.environ and are
redacted from run.json by RunLog.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

load_dotenv(REPO_ROOT / ".env")


@cache
def load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / f"{name}.yaml"
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def models_config() -> dict[str, Any]:
    return load_yaml("models")


def budgets_config() -> dict[str, Any]:
    return load_yaml("budgets")


def sources_config() -> dict[str, Any]:
    return load_yaml("sources")


def concepts_config() -> dict[str, Any]:
    return load_yaml("concepts")


def section_budget(section_id: int) -> dict[str, Any]:
    cfg = budgets_config()
    merged = dict(cfg.get("defaults", {}))
    merged.update(cfg.get("sections", {}).get(section_id, {}))
    return merged


def foundation_budget() -> dict[str, Any]:
    return dict(budgets_config().get("foundation", {}))


def market_data_budget() -> dict[str, Any]:
    return dict(budgets_config().get("market_data", {}))


def sector_for_sic(sic: str | int | None) -> str:
    """Sector tag for a 4-digit SIC code from config/sources.yaml `sectors`; "default" if none."""
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return "default"
    for tag, ranges in (sources_config().get("sectors") or {}).items():
        for r in ranges:
            lo, _, hi = str(r).partition("-")
            if int(lo) <= code <= int(hi or lo):
                return tag
    return "default"


def trade_press(sector_tag: str) -> list[str]:
    tp = sources_config().get("trade_press", {})
    return list(tp.get(sector_tag) or tp.get("default") or [])


def sector_brief_extras(sector_tag: str | None, section: int) -> dict[str, list[str]]:
    """Sector-specific `web_queries` and `sub_questions` for one section, from
    config/sources.yaml `sector_briefs`; empty lists when the sector has none."""
    by_sector = (sources_config().get("sector_briefs") or {}).get(sector_tag or "default") or {}
    extra = by_sector.get(section) or {}
    return {
        "web_queries": list(extra.get("web_queries") or []),
        "sub_questions": list(extra.get("sub_questions") or []),
    }


def peer_fallbacks(sector_tag: str | None) -> list[str]:
    """Competitor names to look for in scraped pages, from `peer_fallbacks` in sources.yaml."""
    pf = sources_config().get("peer_fallbacks") or {}
    return list(pf.get(sector_tag or "default") or pf.get("default") or [])


def active_provider() -> dict[str, Any] | None:
    """The provider block from models.yaml whose key env var is set, or None (template mode).

    `PRIMER_MODEL_PROVIDER` in the environment overrides the `provider` in models.yaml, so
    the config can name the production provider while a developer runs on another.
    """
    cfg = models_config()
    name = os.environ.get("PRIMER_MODEL_PROVIDER", "").strip() or cfg.get("provider")
    spec = (cfg.get("providers") or {}).get(name)
    if not spec or not os.environ.get(spec.get("key_env", "")):
        return None
    return {"name": name, **spec}


def llm_available() -> bool:
    return active_provider() is not None


def provider_base_url() -> str | None:
    """Endpoint for the active provider from its base_url_env; None means the SDK default."""
    p = active_provider()
    if not p:
        return None
    return os.environ.get(p.get("base_url_env", ""), "").strip() or None


def role_model(role: str) -> tuple[str, float | None]:
    """Return (model string, temperature) for a role. "template" means no LLM.

    A role may name a model directly (`model: provider:name`) or a tier (`tier: strong|cheap`)
    resolved against the active provider. Temperature None means "leave at the SDK default",
    which GPT-5-family reasoning models require.
    """
    roles = models_config().get("roles", {})
    spec = roles.get(role, {})
    provider = active_provider()
    if provider is None:
        return "template", None
    model = spec.get("model") or provider.get(spec.get("tier", "strong"), "template")
    temperature = spec.get("temperature", provider.get("temperature"))
    return model, (None if temperature is None else float(temperature))


def edgar_user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT must be set to a descriptive string with a contact email, "
            'e.g. "ethan_sense/0.1 (you@example.com)". SEC returns 403 without it.'
        )
    return ua


def output_root() -> Path:
    return Path(os.environ.get("PRIMER_OUTPUT_DIR", REPO_ROOT / "output"))


def tier_for_url(url: str) -> int:
    """Tier 1 for government and SEC hosts, tier 2 otherwise (requirements §4)."""
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    for t1 in sources_config().get("tier1_hosts", []):
        if host == t1 or host.endswith("." + t1):
            return 1
    return 2


def domain_policy(url: str) -> str:
    """'deny', 'limited' or 'allow'."""
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    cfg = sources_config()
    for d in cfg.get("deny", []):
        if host == d or host.endswith("." + d):
            return "deny"
    for d in cfg.get("limited", []):
        if host == d or host.endswith("." + d):
            return "limited"
    return "allow"
