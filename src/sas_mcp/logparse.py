"""Triage of SAS logs into a compact, structured result.

SAS logs are long and the signal is buried. Worse, SAS routinely *succeeds*
while being semantically wrong -- a many-to-many merge, a silent character-to-
numeric conversion, a misspelled variable that quietly evaluates to missing.
Those show up only as NOTEs, so a caller that checks for ERROR alone will
happily accept a wrong answer.

This module splits a log into errors, warnings, and the specific subset of
NOTEs that mean "ran fine, answer may be wrong", plus per-step row counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

# Lines SAS emits that are pure noise for an agent.
_BANNER = re.compile(
    r"^(NOTE: (Copyright|SAS \(r\)|This session is executing|SAS Institute|"
    r"The SAS System|Licensed to|SAS initialization used|"
    r"AUTOEXEC processing|Unable to print title))"
)

# Page headers, e.g. "\x1447   The SAS System   Monday, August 31, 2026 ...".
# Real IOM logs are paginated, and these lines otherwise crowd out the actual
# source context around an error.
_PAGE_HEADER = re.compile(r"^\d*\s*The SAS System\s+\w+day,")

# SAS paginates with control characters that are not Python whitespace, so
# str.strip() alone leaves them attached to the first line of each page --
# enough to stop "NOTE:" from matching at the start of a line.
_CONTROL = "\x0c\x14\x0b\r"


def _clean(line: str) -> str:
    """Strip pagination control characters and surrounding whitespace."""
    return line.strip().lstrip(_CONTROL).strip()

# "NOTE: The data set WORK.FOO has 12 observations and 3 variables."
_DATASET_CREATED = re.compile(
    r"^NOTE: The data set (?P<ds>\S+) has (?P<obs>\d+) observations? "
    r"and (?P<vars>\d+) variables?\.",
)
# "NOTE: There were 12 observations read from the data set WORK.FOO."
_OBS_READ = re.compile(
    r"^NOTE: There were (?P<obs>\d+) observations? read from the data set (?P<ds>\S+)"
)
# "NOTE: DATA statement used (Total process time):" / "NOTE: PROCEDURE SQL used ..."
_STEP_USED = re.compile(
    r"^NOTE: (?P<step>DATA statement|PROCEDURE [A-Z0-9_]+|MERGE statement) used"
)
_REAL_TIME = re.compile(r"^\s+real time\s+(?P<t>[\d:.]+) seconds")
# "NOTE: PROCEDURE SQL used" gives the proc name; capture proc from source too.
_ERROR_LINE = re.compile(r"^ERROR(?: \d+-\d+)?:?\s")
_WARNING_LINE = re.compile(r"^WARNING(?: \d+-\d+)?:?\s")
_NOTE_LINE = re.compile(r"^NOTE:\s")


@dataclass(frozen=True)
class _Rule:
    """A named diagnostic for a log line that is not an ERROR but matters."""

    name: str
    pattern: re.Pattern[str]
    explanation: str


def _r(name: str, pattern: str, explanation: str) -> _Rule:
    return _Rule(name, re.compile(pattern, re.IGNORECASE), explanation)


# NOTEs and WARNINGs that indicate the code ran but the result is suspect.
# Ordered roughly by how often they signal a real defect.
SUSPICIOUS_RULES: tuple[_Rule, ...] = (
    _r(
        "uninitialized_variable",
        r"Variable (\S+) is uninitialized",
        "Variable was read before being assigned, so it evaluates to missing. "
        "Almost always a misspelled variable name.",
    ),
    _r(
        "many_to_many_merge",
        r"MERGE statement has more than one data set with repeats of BY values",
        "Many-to-many MERGE. SAS does not produce a Cartesian product here; it "
        "silently returns wrong results. Use PROC SQL join instead.",
    ),
    _r(
        "char_to_num_conversion",
        r"Character values have been converted to numeric",
        "Implicit type conversion. Non-numeric strings become missing without error.",
    ),
    _r(
        "num_to_char_conversion",
        r"Numeric values have been converted to character",
        "Implicit type conversion; may produce padded or scientific-notation text.",
    ),
    _r(
        "invalid_data",
        r"Invalid (?:numeric |character |argument to function )?data",
        "Input did not match the informat, so the value was set to missing.",
    ),
    _r(
        "missing_values_generated",
        r"Missing values were generated as a result of performing an operation",
        "An arithmetic operation on missing values produced missing results.",
    ),
    _r(
        "division_by_zero",
        r"Division by zero detected",
        "Division by zero; result set to missing.",
    ),
    _r(
        "math_operation_failed",
        r"Mathematical operations could not be performed",
        "One or more computations failed and produced missing values.",
    ),
    _r(
        "zero_observations",
        r"The data set (\S+) has 0 observations",
        "Step produced an empty data set. Usually an over-restrictive WHERE or a "
        "join that matched nothing.",
    ),
    _r(
        "no_rows_selected",
        r"No rows were selected",
        "Query matched no rows.",
    ),
    _r(
        "sql_remerge",
        r"The query requires remerging summary statistics back with the original data",
        "PROC SQL is remerging an aggregate against detail rows. This is often "
        "unintended and changes the row count.",
    ),
    _r(
        "format_too_small",
        r"At least one W\.D format was too small",
        "A numeric value did not fit its format and was rounded or shown in "
        "scientific notation. Displayed values may be misleading.",
    ),
    _r(
        "lost_card",
        r"LOST CARD",
        "INPUT ran past the end of the data; records were dropped.",
    ),
    _r(
        "input_new_line",
        r"SAS went to a new line when INPUT statement reached past the end of a line",
        "INPUT consumed a following record. Row alignment is likely wrong.",
    ),
    _r(
        "symbolic_not_resolved",
        r"Apparent symbolic reference (\S+) not resolved",
        "A macro variable does not exist. The literal &NAME text was passed through.",
    ),
    _r(
        "incomplete_dataset",
        r"The data set (\S+) may be incomplete",
        "The step was interrupted; the data set holds a partial result.",
    ),
    _r(
        "not_replaced",
        r"was not replaced because this step was stopped",
        "The target data set still holds its previous contents, not the new result.",
    ),
    _r(
        "value_truncated",
        r"(?:has been truncated|values have been truncated)",
        "A character value exceeded its column length and was cut off.",
    ),
    _r(
        "repeats_of_by",
        r"repeats of BY values",
        "Duplicate BY values encountered; check join or merge cardinality.",
    ),
)


@dataclass
class LogEvent:
    """One notable line from the log, with the source context around it."""

    severity: str  # "error" | "warning" | "note"
    line_no: int
    text: str
    rule: str | None = None
    explanation: str | None = None
    context: list[str] = field(default_factory=list)


@dataclass
class StepInfo:
    """Row counts and timing for one DATA or PROC step."""

    step: str
    dataset: str | None = None
    obs_out: int | None = None
    vars_out: int | None = None
    obs_read: int | None = None
    real_time_sec: float | None = None


@dataclass
class LogTriage:
    """Structured verdict on a submitted SAS program."""

    status: str  # "ok" | "suspicious" | "error"
    summary: str
    errors: list[LogEvent] = field(default_factory=list)
    warnings: list[LogEvent] = field(default_factory=list)
    suspicious_notes: list[LogEvent] = field(default_factory=list)
    steps: list[StepInfo] = field(default_factory=list)
    log_line_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_real_time(raw: str) -> float | None:
    """SAS prints either '0.03' or 'mm:ss.ss' for elapsed time."""
    try:
        if ":" in raw:
            parts = [float(p) for p in raw.split(":")]
            total = 0.0
            for part in parts:
                total = total * 60 + part
            return total
        return float(raw)
    except ValueError:
        return None


def _context_for(lines: list[str], idx: int, before: int = 3, after: int = 1) -> list[str]:
    """Grab surrounding lines.

    SAS prints the offending source line and an underline marker *before* the
    ERROR text, so the lines above are where the actual defect is visible.
    Blank lines are skipped rather than counted, since SAS pads the log
    generously and a fixed raw-line window often lands entirely on whitespace.
    """
    def usable(i: int) -> str | None:
        text = _clean(lines[i])
        if not text or _PAGE_HEADER.match(text):
            return None
        return text

    out: list[str] = []
    i = idx - 1
    while i >= 0 and len(out) < before:
        if (text := usable(i)) is not None:
            out.append(text)
        i -= 1
    out.reverse()

    out.append(_clean(lines[idx]))

    i, taken = idx + 1, 0
    while i < len(lines) and taken < after:
        if (text := usable(i)) is not None:
            out.append(text)
            taken += 1
        i += 1
    return out


def _classify_note(text: str) -> tuple[str, str] | None:
    for rule in SUSPICIOUS_RULES:
        if rule.pattern.search(text):
            return rule.name, rule.explanation
    return None


def parse_log(log: str, max_events: int = 25) -> LogTriage:
    """Reduce a raw SAS log to the lines that matter.

    ``max_events`` caps each event list so a pathological log (thousands of
    identical conversion notes) cannot flood the caller's context.
    """
    lines = log.splitlines()
    errors: list[LogEvent] = []
    warnings: list[LogEvent] = []
    suspicious: list[LogEvent] = []
    steps: list[StepInfo] = []

    pending: StepInfo | None = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip()
        stripped = _clean(raw)
        if not stripped:
            continue

        # Step accounting happens regardless of severity classification.
        if m := _DATASET_CREATED.match(stripped):
            pending = pending or StepInfo(step="DATA")
            pending.dataset = m.group("ds")
            pending.obs_out = int(m.group("obs"))
            pending.vars_out = int(m.group("vars"))
        elif m := _OBS_READ.match(stripped):
            pending = pending or StepInfo(step="DATA")
            if pending.obs_read is None:
                pending.obs_read = int(m.group("obs"))
        elif m := _STEP_USED.match(stripped):
            pending = pending or StepInfo(step="DATA")
            pending.step = m.group("step")
        elif m := _REAL_TIME.match(line):
            if pending is not None:
                pending.real_time_sec = _parse_real_time(m.group("t"))
                steps.append(pending)
                pending = None

        if _BANNER.match(stripped):
            continue

        if _ERROR_LINE.match(stripped):
            errors.append(
                LogEvent(
                    severity="error",
                    line_no=idx + 1,
                    text=stripped,
                    context=_context_for(lines, idx),
                )
            )
        elif _WARNING_LINE.match(stripped):
            hit = _classify_note(stripped)
            warnings.append(
                LogEvent(
                    severity="warning",
                    line_no=idx + 1,
                    text=stripped,
                    rule=hit[0] if hit else None,
                    explanation=hit[1] if hit else None,
                    context=_context_for(lines, idx),
                )
            )
        elif _NOTE_LINE.match(stripped):
            if hit := _classify_note(stripped):
                suspicious.append(
                    LogEvent(
                        severity="note",
                        line_no=idx + 1,
                        text=stripped,
                        rule=hit[0],
                        explanation=hit[1],
                        context=_context_for(lines, idx),
                    )
                )

    if pending is not None:  # step never printed a timing line
        steps.append(pending)

    total_errors, total_warnings = len(errors), len(warnings)
    total_suspicious = len(suspicious)

    if errors:
        status = "error"
    elif suspicious:
        status = "suspicious"
    else:
        status = "ok"

    summary = _summarize(status, total_errors, total_warnings, total_suspicious, steps)

    return LogTriage(
        status=status,
        summary=summary,
        errors=errors[:max_events],
        warnings=warnings[:max_events],
        suspicious_notes=suspicious[:max_events],
        steps=steps,
        log_line_count=len(lines),
    )


def _summarize(
    status: str, n_err: int, n_warn: int, n_susp: int, steps: list[StepInfo]
) -> str:
    bits: list[str] = []
    if n_err:
        bits.append(f"{n_err} error{'s' if n_err != 1 else ''}")
    if n_warn:
        bits.append(f"{n_warn} warning{'s' if n_warn != 1 else ''}")
    if n_susp:
        bits.append(f"{n_susp} suspicious note{'s' if n_susp != 1 else ''}")

    made = [s for s in steps if s.obs_out is not None]
    if made:
        outs = ", ".join(f"{s.dataset}={s.obs_out} obs" for s in made[:5])
        bits.append(f"created {outs}")

    if not bits:
        return "Ran cleanly; no output data sets reported."
    prefix = {
        "error": "FAILED",
        "suspicious": "Ran, but results may be wrong",
        "ok": "Ran",
    }[status]
    return f"{prefix}: " + "; ".join(bits) + "."
