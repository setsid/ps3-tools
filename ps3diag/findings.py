"""The one currency the analysis layer deals in.

Kept in its own module, separate from the rules that produce them and the report
that renders them, so that all three can be written against it without any of
them importing the others.

normalise() accepts a Finding, a plain dict or anything with the right
attributes. The presentation layer therefore does not care whether a finding
came from this run's rules engine or out of an analysis/findings.json written by
an older version of the tool.
"""

SEVERITIES = ("error", "warn", "info")

SEVERITY_WORDS = {
    "error": "problem",
    "warn": "worth checking",
    "info": "for information",
}

# Plain words for the window, where a column header has no room to explain
# itself and "warn" on its own reads as more alarming than it is.
SEVERITY_LABELS = {
    "error": "Problem",
    "warn": "Worth checking",
    "info": "Information",
}

SEVERITY_ORDER = {name: index for index, name in enumerate(SEVERITIES)}


class Finding:
    __slots__ = ("rule_id", "severity", "title", "explanation", "fix",
                 "evidence", "category")

    def __init__(self, rule_id, severity, title, explanation, fix="",
                 evidence=(), category=""):
        if severity not in SEVERITIES:
            severity = "info"
        self.rule_id = rule_id
        self.severity = severity
        self.title = title
        self.explanation = explanation
        self.fix = fix or ""
        self.evidence = [str(item) for item in (evidence or ())]
        self.category = category or ""

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "title": self.title,
            "explanation": self.explanation,
            "fix": self.fix,
            "evidence": list(self.evidence),
            "category": self.category,
        }

    def __repr__(self):
        return f"<Finding {self.severity} {self.rule_id}>"


def normalise(item):
    """A Finding, whatever shape it arrived in."""
    if isinstance(item, Finding):
        return item
    if isinstance(item, dict):
        return Finding(
            item.get("rule_id", ""), item.get("severity", "info"),
            item.get("title", ""), item.get("explanation", ""),
            item.get("fix", ""), item.get("evidence", ()),
            item.get("category", ""))
    return Finding(
        getattr(item, "rule_id", ""), getattr(item, "severity", "info"),
        getattr(item, "title", ""), getattr(item, "explanation", ""),
        getattr(item, "fix", ""), getattr(item, "evidence", ()),
        getattr(item, "category", ""))


def sort_findings(findings):
    """Most serious first, then by rule so the order is stable between runs."""
    return sorted((normalise(item) for item in findings),
                  key=lambda item: (SEVERITY_ORDER.get(item.severity, 9),
                                    item.rule_id, item.title))
