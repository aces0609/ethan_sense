"""Validation gate (requirements §7): all checks must pass or the run is marked needs_review."""

from primer.validate.gate import ValidationReport, run_all

__all__ = ["ValidationReport", "run_all"]
