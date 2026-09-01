"""MCP server exposing a SAS 9.4 session to coding agents.

Transport is stdio: the server runs as a subprocess on the developer's own
machine and SASPy reaches whatever SAS deployment they have (local, intranet
server, or SAS ODA). Nothing is hosted.

Tools deliberately fail *informatively* rather than raising: when policy blocks
a submit, the model needs to read the reason to correct itself, so the
explanation comes back as ordinary tool content.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__, schema, validate
from .doctor import run_diagnostics
from .guards import Policy
from .session import ConfigLocked, SASConnectionError, SASSessionManager

INSTRUCTIONS = """\
This server runs SAS 9.4 code through SASPy.

Working effectively:

1. Call `describe_dataset` before writing code against an unfamiliar table.
   Guessing column names is the most common cause of broken SAS here.
2. Read the `status` field of `run_sas` results. `suspicious` means the code
   RAN but the answer is probably WRONG -- an uninitialized variable, a
   many-to-many merge, a silent type conversion. Investigate before continuing;
   do not treat it as success.
3. The session is stateful. WORK data sets, librefs, macro variables, and
   options persist between calls, so you can build results up step by step.
   `session_status` shows what currently exists.
4. Writes are restricted to WORK by default. If a write is blocked, do not try
   to work around it -- report the restriction to the user and let them widen
   the policy.
5. `run_sas` returns triaged findings, not the whole log. Call `get_last_log`
   only when the triage is not enough to diagnose a problem.
6. If a tool returns `status: "config_required"`, the user has more than one
   SAS environment configured. The response lists them; call `use_sas_config`
   to pick one. If the user has not indicated which they want, ask -- running
   against the wrong SAS environment is worse than pausing to check.
"""


def build_server(
    cfgname: str | None = None,
    policy: Policy | None = None,
    manager: SASSessionManager | None = None,
    cfgfile: str | None = None,
    lock_config: bool = False,
) -> MCPServer:
    """Construct the MCP server and bind its tools to one session manager."""
    mgr = manager or SASSessionManager(
        cfgname=cfgname, policy=policy, cfgfile=cfgfile,
        lock_config=lock_config,
    )

    mcp = MCPServer(
        name="sas-mcp",
        title="SAS (via SASPy)",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    mutating = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                               idempotent_hint=False, open_world_hint=False)

    def _connection_error(exc: Exception) -> dict[str, Any]:
        text = str(exc)
        # A config choice is a question for the caller, not a fault to
        # diagnose -- answer it with the options rather than sending the agent
        # to the doctor.
        if "configurations, so one has to be chosen" in text:
            return {
                "status": "config_required",
                "error": text,
                "configs": mgr.available_configs(),
                "next_step": "Call use_sas_config with one of these names. If "
                             "the user has not said which SAS environment they "
                             "want, ask them rather than guessing.",
            }
        return {
            "status": "connection_error",
            "error": text,
            "next_step": "Run the sas_doctor tool; it reports configuration, "
                         "Java, credential, and network problems with fixes.",
            "troubleshooting": (
                "If sas_doctor does not resolve it, point the user at SASPy's "
                "troubleshooting guide: "
                "https://sassoftware.github.io/saspy/troubleshooting.html"
            ),
        }

    # --- diagnostics ---------------------------------------------------------

    @mcp.tool(
        name="sas_doctor",
        description=(
            "Diagnose SASPy configuration without connecting: config file and "
            "connection method, Java runtime for IOM, ~/.authinfo presence and "
            "permissions, ODA hostname validity, network reachability, and "
            "encoding. Run this first whenever a connection fails."
        ),
        annotations=read_only,
    )
    def sas_doctor(probe_network: bool = True) -> dict[str, Any]:
        """Check the SAS setup and report problems with their fixes.

        Args:
            probe_network: Also attempt DNS and TCP checks against the
                configured SAS host. Set false when offline or behind a proxy.
        """
        return run_diagnostics(
            cfgname=mgr.cfgname,
            probe_network=probe_network,
            cfgfile=mgr.cfgfile,
        )

    @mcp.tool(
        name="session_status",
        description=(
            "Report whether a SAS session is live, the SAS version and "
            "encoding, the assigned librefs, the data sets currently in WORK, "
            "and the active write policy. Useful for reorienting after a long "
            "conversation."
        ),
        annotations=read_only,
    )
    def session_status() -> dict[str, Any]:
        """Show the current state of the SAS session."""
        try:
            return mgr.status()
        except SASConnectionError as exc:
            return _connection_error(exc)

    @mcp.tool(
        name="list_sas_configs",
        description=(
            "List the SAS configurations available in the user's SASPy setup, "
            "with the access method and target server for each. Call this when "
            "connecting reports that a configuration must be chosen, or when "
            "the user mentions a specific SAS environment by name."
        ),
        annotations=read_only,
    )
    def list_sas_configs() -> dict[str, Any]:
        """Show the SAS configurations that can be connected to."""
        configs = mgr.available_configs()
        if mgr.lock_config:
            note = (
                f"This server is pinned to {mgr.cfgname!r} by its startup "
                f"options; switching is disabled."
            )
        elif len(configs) > 1:
            note = ("Select one with use_sas_config before running code.")
        else:
            note = None
        return {
            "active": mgr.cfgname,
            "locked": mgr.lock_config,
            "configs": configs,
            "note": note,
        }

    @mcp.tool(
        name="use_sas_config",
        description=(
            "Select which SAS configuration to connect to, by name. Ends any "
            "current SAS session, so WORK data sets and librefs from the "
            "previous configuration are lost. Use when the user names a SAS "
            "environment, or after list_sas_configs shows several."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                    open_world_hint=False),
    )
    def use_sas_config(name: str) -> dict[str, Any]:
        """Switch to a named SAS configuration.

        Args:
            name: A configuration name from list_sas_configs.
        """
        try:
            return {"status": "ok", **mgr.set_config(name)}
        except ConfigLocked as exc:
            return {
                "status": "config_locked",
                "error": str(exc),
                "active_config": mgr.cfgname,
                "next_step": "Tell the user the server is pinned to this "
                             "configuration and that changing it means "
                             "restarting the MCP server with different "
                             "options. Do not try to work around it.",
            }
        except SASConnectionError as exc:
            return {"status": "invalid_config", "error": str(exc),
                    "next_step": "Call list_sas_configs to see valid names."}

    # --- running code --------------------------------------------------------

    @mcp.tool(
        name="run_sas",
        description=(
            "Submit SAS code to the live session and return a triaged result: "
            "status (ok / suspicious / error), extracted errors and warnings, "
            "the NOTEs that mean the code ran but the answer may be wrong, "
            "per-step row counts, and the listing output. Writes outside WORK "
            "are blocked by policy. The session keeps state between calls."
        ),
        annotations=mutating,
    )
    def run_sas(code: str, include_log: bool = False) -> dict[str, Any]:
        """Run SAS code and return structured results.

        Args:
            code: The SAS program to submit.
            include_log: Return the full raw log as well. Leave false unless
                the triage is insufficient; logs are long.
        """
        try:
            result = mgr.submit(code)
        except PermissionError as exc:
            return {
                "status": "blocked_by_policy",
                "explanation": str(exc),
                "next_step": "Do not attempt to bypass this. Tell the user what "
                             "was blocked and let them decide whether to widen "
                             "the policy.",
            }
        except SASConnectionError as exc:
            return _connection_error(exc)
        return result.to_dict(include_log=include_log)

    @mcp.tool(
        name="get_last_log",
        description=(
            "Return the full raw SAS log from the most recent submit. Use only "
            "when the triaged output from run_sas was not enough to diagnose "
            "the problem, since logs consume a lot of context."
        ),
        annotations=read_only,
    )
    def get_last_log(max_lines: int = 400, from_end: bool = True) -> dict[str, Any]:
        """Fetch the raw log of the last submission.

        Args:
            max_lines: Cap on lines returned.
            from_end: Return the tail rather than the head. Errors are usually
                near the end.
        """
        log = mgr.last_log()
        if not log:
            return {"log": "", "note": "No code has been submitted yet."}
        lines = log.splitlines()
        truncated = len(lines) > max_lines
        keep = lines[-max_lines:] if from_end else lines[:max_lines]
        return {
            "log": "\n".join(keep),
            "total_lines": len(lines),
            "returned_lines": len(keep),
            "truncated": truncated,
        }

    @mcp.tool(
        name="reset_session",
        description=(
            "Delete every data set in WORK to clear accumulated state. Librefs "
            "and the connection itself are preserved. Use when earlier "
            "intermediate tables are causing confusion."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                    open_world_hint=False),
    )
    def reset_session() -> dict[str, Any]:
        """Clear the WORK library."""
        try:
            return {"status": "ok", "message": mgr.reset()}
        except SASConnectionError as exc:
            return _connection_error(exc)

    # --- schema discovery ----------------------------------------------------

    @mcp.tool(
        name="list_libraries",
        description=(
            "List assigned SAS libraries with path, engine, whether SAS "
            "considers them read-only, and whether this server's policy allows "
            "writing to them."
        ),
        annotations=read_only,
    )
    def list_libraries() -> dict[str, Any]:
        """Show available SAS libraries."""
        try:
            return {"libraries": schema.list_libraries(mgr)}
        except SASConnectionError as exc:
            return _connection_error(exc)

    @mcp.tool(
        name="list_datasets",
        description=(
            "List the data sets in a SAS library, with row counts, column "
            "counts, and modification dates."
        ),
        annotations=read_only,
    )
    def list_datasets(libref: str) -> dict[str, Any]:
        """List tables in a library.

        Args:
            libref: The library name, e.g. WORK or SASHELP.
        """
        try:
            return {
                "libref": libref.upper(),
                "datasets": schema.list_datasets(mgr, libref),
            }
        except schema.InvalidName as exc:
            return {"status": "invalid_name", "error": str(exc)}
        except SASConnectionError as exc:
            return _connection_error(exc)

    @mcp.tool(
        name="describe_dataset",
        description=(
            "Return the columns of a SAS data set with type, length, format, "
            "informat, and label, plus the row count. Call this before writing "
            "code against a table you have not already inspected."
        ),
        annotations=read_only,
    )
    def describe_dataset(libref: str, table: str) -> dict[str, Any]:
        """Get the schema of a data set.

        Args:
            libref: The library name, e.g. SASHELP.
            table: The data set name, e.g. CLASS.
        """
        try:
            return schema.describe_dataset(mgr, libref, table)
        except schema.InvalidName as exc:
            return {"status": "invalid_name", "error": str(exc)}
        except LookupError as exc:
            return {"status": "not_found", "error": str(exc)}
        except SASConnectionError as exc:
            return _connection_error(exc)

    @mcp.tool(
        name="sample_rows",
        description=(
            "Return the first N rows of a SAS data set as records, to inspect "
            "actual values, coding schemes, and missingness."
        ),
        annotations=read_only,
    )
    def sample_rows(libref: str, table: str, n: int = 10) -> dict[str, Any]:
        """Preview rows from a data set.

        Args:
            libref: The library name.
            table: The data set name.
            n: Number of rows (1-200).
        """
        try:
            return schema.sample_rows(mgr, libref, table, n)
        except schema.InvalidName as exc:
            return {"status": "invalid_name", "error": str(exc)}
        except LookupError as exc:
            return {"status": "not_found", "error": str(exc)}
        except SASConnectionError as exc:
            return _connection_error(exc)

    # --- validation ----------------------------------------------------------

    @mcp.tool(
        name="compare_datasets",
        description=(
            "Run PROC COMPARE between two data sets and return a structured "
            "diff: whether they are identical, whether the difference is in "
            "the data or only in metadata (labels, formats, lengths), and the "
            "specific kinds of difference found. This is the primary way to "
            "validate that a rewrite produces the same result as the original."
        ),
        annotations=read_only,
    )
    def compare_datasets(
        base: str,
        compare: str,
        by: str | None = None,
        criterion: float | None = None,
    ) -> dict[str, Any]:
        """Compare two SAS data sets.

        Args:
            base: Reference data set, as TABLE or LIB.TABLE.
            compare: Data set to check against the base.
            by: Optional space- or comma-separated BY columns. Both data sets
                must already be sorted by them.
            criterion: Optional numeric tolerance for value comparisons.
        """
        try:
            return validate.compare_datasets(mgr, base, compare, by, criterion)
        except (ValueError, schema.InvalidName) as exc:
            return {"status": "invalid_name", "error": str(exc)}
        except SASConnectionError as exc:
            return _connection_error(exc)

    @mcp.tool(
        name="run_sas_tests",
        description=(
            "Run SAS code with the assertion macro library available, and "
            "return each assertion's pass/fail result alongside the usual log "
            "triage. Available macros: %assert_exists(ds), "
            "%assert_rows(ds, n), %assert_not_empty(ds), "
            "%assert_no_missing(ds, var), %assert_unique(ds, key), "
            "%assert_equal_datasets(base, compare), and "
            "%assert_condition(condition, detail=...). Use this to validate "
            "code you have written."
        ),
        annotations=mutating,
    )
    def run_sas_tests(code: str, include_log: bool = False) -> dict[str, Any]:
        """Run SAS test code and report assertion results.

        Args:
            code: SAS code containing assertion macro calls.
            include_log: Also return the full raw log.
        """
        try:
            # Reject before loading anything, so a blocked request causes no
            # SAS traffic.
            mgr.check_policy(code)
            mgr.ensure_macros()
            result = mgr.submit(code)
        except PermissionError as exc:
            return {
                "status": "blocked_by_policy",
                "explanation": str(exc),
                "next_step": "Do not attempt to bypass this. Tell the user what "
                             "was blocked and let them decide whether to widen "
                             "the policy.",
            }
        except SASConnectionError as exc:
            return _connection_error(exc)

        assertions = validate.parse_assertions(result.log)
        payload = result.to_dict(include_log=include_log)
        payload["assertions"] = assertions
        payload["assertion_summary"] = validate.summarize_assertions(assertions)
        # A passing log with a failed assertion is still a failure.
        if payload["assertion_summary"]["failed"]:
            payload["status"] = "assertions_failed"
        return payload

    return mcp
