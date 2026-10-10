"""Mechanical checks that a code recommendation does what it says. No LLM; plain pattern matching.

They catch the failure modes seen in real recommendations, not every possible bug:

- an error branch the edit adds or changes that neither logs nor passes on the error (it would
  hide the very failure chaos testing is meant to surface);
- a comment that says the error is logged when the edit makes no log call;
- a diagnosis that blames missing deadlines or timeouts, with no edit that adds one;
- a Go `context.WithTimeout` / `WithDeadline` whose cancel function is never called.

Only text the edit adds is judged: upstream code the edit leaves alone is not this proposal's fault.
"""

from __future__ import annotations

import re

from .models import CodeEdit
from .source import family

#: A logging call in any of the services' languages: log.Warn(..), logger.error(..),
#: log.WithError(err).Warn(..), console.error(..), Console.WriteLine(..), _logger.LogWarning(..).
LOG_CALL = re.compile(
    r"\b(?:log|logger|_logger|logging|console|Console|LOG)\b(?:\s*\.\s*\w+\s*\([^)]*\))*\s*\.\s*\w+\s*\("
    r"|\.\s*(?:Warn|Warnf|Warning|Error|Errorf|Info|Infof|Debug|Debugf|LogWarning|LogError|"
    r"LogInformation|WriteLine|warning|exception)\s*\("
)
PASSES_ON = re.compile(r"\b(?:throw|raise|panic)\b")
#: "no per-call deadlines", "missing timeouts", "without a deadline", "lacks timeouts", ...
DEADLINE_CLAIM = re.compile(
    r"\b(?:no|missing|without|lacks?|lacking|absent)\b(?:\W+\w+){0,5}?\W+(?:deadlines?|timeouts?)\b",
    re.IGNORECASE,
)
DEADLINE_ADDED = {
    "go": re.compile(r"context\.With(?:Timeout|Deadline)\s*\("),
    "python": re.compile(r"\btimeout\s*=|\bdeadline\b", re.IGNORECASE),
    "brace": re.compile(r"deadline|timeout|CancellationTokenSource|withDeadlineAfter", re.IGNORECASE),
}
GO_WITH_TIMEOUT = re.compile(r"(\w+)\s*,\s*(\w+)\s*:?=\s*context\.With(?:Timeout|Deadline)\s*\(")
COMMENT_SAYS_LOG = re.compile(r"\blog(?:s|ged|ging)?\b", re.IGNORECASE)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _brace_body(text: str, open_at: int) -> str:
    """The text between the `{` at `open_at` and its matching `}` (to the end if unbalanced)."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    return text[open_at + 1 :]


def error_branches(code: str, language: str) -> list[tuple[str | None, str, str]]:
    """(error variable or None, body, the whole branch) for each error-handling branch in `code`."""
    kind = family(language)
    found: list[tuple[str | None, str, str]] = []
    if kind == "go":
        for m in re.finditer(r"\bif\s+(?:[^;{}\n]+;\s*)?(\w+)\s*!=\s*nil\s*\{", code):
            body = _brace_body(code, m.end() - 1)
            found.append((m.group(1), body, m.group(0) + body))
    elif kind == "python":
        lines = code.splitlines()
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)except\b[^:]*?(?:\bas\s+(\w+))?\s*:\s*(.*)$", line)
            if not m:
                continue
            indent, body = len(m.group(1)), [m.group(3)] if m.group(3) else []
            for nxt in lines[i + 1 :]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                body.append(nxt)
            found.append((m.group(2), "\n".join(body), line + "\n" + "\n".join(body)))
    else:
        for m in re.finditer(r"\bcatch\b\s*(?:\(\s*(?:[\w.<>\[\]]+\s+)?(\w+)?\s*\))?\s*\{", code):
            body = _brace_body(code, m.end() - 1)
            found.append((m.group(1), body, m.group(0) + body))
    return found


def handles(body: str, var: str | None) -> bool:
    """Does an error branch log the error, or pass it on (return, rethrow, hand it to a call)?"""
    if LOG_CALL.search(body) or PASSES_ON.search(body):
        return True
    if var and var != "_":
        word = rf"\b{re.escape(var)}\b"
        return bool(re.search(rf"\breturn\b[^\n]*{word}", body) or re.search(rf"\w\s*\([^\n]*{word}", body))
    return False


def _added_lines(edit: CodeEdit) -> list[str]:
    before = {line.strip() for line in edit.original.splitlines()}
    return [line for line in edit.replacement.splitlines() if line.strip() and line.strip() not in before]


def lint_edit(edit: CodeEdit, language: str) -> list[str]:
    where = f"{edit.service}/{edit.file}" + (f" ({edit.function})" if edit.function else "")
    problems = []
    # Whole branches (condition and body) upstream already had: not this edit's doing.
    upstream = {_squash(whole) for _, _, whole in error_branches(edit.original, language)}
    for var, body, whole in error_branches(edit.replacement, language):
        if _squash(whole) in upstream:
            continue
        if not handles(body, var):
            problems.append(f"{where}: an error branch neither logs nor returns the error - it would hide it")
    marker = "#" if family(language) == "python" else "//"
    comments = [line for line in _added_lines(edit) if line.strip().startswith((marker, "/*", "*"))]
    if any(COMMENT_SAYS_LOG.search(c) for c in comments) and not LOG_CALL.search(edit.replacement):
        problems.append(f"{where}: a comment says the error is logged, but the edit makes no log call")
    if family(language) == "go":
        for m in GO_WITH_TIMEOUT.finditer(edit.replacement):
            cancel = m.group(2)
            if cancel == "_" or not re.search(rf"\b{re.escape(cancel)}\s*\(\s*\)", edit.replacement):
                problems.append(f"{where}: context.With{{Timeout,Deadline}} without calling its cancel func")
    return problems


def lint_edits(edits: list[CodeEdit], languages: dict[str, str], diagnosis: str) -> list[str]:
    """Every mechanical problem with these edits; `languages` maps service -> language."""
    problems = [p for e in edits for p in lint_edit(e, languages.get(e.service, ""))]
    if DEADLINE_CLAIM.search(diagnosis):
        adds = any(
            DEADLINE_ADDED[family(languages.get(e.service, ""))].search("\n".join(_added_lines(e)))
            for e in edits
        )
        if not adds:
            problems.append(
                "the diagnosis blames missing deadlines/timeouts, but no edit adds one "
                "(Go: ctx, cancel := context.WithTimeout(ctx, d); defer cancel())"
            )
    return problems
