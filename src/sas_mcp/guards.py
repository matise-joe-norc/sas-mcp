"""Best-effort guardrails against agent-authored SAS that destroys data.

Scope, stated plainly: this is a defense against *model error*, not a security
boundary. SAS can generate code at run time (CALL EXECUTE, DOSUBL, macro
expansion), so a determined bypass is always possible and no static scan can
close that hole. What this does close is the common accident -- an agent
improvising ``proc datasets lib=prod kill;`` against a production libref, or
shelling out with an X statement on a locked-down corporate server.

Policy is deny-by-default for anything outside WORK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

# --- comment stripping -------------------------------------------------------

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# A statement comment: '*' or '%*' at statement start, running to the next ';'.
_STAR_COMMENT = re.compile(r"(?m)^[ \t]*%?\*[^;]*;")


def strip_comments(code: str) -> str:
    """Blank out SAS comments, preserving line numbering.

    Newlines inside removed comments are kept so reported line numbers still
    match the caller's source.
    """

    def _blank(m: re.Match[str]) -> str:
        return "".join(ch if ch == "\n" else " " for ch in m.group(0))

    return _STAR_COMMENT.sub(_blank, _BLOCK_COMMENT.sub(_blank, code))


# --- rules -------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    name: str
    pattern: re.Pattern[str]
    category: str  # "os_escape" | "destructive"
    message: str


def _r(name: str, pattern: str, category: str, message: str) -> _Rule:
    return _Rule(name, re.compile(pattern, re.IGNORECASE | re.MULTILINE), category, message)


# Statements that reach outside the SAS session to the operating system.
OS_ESCAPE_RULES: tuple[_Rule, ...] = (
    _r("x_statement", r"(?:^|;)\s*x\s+(?:['\"]|\w)", "os_escape",
       "X statement runs an operating-system command."),
    _r("sysexec", r"%sysexec\b", "os_escape",
       "%SYSEXEC runs an operating-system command."),
    _r("systask", r"\bsystask\s+command\b", "os_escape",
       "SYSTASK COMMAND runs an operating-system command."),
    _r("call_system", r"\bcall\s+system\s*\(", "os_escape",
       "CALL SYSTEM runs an operating-system command."),
    _r("sysfunc_system", r"%sysfunc\s*\(\s*system\s*\(", "os_escape",
       "SYSTEM() via %SYSFUNC runs an operating-system command."),
    _r("filename_pipe", r"\bfilename\s+\w+\s+pipe\b", "os_escape",
       "FILENAME PIPE runs an operating-system command."),
    _r("infile_pipe", r"\binfile\s+[^;]*\bpipe\b", "os_escape",
       "INFILE with PIPE runs an operating-system command."),
    _r("proc_scripting_lang", r"\bproc\s+(?:python|lua|groovy)\b", "os_escape",
       "PROC PYTHON/LUA/GROOVY executes arbitrary non-SAS code."),
)

# Statements that delete or overwrite data.
DESTRUCTIVE_RULES: tuple[_Rule, ...] = (
    _r("datasets_kill", r"\bkill\b", "destructive",
       "KILL deletes every member of a library."),
    _r("datasets_delete", r"(?:^|;)\s*delete\s+", "destructive",
       "DELETE removes data sets."),
    _r("proc_delete", r"\bproc\s+delete\b", "destructive",
       "PROC DELETE removes data sets."),
    _r("sql_drop", r"\bdrop\s+(?:table|view|index)\b", "destructive",
       "DROP removes a table, view, or index."),
    _r("fdelete", r"%sysfunc\s*\(\s*fdelete\s*\(", "destructive",
       "FDELETE removes an external file."),
    _r("file_clear", r"\bproc\s+datasets\b[^;]*\bnolist\b[^;]*;\s*\bage\b", "destructive",
       "AGE rotates and discards data sets."),
)

# --- write-target detection --------------------------------------------------

_TWO_LEVEL = r"([A-Za-z_]\w{0,7})\.([A-Za-z_]\w*)"

# Places a two-level name means "this gets written".
_WRITE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Anchored to a statement boundary, not start-of-line: SAS happily accepts
    # `libname out '/tmp'; data out.x;` on a single line, and a line-anchored
    # pattern would miss the write entirely.
    ("data_step", re.compile(r"(?is)(?:^|;)\s*data\s+(?P<targets>[^;]*);")),
    ("sql_create", re.compile(rf"(?i)\bcreate\s+(?:table|view)\s+{_TWO_LEVEL}")),
    ("sql_insert", re.compile(rf"(?i)\binsert\s+into\s+{_TWO_LEVEL}")),
    ("sql_update", re.compile(rf"(?i)\bupdate\s+{_TWO_LEVEL}")),
    ("sql_delete_from", re.compile(rf"(?i)\bdelete\s+from\s+{_TWO_LEVEL}")),
    ("proc_append", re.compile(rf"(?i)\b(?:base|data)\s*=\s*{_TWO_LEVEL}")),
    ("out_option", re.compile(rf"(?i)\bout\s*=\s*{_TWO_LEVEL}")),
    ("outfile_lib", re.compile(rf"(?i)\bdatasets\s+(?:lib|library)\s*=\s*([A-Za-z_]\w{{0,7}})\b")),
)


@dataclass
class Policy:
    """What this session is permitted to do."""

    writable_libs: frozenset[str] = frozenset({"WORK"})
    allow_os_escape: bool = False
    allow_destructive: bool = False

    @classmethod
    def from_spec(
        cls,
        writable_libs: str | list[str] | None = None,
        allow_os_escape: bool = False,
        allow_destructive: bool = False,
    ) -> "Policy":
        if writable_libs is None:
            libs = {"WORK"}
        elif isinstance(writable_libs, str):
            libs = {p.strip().upper() for p in writable_libs.split(",") if p.strip()}
        else:
            libs = {str(p).strip().upper() for p in writable_libs if str(p).strip()}
        libs.add("WORK")  # WORK is scratch; always writable
        return cls(frozenset(libs), allow_os_escape, allow_destructive)


@dataclass
class Violation:
    rule: str
    category: str
    line_no: int
    matched: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GuardResult:
    allowed: bool
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "violations": [v.to_dict() for v in self.violations],
        }

    def explain(self) -> str:
        if self.allowed:
            return "No policy violations."
        lines = ["Blocked by sas-mcp policy:"]
        for v in self.violations:
            lines.append(f"  line {v.line_no}: [{v.rule}] {v.message}")
            lines.append(f"    matched: {v.matched.strip()!r}")
        lines.append(
            "\nIf this is intentional, the user can widen policy: set "
            "--writable-libs to include the target library, or start the server "
            "with --allow-destructive / --allow-os-escape."
        )
        return "\n".join(lines)


def _line_of(code: str, pos: int) -> int:
    return code.count("\n", 0, pos) + 1


def _split_data_targets(clause: str) -> list[tuple[str, str]]:
    """Pull two-level names out of a DATA statement's target list.

    Handles ``data work.a lib.b(keep=x);`` -- data set options are ignored.
    """
    out: list[tuple[str, str]] = []
    for tok in re.finditer(rf"{_TWO_LEVEL}", clause):
        out.append((tok.group(1).upper(), tok.group(2).upper()))
    return out


def check(code: str, policy: Policy) -> GuardResult:
    """Scan ``code`` against ``policy``. Returns violations, does not raise."""
    scan = strip_comments(code)
    violations: list[Violation] = []

    if not policy.allow_os_escape:
        for rule in OS_ESCAPE_RULES:
            for m in rule.pattern.finditer(scan):
                violations.append(
                    Violation(rule.name, rule.category, _line_of(scan, m.start()),
                              m.group(0), rule.message)
                )

    if not policy.allow_destructive:
        for rule in DESTRUCTIVE_RULES:
            for m in rule.pattern.finditer(scan):
                # KILL and DELETE are only meaningful inside PROC DATASETS/SQL;
                # requiring that context avoids firing on a variable named kill.
                if rule.name in {"datasets_kill", "datasets_delete"}:
                    if not re.search(r"\bproc\s+(?:datasets|sql)\b", scan[: m.start()],
                                     re.IGNORECASE):
                        continue
                violations.append(
                    Violation(rule.name, rule.category, _line_of(scan, m.start()),
                              m.group(0), rule.message)
                )

    violations.extend(_check_write_targets(scan, policy))
    violations.extend(_check_libref_rebinding(scan, policy))

    violations.sort(key=lambda v: (v.line_no, v.rule))
    return GuardResult(allowed=not violations, violations=violations)


# Assignment of a libref to a physical path (not CLEAR / LIST).
_LIBNAME_ASSIGN = re.compile(
    r"(?i)(?:^|;)\s*libname\s+(?P<lib>[A-Za-z_]\w{0,7})\s+(?P<rest>[^;]*)"
)


def _check_libref_rebinding(scan: str, policy: Policy) -> list[Violation]:
    """Flag re-pointing an allowlisted libref at a new location.

    The write allowlist is by libref name, so rebinding an approved name to a
    different directory would let writes land somewhere the user never
    approved while still passing the allowlist check.
    """
    found: list[Violation] = []
    for m in _LIBNAME_ASSIGN.finditer(scan):
        rest = m.group("rest").strip()
        if not rest or re.match(r"(?i)^(clear|list)\b", rest):
            continue
        lib = m.group("lib").upper()
        if lib == "WORK" or lib in policy.writable_libs:
            found.append(
                Violation(
                    rule="libref_rebinding",
                    category="write",
                    line_no=_line_of(scan, m.start("lib")),
                    matched=f"libname {lib} {rest[:40]}",
                    message=(
                        f"Reassigns library {lib}, which is on the writable "
                        f"allowlist. Writes would go to a location the user did "
                        f"not approve."
                    ),
                )
            )
    return found


def _check_write_targets(scan: str, policy: Policy) -> list[Violation]:
    found: list[Violation] = []
    seen: set[tuple[int, str]] = set()

    for kind, pat in _WRITE_PATTERNS:
        for m in pat.finditer(scan):
            if kind == "data_step":
                targets = _split_data_targets(m.group("targets"))
                base = m.start("targets")
            elif kind == "outfile_lib":
                targets = [(m.group(1).upper(), "*")]
                base = m.start(1)
            else:
                targets = [(m.group(1).upper(), m.group(2).upper())]
                base = m.start(1)

            for lib, member in targets:
                if lib in policy.writable_libs:
                    continue
                line_no = _line_of(scan, base)
                key = (line_no, f"{lib}.{member}")
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    Violation(
                        rule="write_outside_allowlist",
                        category="write",
                        line_no=line_no,
                        matched=f"{lib}.{member}",
                        message=(
                            f"Writes to library {lib}, which is not in the "
                            f"writable allowlist "
                            f"({', '.join(sorted(policy.writable_libs))})."
                        ),
                    )
                )
    return found
