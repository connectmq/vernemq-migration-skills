"""Shared ConnectMQ ACL rule model used by the source-specific converter scripts.

ConnectMQ evaluates an ordered rule list; the first enabled rule whose subject, action and topic filter match decides,
otherwise the tenant's "no match" policy applies. Rules are entered in the ConnectMQ console (Access control).

This module only uses the Python standard library.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

PLACEHOLDER_RE = re.compile(r"\$\{(clientid|username)\}", re.IGNORECASE)


@dataclass
class Rule:
    permission: str  # allow | deny
    action: str  # publish | subscribe | all
    topic_filter: str
    subject_type: str = "all"  # all | username | clientid
    subject_value: str = ""
    description: str = ""
    warnings: list[str] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.permission, self.action, self.topic_filter, self.subject_type, self.subject_value)


def validate_topic_filter(topic: str) -> str | None:
    """Same checks as the ConnectMQ API. Returns an error message or None."""
    if not topic:
        return "topic filter is empty"
    if len(topic) > 512:
        return "topic filter is longer than 512 characters"
    levels = topic.split("/")
    for i, level in enumerate(levels):
        if "#" in level and (level != "#" or i != len(levels) - 1):
            return "'#' must be a whole level and the last one"
        if "+" in level and level != "+":
            return "'+' must be a whole level"
        if "${" in PLACEHOLDER_RE.sub("", level):
            return "unsupported placeholder (only ${clientid} and ${username})"
    return None


def load_map(path: str | None, old_col: str, new_col: str) -> dict[str, str]:
    """Reads a CSV mapping (e.g. old_client_id,new_client_id). Missing file -> empty map."""
    if not path:
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = {old_col, new_col} - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{path}: missing column(s) {', '.join(sorted(missing))}")
        return {row[old_col].strip(): row[new_col].strip() for row in reader if row[old_col].strip()}


def expand_pattern(rule: Rule, client_map: dict[str, str], placeholder: str = "${clientid}",
                   subject_type: str = "clientid") -> list[Rule]:
    """Expands a rule whose topic uses ${clientid} into one rule per known client, keeping the OLD client ID in the
    topic (so the topic tree does not change) and matching the NEW ConnectMQ client ID as subject."""
    if placeholder.lower() not in rule.topic_filter.lower() or not client_map:
        return [rule]
    out = []
    for old_id, new_id in client_map.items():
        out.append(Rule(
            permission=rule.permission,
            action=rule.action,
            topic_filter=re.sub(re.escape(placeholder), lambda _m, v=old_id: v, rule.topic_filter, flags=re.IGNORECASE),
            subject_type=subject_type,
            subject_value=new_id,
            description=(rule.description + f" (expanded for {old_id})").strip(),
        ))
    return out


def dedupe(rules: Iterable[Rule]) -> list[Rule]:
    seen, out = set(), []
    for r in rules:
        if r.key() in seen:
            continue
        seen.add(r.key())
        out.append(r)
    return out


def check(rules: list[Rule]) -> list[str]:
    problems = []
    for i, r in enumerate(rules, 1):
        err = validate_topic_filter(r.topic_filter)
        if err:
            problems.append(f"rule {i} ({r.topic_filter}): {err}")
        if r.permission not in ("allow", "deny"):
            problems.append(f"rule {i}: bad permission {r.permission}")
        if r.action not in ("publish", "subscribe", "all"):
            problems.append(f"rule {i}: bad action {r.action}")
        if r.subject_type not in ("all", "username", "clientid"):
            problems.append(f"rule {i}: bad subject type {r.subject_type}")
    return problems


def render(rules: list[Rule], no_match: str, fmt: str, notes: list[str] | None = None) -> str:
    notes = notes or []
    if fmt == "json":
        return json.dumps({
            "settings": {"enabled": True, "noMatch": no_match},
            "rules": [{
                "order": i, "permission": r.permission, "action": r.action, "topicFilter": r.topic_filter,
                "subjectType": r.subject_type, "subjectValue": r.subject_value, "description": r.description,
                "warnings": r.warnings,
            } for i, r in enumerate(rules, 1)],
            "notes": notes,
        }, indent=2)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["order", "permission", "action", "topicFilter", "subjectType", "subjectValue", "description", "warnings"])
        for i, r in enumerate(rules, 1):
            w.writerow([i, r.permission, r.action, r.topic_filter, r.subject_type, r.subject_value, r.description,
                        "; ".join(r.warnings)])
        return buf.getvalue()
    # markdown
    lines = [
        "# ConnectMQ access control rules",
        "",
        f"Settings: **ACL enabled**, no-match policy **{no_match}**. Enter the rules in this order in the ConnectMQ",
        "console (Access control). The first matching rule decides. Per-topic ACL needs the Basic or Scale plan.",
        "",
        "| # | Permission | Action | Topic filter | Subject | Value | Note |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(rules, 1):
        note = r.description + ("; WARNING: " + "; ".join(r.warnings) if r.warnings else "")
        lines.append(f"| {i} | {r.permission} | {r.action} | `{r.topic_filter}` | {r.subject_type} | "
                     f"{('`' + r.subject_value + '`') if r.subject_value else ''} | {note.strip('; ')} |")
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(lines) + "\n"


def to_dict(rule: Rule) -> dict:
    return asdict(rule)
