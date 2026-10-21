"""Gate 2: the file compiles with @mdx-js/mdx via the Node sidecar; frontmatter parses."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import yaml

from primer.config import REPO_ROOT

SIDECAR = REPO_ROOT / "tools" / "mdx-check.mjs"


def check_frontmatter(mdx: str) -> dict:
    if not mdx.startswith("---\n"):
        return {"ok": False, "error": "no frontmatter"}
    end = mdx.find("\n---\n", 4)
    if end < 0:
        return {"ok": False, "error": "unterminated frontmatter"}
    try:
        fm = yaml.safe_load(mdx[4:end])
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


def check_mdx_compiles(path: Path) -> dict:
    node = shutil.which("node")
    if not node:
        return {"ok": None, "skipped": "node not found"}
    if not (SIDECAR.parent / "node_modules" / "@mdx-js" / "mdx").exists():
        return {"ok": None, "skipped": "tools/node_modules missing; run `npm install` in tools/"}
    proc = subprocess.run(
        [node, str(SIDECAR), str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(SIDECAR.parent),
    )
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    out.setdefault("ok", proc.returncode == 0)
    if proc.returncode != 0:
        out["ok"] = False
        out.setdefault("error", proc.stderr.strip()[-500:])
    return out
