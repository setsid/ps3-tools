"""The analysis layer: pure functions from an ArtefactSet to Findings.

    from ps3diag.rules import run_rules
    outcome = run_rules(artefact_set)
    outcome.findings      # sorted, most serious first
    outcome.broken_rules  # the ones that threw, with their exception text

Nothing in here opens a socket, reads a file or looks at the clock. The same
set gives the same findings whether it came off a console on the desk or out of
a zip somebody emailed last year, which is the whole point of the split
described in docs/artefact-schema.md.

Importing this package registers the built-in rules, so a caller never has to
remember to do it.
"""

from .engine import (Rule, RuleRun, get_rule, registered_rules, rule,
                     rule_ids, run_rules)
from . import builtin  # noqa: F401  imported for the registrations it performs

__all__ = ["Rule", "RuleRun", "builtin", "get_rule", "registered_rules",
           "rule", "rule_ids", "run_rules"]
