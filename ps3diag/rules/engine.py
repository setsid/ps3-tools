"""The registry and the runner.

Two things matter here and nothing else does.

The first is that run_rules cannot fail. A rule is a small piece of reasoning
about a console nobody here has seen, written against facts that a parser may
have failed to find, so a rule will eventually trip over a None it did not
expect. When that happens the helper must still get the other twenty answers,
so a rule that raises is recorded and the run carries on. That is the reason
every return value is checked rather than trusted: a rule that returns
something nonsensical is a broken rule, not a corrupt findings list.

The second is that a rule stays an ordinary function. The decorator registers
it and hands the function straight back, so a test can call it with one
hand-built ArtefactSet and read the finding it returns without going near the
engine.
"""

from ..findings import Finding, normalise, sort_findings

SCHEMA_VERSION = 1

# How much of an exception message is worth keeping. Enough to identify the
# fault, short enough that it does not swamp summary.txt.
MAX_ERROR_CHARS = 300


class Rule:
    __slots__ = ("rule_id", "category", "fn")

    def __init__(self, rule_id, category, fn):
        self.rule_id = rule_id
        self.category = category
        self.fn = fn

    def __call__(self, artefact_set):
        return self.fn(artefact_set)

    def __repr__(self):
        return f"<Rule {self.rule_id}>"


_REGISTRY = []
_BY_ID = {}


def rule(rule_id, category=""):
    """Register a function as a rule and return it unchanged.

    Unchanged on purpose: `builtin.temperature_high(some_set)` in a test is the
    same call the engine makes, so a rule can be exercised on its own.
    """
    def register(fn):
        if rule_id in _BY_ID:
            raise ValueError(
                f"a rule with id {rule_id!r} is already registered")
        entry = Rule(rule_id, category, fn)
        _REGISTRY.append(entry)
        _BY_ID[rule_id] = entry
        fn.rule = entry
        fn.rule_id = rule_id
        fn.category = category
        return fn
    return register


def registered_rules():
    """Every rule, in registration order."""
    return list(_REGISTRY)


def rule_ids():
    return [entry.rule_id for entry in _REGISTRY]


def get_rule(rule_id):
    return _BY_ID.get(rule_id)


class RuleRun:
    __slots__ = ("findings", "broken_rules")

    def __init__(self, findings=(), broken_rules=()):
        self.findings = list(findings)
        self.broken_rules = list(broken_rules)

    def of_severity(self, severity):
        return [item for item in self.findings if item.severity == severity]

    def to_dict(self):
        """The analysis/findings.json payload, as the schema defines it."""
        return {
            "schema_version": SCHEMA_VERSION,
            "findings": [item.to_dict() for item in self.findings],
            "broken_rules": [dict(item) for item in self.broken_rules],
        }

    def __repr__(self):
        return (f"<RuleRun {len(self.findings)} findings "
                f"{len(self.broken_rules)} broken>")


def run_rules(artefact_set, rules=None):
    """Run every rule over one set. Never raises.

    `rules` accepts Rule objects, rule ids, or plain functions, so a caller can
    run one rule, a hand-picked handful, or something not registered at all.
    """
    chosen, broken = _selected(rules)
    findings = []
    for entry in chosen:
        try:
            produced = entry(artefact_set)
            findings.extend(_coerce(produced, entry))
        except Exception as exc:                          # noqa: BLE001
            broken.append({"rule_id": entry.rule_id, "error": _describe(exc)})
    return RuleRun(sort_findings(findings), broken)


def _selected(rules):
    """Resolve the requested rules, turning a bad request into a broken rule
    rather than into an exception the caller has to handle."""
    if rules is None:
        return list(_REGISTRY), []
    chosen = []
    broken = []
    for item in rules:
        if isinstance(item, Rule):
            chosen.append(item)
            continue
        if isinstance(item, str):
            found = _BY_ID.get(item)
            if found is None:
                broken.append({"rule_id": item,
                               "error": "LookupError: no rule with this id is "
                                        "registered"})
            else:
                chosen.append(found)
            continue
        attached = getattr(item, "rule", None)
        if isinstance(attached, Rule):
            chosen.append(attached)
            continue
        chosen.append(Rule(getattr(item, "rule_id", "")
                           or getattr(item, "__name__", "anonymous"),
                           getattr(item, "category", ""), item))
    return chosen, broken


def _coerce(produced, entry):
    """One rule's return value as a list of Findings, or a TypeError.

    Also fills in the rule id and category the registration already knows, so
    a rule body does not have to repeat them, and forces the sortable fields to
    strings so that ordering the findings afterwards cannot fail.
    """
    if produced is None:
        return []
    if isinstance(produced, (Finding, dict)):
        produced = [produced]
    if not isinstance(produced, (list, tuple)):
        raise TypeError(f"a rule returns a Finding, a list of them or None, "
                        f"not {type(produced).__name__}")
    out = []
    for item in produced:
        if item is None:
            continue
        if not isinstance(item, (Finding, dict)):
            raise TypeError(f"a rule returns Findings, not "
                            f"{type(item).__name__}")
        finding = normalise(item)
        finding.rule_id = str(finding.rule_id or entry.rule_id)
        finding.category = str(finding.category or entry.category)
        finding.title = str(finding.title or "")
        finding.explanation = str(finding.explanation or "")
        finding.fix = str(finding.fix or "")
        out.append(finding)
    return out


def _describe(exc):
    text = f"{type(exc).__name__}: {exc}".strip()
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS - 3] + "..."
    return text
