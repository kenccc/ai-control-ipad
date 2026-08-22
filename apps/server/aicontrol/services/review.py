"""Code review: structured feedback back to an agent, and cross-agent review.

Review comments the user leaves on a diff are turned into one structured prompt and
sent to the *same* session that produced the diff. Cross-agent review launches the
other provider in a read-only posture -- review, do not edit -- against the same
working tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

REVIEW_SEVERITIES = ("CRITICAL", "WARNING", "SUGGESTION")


def build_feedback_prompt(comments: list[dict[str, Any]]) -> str:
    """Turn stored inline comments into the prompt sent back to the agent."""
    lines = ["Please address the following review comments:", ""]
    for index, comment in enumerate(comments, start=1):
        location = comment["file_path"]
        if comment.get("line"):
            location += f":{comment['line']}"
        body = (comment.get("body") or "").strip()
        lines.append(f"{index}. {location}")
        for body_line in body.splitlines():
            lines.append(f"   {body_line}")
        lines.append("")
    lines.append("Make the changes and tell me what you changed for each point.")
    return "\n".join(lines)


def build_review_prompt(*, diff: str, repository: str,
                        branch: Optional[str] = None,
                        issue_context: Optional[str] = None,
                        max_diff_chars: int = 120_000) -> str:
    """Prompt for a reviewing agent. Explicitly read-only by default."""
    truncated = len(diff) > max_diff_chars
    body = diff[:max_diff_chars]

    sections = [
        "You are reviewing code changes. Do NOT edit any files -- produce a review only.",
        "",
        f"Repository: {repository}",
    ]
    if branch:
        sections.append(f"Branch: {branch}")
    if issue_context:
        sections += ["", "## Original requirement", issue_context]
    sections += [
        "",
        "## Diff",
        "```diff",
        body,
        "```" + ("\n\n(diff truncated)" if truncated else ""),
        "",
        "## Output format",
        "Group your findings under these headings, omitting any that are empty. "
        "Under each, use one bullet per finding, starting with `path:line -- `:",
        "",
        "CRITICAL — bugs, data loss, security problems, broken behaviour",
        "WARNING — likely problems, missing error handling, missing tests",
        "SUGGESTION — clarity, structure, naming",
        "",
        "Be specific and concrete. If the change looks correct, say so plainly rather "
        "than inventing findings.",
    ]
    return "\n".join(sections)


@dataclass
class ReviewFinding:
    severity: str
    text: str
    file: Optional[str] = None
    line: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "text": self.text,
                "file": self.file, "line": self.line}


_HEADING = re.compile(r"^\s*#*\s*(CRITICAL|WARNING|SUGGESTION)\b", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*)$")
_LOCATION = re.compile(r"^([\w./\\-]+\.\w+)(?::(\d+))?\s*(?:--|—|-|:)\s*(.*)$")


def parse_review_output(text: str) -> list[ReviewFinding]:
    """Parse a reviewing agent's prose back into structured findings.

    Tolerant by design: an agent that ignores the requested format still produces
    usable output, and anything unparseable is preserved as plain text rather than
    dropped.
    """
    findings: list[ReviewFinding] = []
    severity = "SUGGESTION"
    for raw in text.splitlines():
        heading = _HEADING.match(raw)
        if heading and len(raw.strip()) < 60:
            severity = heading.group(1).upper()
            continue
        bullet = _BULLET.match(raw)
        if not bullet:
            continue
        content = bullet.group(1).strip().lstrip("`").strip()
        if not content:
            continue
        location = _LOCATION.match(content)
        if location:
            findings.append(ReviewFinding(
                severity=severity, file=location.group(1),
                line=int(location.group(2)) if location.group(2) else None,
                text=location.group(3).strip()))
        else:
            findings.append(ReviewFinding(severity=severity, text=content))
    return findings


def findings_to_prompt(findings: list[ReviewFinding], *, reviewer: str) -> str:
    """Turn another agent's findings into instructions for the implementing agent."""
    actionable = [f for f in findings if f.severity in {"CRITICAL", "WARNING"}]
    chosen = actionable or findings
    lines = [f"A {reviewer} review of your changes raised the following points. "
             "Please address them:", ""]
    for index, finding in enumerate(chosen, start=1):
        location = ""
        if finding.file:
            location = finding.file + (f":{finding.line}" if finding.line else "")
            location = f"{location} — "
        lines.append(f"{index}. [{finding.severity}] {location}{finding.text}")
    lines += ["", "Tell me what you changed for each point, or why it should not change."]
    return "\n".join(lines)
