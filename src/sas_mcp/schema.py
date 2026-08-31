"""Schema discovery, so the model stops guessing column names.

Hallucinated variable names are the single most common way an LLM writes
broken SAS, and it is entirely preventable: give the model the real columns
before it writes the step. Everything here reads SAS dictionary tables, which
are cheap and always present on 9.4.
"""

from __future__ import annotations

import re
from typing import Any

from .session import SASSessionManager

# SAS names: start with a letter or underscore, then letters/digits/underscore.
# Librefs cap at 8 characters, member and column names at 32.
_LIBREF_RE = re.compile(r"^[A-Za-z_]\w{0,7}$")
_MEMBER_RE = re.compile(r"^[A-Za-z_]\w{0,31}$")


class InvalidName(ValueError):
    """Raised when an identifier is not a legal SAS name."""


def validate_libref(name: str) -> str:
    """Return the uppercased libref, or raise.

    These values are interpolated into generated SQL against the dictionary
    tables, so they are validated rather than escaped -- a SAS name has no
    legal quoting form that could carry a statement separator anyway.
    """
    if not isinstance(name, str) or not _LIBREF_RE.match(name):
        raise InvalidName(
            f"{name!r} is not a valid SAS libref (letters, digits and "
            f"underscore; must start with a letter or underscore; 8 chars max)."
        )
    return name.upper()


def validate_member(name: str) -> str:
    if not isinstance(name, str) or not _MEMBER_RE.match(name):
        raise InvalidName(
            f"{name!r} is not a valid SAS table name (letters, digits and "
            f"underscore; must start with a letter or underscore; 32 chars max)."
        )
    return name.upper()


def _scalar(v: Any) -> Any:
    """Normalize one cell for JSON output.

    SAS numerics all arrive as float64, so counts and lengths would otherwise
    surface as ``19.0`` and ``8.0``; whole numbers are narrowed back to int.
    Missing values become None rather than NaN, which is not valid JSON.
    """
    if hasattr(v, "item"):  # numpy scalar -> python scalar
        v = v.item()
    if v is None:
        return None
    if isinstance(v, float):
        if v != v:  # NaN
            return None
        if v.is_integer():
            return int(v)
        return v
    if isinstance(v, str):
        return v.strip()
    return v


def _df_records(sas: Any, table: str) -> list[dict[str, Any]]:
    df = sas.sasdata2dataframe(table=table, libref="WORK")
    if df is None:
        return []
    return [
        {str(k).lower(): _scalar(v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]


def list_libraries(mgr: SASSessionManager) -> list[dict[str, Any]]:
    """Every assigned libref, with its path, engine, and read/write status."""
    sas = mgr.connect()
    code = """
    proc sql noprint;
      create table work._sasmcp_libs as
      select distinct libname, path, engine, readonly
        from dictionary.libnames
       order by libname;
    quit;
    """
    mgr.submit_raw(code)
    rows = _df_records(sas, "_sasmcp_libs")
    mgr.submit_raw("proc delete data=work._sasmcp_libs; run;")

    writable = mgr.policy.writable_libs
    for r in rows:
        lib = str(r.get("libname", "")).upper()
        r["policy_writable"] = lib in writable
    return rows


def list_datasets(mgr: SASSessionManager, libref: str) -> list[dict[str, Any]]:
    """Tables in a library, with row and column counts."""
    lib = validate_libref(libref)
    sas = mgr.connect()
    code = f"""
    proc sql noprint;
      create table work._sasmcp_tabs as
      select memname, memtype, nobs, nvar, crdate, modate, memlabel
        from dictionary.tables
       where libname = "{lib}"
       order by memname;
    quit;
    """
    mgr.submit_raw(code)
    rows = _df_records(sas, "_sasmcp_tabs")
    mgr.submit_raw("proc delete data=work._sasmcp_tabs; run;")
    return rows


def describe_dataset(
    mgr: SASSessionManager, libref: str, table: str
) -> dict[str, Any]:
    """Columns with type, length, format, and label, plus row count.

    This is the call to make before writing a step against an unfamiliar table.
    """
    lib = validate_libref(libref)
    mem = validate_member(table)
    sas = mgr.connect()

    code = f"""
    proc sql noprint;
      create table work._sasmcp_cols as
      select name, type, length, varnum, label, format, informat
        from dictionary.columns
       where libname = "{lib}" and memname = "{mem}"
       order by varnum;
      create table work._sasmcp_meta as
      select nobs, nvar, memlabel, crdate, modate, encoding
        from dictionary.tables
       where libname = "{lib}" and memname = "{mem}";
    quit;
    """
    mgr.submit_raw(code)
    columns = _df_records(sas, "_sasmcp_cols")
    meta_rows = _df_records(sas, "_sasmcp_meta")
    mgr.submit_raw(
        "proc delete data=work._sasmcp_cols work._sasmcp_meta; run;"
    )

    if not columns and not meta_rows:
        raise LookupError(
            f"{lib}.{mem} was not found. Use list_datasets to see what exists "
            f"in {lib}."
        )

    meta = meta_rows[0] if meta_rows else {}
    return {
        "libref": lib,
        "table": mem,
        "n_rows": meta.get("nobs"),
        "n_columns": meta.get("nvar") or len(columns),
        "label": meta.get("memlabel"),
        "encoding": meta.get("encoding"),
        "modified": str(meta.get("modate")) if meta.get("modate") else None,
        "columns": columns,
    }


def sample_rows(
    mgr: SASSessionManager, libref: str, table: str, n: int = 10
) -> dict[str, Any]:
    """First ``n`` rows, as records. Capped to keep responses small."""
    lib = validate_libref(libref)
    mem = validate_member(table)
    n = max(1, min(int(n), 200))
    sas = mgr.connect()

    df = sas.sasdata2dataframe(table=mem, libref=lib, dsopts={"obs": n})
    if df is None:
        raise LookupError(f"Could not read {lib}.{mem}.")
    return {
        "libref": lib,
        "table": mem,
        "returned_rows": len(df),
        "columns": [str(c) for c in df.columns],
        "rows": [
            {str(k): _scalar(v) for k, v in row.items()}
            for row in df.to_dict(orient="records")
        ],
    }
