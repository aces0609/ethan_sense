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


def trade_press(sector_tag: str) -> list[str]:
    tp = sources_config().get("trade_press", {})
    return list(tp.get(sector_tag) or tp.get("default") or [])


def llm_available() -> bool:
    key_env = models_config().get("provider_key_env", "ANTHROPIC_API_KEY")
    return bool(os.environ.get(key_env))


def role_model(role: str) -> tuple[str, float]:
    """Return (model string, temperature) for a role. "template" means no LLM."""
    roles = models_config().get("roles", {})
    spec = roles.get(role, {})
    model = spec.get("model", "template")
    if not llm_available():
        model = "template"
    return model, float(spec.get("temperature", 0))


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
