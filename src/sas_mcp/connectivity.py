"""Live end-to-end checks against a real SAS session.

`doctor` deliberately never connects: it has to work when the connection is
exactly what is broken. These checks are the next rung -- they spend a real
SAS session to answer "does this actually work", which is a different question
from "is the configuration sane".

The probe that matters most is `log_notes`. SASPy's IOM sessions start with
NONOTES, which suppresses the NOTE: lines the triage layer reads. When that
happens every result still looks like a clean success while being blind to
the class of error that matters most, so it is checked explicitly rather than
inferred from a passing submit.
"""

from __future__ import annotations

from typing import Any

from .doctor import FAIL, INFO, PASS, WARN, Check, _fail, _ok, _warn
from .session import SASConnectionError, SASSessionManager

# A step with a misspelled variable. SAS runs it happily and writes 19 rows;
# only the NOTEs reveal that every value is missing.
_TRIAGE_PROBE = "data work._sasmcp_probe; set sashelp.class; " \
                "bmi = weigth / height; run;"

# 'e' with an acute accent: representable in both wlatin1 and UTF-8, so it
# round-trips cleanly when encodings agree and corrupts visibly when they do not.
_ENCODING_PROBE_VALUE = "café"


def run_connection_checks(
    cfgname: str | None = None, cfgfile: str | None = None
) -> list[Check]:
    """Connect, exercise the stack, and clean up after itself."""
    mgr = SASSessionManager(cfgname=cfgname, cfgfile=cfgfile)
    checks: list[Check] = []

    try:
        sas = mgr.connect()
    except SASConnectionError as exc:
        return [
            _fail(
                "connect",
                f"Could not start a SAS session: {exc}".split("\n\n")[0],
                "Re-run `sas-mcp doctor` for configuration problems, then see "
                "https://sassoftware.github.io/saspy/troubleshooting.html",
            )
        ]
    except Exception as exc:  # pragma: no cover - defensive
        return [_fail("connect", f"Unexpected error connecting: {exc}")]

    try:
        checks.append(_ok("connect", _session_banner(sas)))
        checks.extend(_check_submit(mgr))
        checks.extend(_check_log_notes(mgr))
        checks.extend(_check_schema(mgr))
        checks.extend(_check_encoding(mgr))
    finally:
        try:
            mgr.submit_raw(
                "proc datasets library=work nolist; "
                "delete _sasmcp_probe _sasmcp_enc; quit;"
            )
        except Exception:
            pass
        mgr.disconnect()

    return checks


def _session_banner(sas: Any) -> str:
    bits = []
    for attr, label in (("sasver", "SAS"), ("sascei", "encoding")):
        value = getattr(sas, attr, None)
        if value:
            bits.append(f"{label} {value}")
    return "Connected" + (f" ({', '.join(bits)})" if bits else "")


def _check_submit(mgr: SASSessionManager) -> list[Check]:
    """A plain DATA step runs, and its row counts are parsed back out."""
    try:
        r = mgr.submit("data work._sasmcp_probe; set sashelp.class; run;")
    except Exception as exc:
        return [_fail("submit", f"Submitting code failed: {exc}")]

    if r.triage.status == "error":
        detail = r.triage.errors[0].text if r.triage.errors else r.triage.summary
        return [
            _fail("submit", f"A basic DATA step failed: {detail}",
                  "Check that SASHELP is available to this SAS session.")
        ]

    made = [s for s in r.triage.steps if s.obs_out is not None]
    if not made:
        return [
            _fail(
                "submit",
                "The step ran but no row counts were parsed from the log, so "
                "the log is missing its NOTEs.",
                "See the log_notes check below.",
            )
        ]
    if made[0].obs_out != 19:
        return [
            _warn("submit",
                  f"SASHELP.CLASS returned {made[0].obs_out} rows, expected 19.",
                  "Harmless if your site customised SASHELP.")
        ]
    return [_ok("submit", f"DATA step ran; {made[0].dataset} = {made[0].obs_out} rows.")]


def _check_log_notes(mgr: SASSessionManager) -> list[Check]:
    """The critical probe: is triage actually seeing NOTEs?

    A session with NONOTES set returns a clean 'ok' for genuinely wrong code,
    so this runs code that must be flagged and fails when it is not.
    """
    try:
        r = mgr.submit(_TRIAGE_PROBE)
    except Exception as exc:
        return [_fail("log_notes", f"Probe submission failed: {exc}")]

    rules = {n.rule for n in r.triage.suspicious_notes}
    if "uninitialized_variable" in rules:
        return [
            _ok("log_notes",
                f"Log triage is working: flagged {r.triage.status} "
                f"({', '.join(sorted(rules))}).")
        ]

    return [
        _fail(
            "log_notes",
            "Log triage is BLIND. Code with a misspelled variable returned "
            f"status '{r.triage.status}' with no findings, which means the SAS "
            "log contains no NOTE: lines. Results will look like clean "
            "successes while being wrong.",
            "The session should set `options notes source;` on connect. If you "
            "see this, report it as a bug with your SAS version and access "
            "method.",
        )
    ]


def _check_schema(mgr: SASSessionManager) -> list[Check]:
    """Dictionary-table reads work, which is how column names are discovered."""
    from . import schema

    try:
        info = schema.describe_dataset(mgr, "SASHELP", "CLASS")
    except Exception as exc:
        return [
            _fail("schema", f"Reading SASHELP.CLASS metadata failed: {exc}",
                  "Schema discovery needs read access to dictionary tables.")
        ]

    names = {str(c.get("name", "")).upper() for c in info.get("columns", [])}
    if not names:
        return [_fail("schema", "No columns returned for SASHELP.CLASS.")]
    if not {"NAME", "AGE"} <= names:
        return [
            _warn("schema",
                  f"SASHELP.CLASS columns look unusual: {sorted(names)}.")
        ]
    return [
        _ok("schema",
            f"Schema discovery works ({info.get('n_columns')} columns, "
            f"{info.get('n_rows')} rows).")
    ]


def _check_encoding(mgr: SASSessionManager) -> list[Check]:
    """Round-trip a non-ASCII value.

    An encoding mismatch between the SAS session and the Python client does
    not raise -- it silently corrupts character data, which is far harder to
    notice than an error.
    """
    code = (
        f'data work._sasmcp_enc; length t $20; t = "{_ENCODING_PROBE_VALUE}"; '
        f"output; run;"
    )
    try:
        mgr.submit_raw(code)
        sas = mgr.connect()
        df = sas.sasdata2dataframe(table="_SASMCP_ENC", libref="WORK")
    except Exception as exc:
        return [_warn("encoding", f"Could not run the encoding probe: {exc}")]

    if df is None or df.empty:
        return [_warn("encoding", "Encoding probe returned no rows.")]

    got = str(df.iloc[0, 0]).strip()
    if got == _ENCODING_PROBE_VALUE:
        return [_ok("encoding", f"Non-ASCII round-trip is clean ({got!r}).")]
    return [
        _warn(
            "encoding",
            f"Non-ASCII text did not round-trip: sent "
            f"{_ENCODING_PROBE_VALUE!r}, got {got!r}. Character data will be "
            f"corrupted.",
            "Set 'encoding' in your SASPy config to match the SAS session "
            "encoding -- 'wlatin1' for many Windows SAS 9.4 installs, 'utf-8' "
            "for ODA and most Linux servers.",
        )
    ]


def summarize(checks: list[Check]) -> dict[str, Any]:
    """Fold connection checks into the same report shape doctor produces."""
    counts = {
        "pass": sum(c.status == PASS for c in checks),
        "warn": sum(c.status == WARN for c in checks),
        "fail": sum(c.status == FAIL for c in checks),
    }
    if counts["fail"]:
        verdict = "Connection checks FAILED."
    elif counts["warn"]:
        verdict = "Connected, with warnings."
    else:
        verdict = "Connected and working."
    return {"counts": counts, "verdict": verdict,
            "checks": [c.to_dict() for c in checks]}
