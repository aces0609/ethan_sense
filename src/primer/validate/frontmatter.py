"""Gate 2: the primer.md frontmatter parses and carries the required keys."""

from __future__ import annotations

import yaml


def check_frontmatter(md: str) -> dict:
    if not md.startswith("---\n"):
        return {"ok": False, "error": "no frontmatter"}
    end = md.find("\n---\n", 4)
    if end < 0:
        return {"ok": False, "error": "unterminated frontmatter"}
    try:
        fm = yaml.safe_load(md[4:end])
    except yaml.YAMLError as e:
        return {"ok": False, "error": f"yaml: {e}"}
    required = [
        "company",
        "as_of",
        "run_id",
        "generator_version",
        "coverage",
        "source_limits",
        "disclaimer",
    ]
    missing = [k for k in required if k not in (fm or {})]
    return {"ok": not missing, "missing": missing, "keys": sorted((fm or {}).keys())}
