"""Runs every analysis pass over an artefact set and returns findings.

This is the one place the analysis layer is assembled. It exists so that the
presentation layer has a single call to make and does not need to know which
passes exist, and so that a pass which is missing or broken costs its own
findings and nothing else.

The imports are deliberately soft. Each pass lives in its own module and any of
them may be absent from a given build or may fail to import; the tool must still
collect, still render and still write a zip. A missing pass is reported as a
broken rule, which is the same shape a rule that threw would produce, so the UI
needs no special case for it.

Nothing here opens a socket or reads a file. Given the same artefact set it
returns the same findings, which is what makes re-running today's rules against
a zip from months ago meaningful.
"""

import json

from .findings import sort_findings

# (module, attribute, description) for each optional pass. The attribute is
# called with the artefact set and returns findings.
# Each entry names more than one attribute because the passes are written
# independently and settled on slightly different names for the same thing. The
# first one that exists wins, which is cheaper than making four modules agree.
PASSES = (
    ("ps3diag.rules", ("run_rules",), "rule set"),
    ("ps3diag.patchstate", ("findings", "findings_for"), "known PSN fixes"),
    ("ps3diag.psnsafety", ("findings_for", "psn_findings"),
     "PSN safety check"),
)

# Passes that also write a JSON artefact back into the set, so the zip carries
# the conclusions next to the evidence.
ARTEFACT_PASSES = (
    ("ps3diag.patchstate", ("patch_state",), "patches/patch-state.json"),
    ("ps3diag.psnsafety", ("assessment",), "psn/safety.json"),
)


class Analysis:
    def __init__(self):
        self.findings = []
        self.broken_rules = []

    def counts(self):
        out = {}
        for finding in self.findings:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out


def _load(module_name, names):
    module = __import__(module_name, fromlist=["__name__"])
    for name in names:
        found = getattr(module, name, None)
        if callable(found):
            return found
    raise AttributeError(
        f"{module_name} has none of {', '.join(names)}")


def analyse(artefacts):
    """Every pass, in order, none of them able to stop another."""
    outcome = Analysis()
    collected = []

    for module_name, names, description in PASSES:
        try:
            call = _load(module_name, names)
        except (ImportError, AttributeError) as exc:
            outcome.broken_rules.append({
                "rule_id": module_name.rsplit(".", 1)[-1],
                "error": f"The {description} could not be loaded: {exc}",
            })
            continue
        try:
            result = call(artefacts)
        except Exception as exc:
            outcome.broken_rules.append({
                "rule_id": module_name.rsplit(".", 1)[-1],
                "error": f"The {description} failed: "
                         f"{exc.__class__.__name__}: {exc}",
            })
            continue
        # The rules engine returns a RuleRun; the single-purpose passes return a
        # plain list. Both are accepted so neither has to know about the other.
        if hasattr(result, "findings"):
            collected.extend(result.findings)
            outcome.broken_rules.extend(getattr(result, "broken_rules", []))
        elif result:
            collected.extend(result)

    for module_name, names, artefact_name in ARTEFACT_PASSES:
        try:
            call = _load(module_name, names)
            payload = call(artefacts)
        except (ImportError, AttributeError):
            continue
        except Exception as exc:
            outcome.broken_rules.append({
                "rule_id": artefact_name,
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            continue
        if payload:
            artefacts.put(artefact_name,
                          json.dumps(payload, indent=2, sort_keys=True,
                                     default=str))

    outcome.findings = sort_findings(collected)
    return outcome
