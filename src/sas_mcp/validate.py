"""Validation primitives: PROC COMPARE decoding and SAS assertion macros.

SAS has no dominant unit-test framework, and teaching an agent a niche one
(SASUnit, FUTS) means teaching it an unfamiliar DSL. The idiomatic SAS
validation tool is already machine-readable: PROC COMPARE sets &SYSINFO to a
bitmask where each bit names a specific *kind* of difference. Decoding that
gives a real assertion primitive with no new framework.

On top of it sits a small macro library whose assertions write a delimited
marker to the log, which is cheap to parse back into structured results.
"""

from __future__ import annotations

import re
from typing import Any

from .schema import validate_libref, validate_member
from .session import SASSessionManager

# PROC COMPARE's documented &SYSINFO bits.
SYSINFO_BITS: tuple[tuple[int, str, str], ...] = (
    (1, "dslabel", "Data set labels differ"),
    (2, "dstype", "Data set types differ"),
    (4, "informat", "A variable has a different informat"),
    (8, "format", "A variable has a different format"),
    (16, "length", "A variable has a different length"),
    (32, "label", "A variable has a different label"),
    (64, "base_obs", "Base data set has observations not in comparison"),
    (128, "compare_obs", "Comparison data set has observations not in base"),
    (256, "base_by", "Base data set has BY groups not in comparison"),
    (512, "compare_by", "Comparison data set has BY groups not in base"),
    (1024, "base_var", "Base data set has variables not in comparison"),
    (2048, "compare_var", "Comparison data set has variables not in base"),
    (4096, "value", "At least one value comparison was unequal"),
    (8192, "type", "Conflicting variable types"),
    (16384, "byvar", "BY variables do not match"),
    (32768, "fatal", "Fatal error; comparison was not completed"),
)

# Differences that mean the data itself disagrees, as opposed to cosmetic
# metadata differences like a label or format.
_DATA_BITS = {64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768}


def decode_sysinfo(sysinfo: int) -> dict[str, Any]:
    """Turn PROC COMPARE's return code into named findings."""
    findings = [
        {"bit": bit, "code": code, "meaning": meaning}
        for bit, code, meaning in SYSINFO_BITS
        if sysinfo & bit
    ]
    data_differs = any(f["bit"] in _DATA_BITS for f in findings)
    return {
        "sysinfo": sysinfo,
        "identical": sysinfo == 0,
        "data_differs": data_differs,
        "metadata_only": bool(findings) and not data_differs,
        "findings": findings,
    }


def compare_datasets(
    mgr: SASSessionManager,
    base: str,
    compare: str,
    by: str | None = None,
    criterion: float | None = None,
    max_print: int = 20,
) -> dict[str, Any]:
    """Run PROC COMPARE and return a structured diff.

    ``base`` and ``compare`` are ``LIB.TABLE`` or bare table names (WORK).
    """
    base_q = _qualify(base)
    comp_q = _qualify(compare)

    opts = [f"base={base_q}", f"compare={comp_q}", f"maxprint=({max_print},{max_print})"]
    if criterion is not None:
        opts.append(f"criterion={float(criterion)}")

    by_stmt = ""
    if by:
        cols = _validate_columns(by)
        by_stmt = f"  by {' '.join(cols)};\n"

    code = (
        f"proc compare {' '.join(opts)};\n{by_stmt}run;\n"
        f"%put SASMCP_SYSINFO|&sysinfo;\n"
    )

    log, listing = mgr.submit_raw(code)

    sysinfo = _extract_sysinfo(log)
    if sysinfo is None:
        return {
            "status": "error",
            "error": "PROC COMPARE did not report a return code; the step "
                     "probably failed. Check the log.",
            "listing": listing.strip(),
        }

    result = decode_sysinfo(sysinfo)
    result["status"] = "ok"
    result["base"] = base_q.upper()
    result["compare"] = comp_q.upper()
    result["listing"] = listing.strip()
    result["summary"] = _compare_summary(result)
    return result


def _compare_summary(r: dict[str, Any]) -> str:
    if r["identical"]:
        return "Data sets are identical."
    if r["metadata_only"]:
        names = ", ".join(f["code"] for f in r["findings"])
        return f"Values match; only metadata differs ({names})."
    names = ", ".join(f["meaning"] for f in r["findings"])
    return f"Data sets differ: {names}."


_SYSINFO_RE = re.compile(r"^SASMCP_SYSINFO\|(-?\d+)\s*$", re.MULTILINE)


def _extract_sysinfo(log: str) -> int | None:
    matches = _SYSINFO_RE.findall(log)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def _qualify(name: str) -> str:
    """Validate a one- or two-level SAS data set name."""
    parts = name.strip().split(".")
    if len(parts) == 1:
        return f"WORK.{validate_member(parts[0])}"
    if len(parts) == 2:
        return f"{validate_libref(parts[0])}.{validate_member(parts[1])}"
    raise ValueError(
        f"{name!r} is not a valid SAS data set name; use TABLE or LIB.TABLE."
    )


_COLUMN_RE = re.compile(r"^[A-Za-z_]\w{0,31}$")


def _validate_columns(spec: str) -> list[str]:
    cols = [c for c in re.split(r"[,\s]+", spec.strip()) if c]
    for c in cols:
        if not _COLUMN_RE.match(c):
            raise ValueError(f"{c!r} is not a valid SAS column name.")
    return cols


# --- assertion macros --------------------------------------------------------

ASSERT_MARKER = "SASMCP_ASSERT"

ASSERT_MACROS = f"""
/* sas-mcp assertion library. Each assertion writes one delimited log line. */
%macro sasmcp_nobs(ds);
  %local dsid n;
  %let dsid = %sysfunc(open(&ds));
  %if &dsid %then %do;
    %let n = %sysfunc(attrn(&dsid, NLOBS));
    %let dsid = %sysfunc(close(&dsid));
  %end;
  %else %let n = -1;
  &n
%mend sasmcp_nobs;

%macro assert_exists(ds, name=assert_exists);
  %if %sysfunc(exist(&ds)) %then
    %put {ASSERT_MARKER}|PASS|&name|&ds exists;
  %else
    %put {ASSERT_MARKER}|FAIL|&name|&ds does not exist;
%mend assert_exists;

%macro assert_rows(ds, expected, name=assert_rows);
  %local n;
  %let n = %sasmcp_nobs(&ds);
  %if &n = -1 %then
    %put {ASSERT_MARKER}|FAIL|&name|&ds could not be opened;
  %else %if &n = &expected %then
    %put {ASSERT_MARKER}|PASS|&name|&ds has &n rows;
  %else
    %put {ASSERT_MARKER}|FAIL|&name|&ds has &n rows, expected &expected;
%mend assert_rows;

%macro assert_not_empty(ds, name=assert_not_empty);
  %local n;
  %let n = %sasmcp_nobs(&ds);
  %if &n > 0 %then
    %put {ASSERT_MARKER}|PASS|&name|&ds has &n rows;
  %else
    %put {ASSERT_MARKER}|FAIL|&name|&ds is empty;
%mend assert_not_empty;

%macro assert_no_missing(ds, var, name=assert_no_missing);
  %local nmiss;
  proc sql noprint;
    select count(*) into :nmiss trimmed from &ds where missing(&var);
  quit;
  %if &nmiss = 0 %then
    %put {ASSERT_MARKER}|PASS|&name|&var has no missing values in &ds;
  %else
    %put {ASSERT_MARKER}|FAIL|&name|&var has &nmiss missing values in &ds;
%mend assert_no_missing;

%macro assert_unique(ds, key, name=assert_unique);
  %local dups;
  proc sql noprint;
    select count(*) into :dups trimmed
      from (select &key from &ds group by &key having count(*) > 1);
  quit;
  %if &dups = 0 %then
    %put {ASSERT_MARKER}|PASS|&name|&key uniquely identifies rows in &ds;
  %else
    %put {ASSERT_MARKER}|FAIL|&name|&dups duplicate &key value(s) in &ds;
%mend assert_unique;

%macro assert_equal_datasets(base, compare, name=assert_equal_datasets);
  proc compare base=&base compare=&compare noprint;
  run;
  %if &sysinfo = 0 %then
    %put {ASSERT_MARKER}|PASS|&name|&base and &compare are identical;
  %else
    %put {ASSERT_MARKER}|FAIL|&name|&base and &compare differ (sysinfo=&sysinfo);
%mend assert_equal_datasets;

%macro assert_condition(condition, name=assert_condition, detail=);
  %if %unquote(&condition) %then
    %put {ASSERT_MARKER}|PASS|&name|&detail;
  %else
    %put {ASSERT_MARKER}|FAIL|&name|&detail;
%mend assert_condition;
"""

_ASSERT_RE = re.compile(
    rf"^{ASSERT_MARKER}\|(?P<status>PASS|FAIL|ERROR)\|(?P<name>[^|]*)\|(?P<detail>.*)$",
    re.MULTILINE,
)


def parse_assertions(log: str) -> list[dict[str, str]]:
    """Pull assertion markers back out of the log."""
    return [
        {
            "status": m.group("status"),
            "name": m.group("name").strip(),
            "detail": m.group("detail").strip(),
        }
        for m in _ASSERT_RE.finditer(log)
    ]


def summarize_assertions(assertions: list[dict[str, str]]) -> dict[str, Any]:
    passed = sum(a["status"] == "PASS" for a in assertions)
    failed = sum(a["status"] != "PASS" for a in assertions)
    if not assertions:
        verdict = (
            "No assertions ran. Call the assertion macros, e.g. "
            "%assert_rows(work.out, 19);"
        )
    elif failed:
        verdict = f"{failed} of {len(assertions)} assertions FAILED."
    else:
        verdict = f"All {passed} assertions passed."
    return {
        "total": len(assertions),
        "passed": passed,
        "failed": failed,
        "verdict": verdict,
    }
