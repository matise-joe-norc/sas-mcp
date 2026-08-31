"""End-to-end tests of the MCP tool surface against a fake SAS session.

These exercise the real server, session manager, guards, and log parser
together; only SASPy itself is faked, so the wiring is genuinely covered
without needing a live SAS deployment.
"""

import asyncio
import json

import pytest

from sas_mcp.guards import Policy
from sas_mcp.server import build_server
from sas_mcp.session import SASSessionManager


CLEAN_LOG = """\
1          data work.a;
2             set sashelp.class;
3          run;

NOTE: There were 19 observations read from the data set SASHELP.CLASS.
NOTE: The data set WORK.A has 19 observations and 5 variables.
NOTE: DATA statement used (Total process time):
      real time           0.01 seconds
"""

TYPO_LOG = """\
1          data work.b;
2             bmi = weigth / height;
3          run;

NOTE: Variable weigth is uninitialized.
NOTE: The data set WORK.B has 19 observations and 6 variables.
NOTE: DATA statement used (Total process time):
      real time           0.01 seconds
"""


class FakeSAS:
    """Minimal stand-in for saspy.SASsession."""

    def __init__(self, log=CLEAN_LOG, lst="output here"):
        self.submitted: list[str] = []
        self.log = log
        self.lst = lst
        self.ended = False

    def submit(self, code, results="TEXT", **kw):
        self.submitted.append(code)
        return {"LOG": self.log, "LST": self.lst}

    def SYSERR(self):
        return 0

    def SYSERRORTEXT(self):
        return ""

    def assigned_librefs(self):
        return ["WORK", "SASHELP"]

    def list_tables(self, libref, results="pandas"):
        import pandas as pd
        return pd.DataFrame({"MEMNAME": ["A", "B"]})

    def endsas(self):
        self.ended = True


def make(policy=None, log=CLEAN_LOG):
    """Build a server wired to a manager with a pre-injected fake session."""
    mgr = SASSessionManager(policy=policy or Policy.from_spec())
    fake = FakeSAS(log=log)
    mgr._sas = fake  # bypass real connection
    return build_server(manager=mgr), mgr, fake


def call(server, name, **args):
    """Invoke a tool and return its structured payload as a plain dict."""
    result = asyncio.run(server.call_tool(name, args))
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


# --- run_sas -----------------------------------------------------------------


def test_run_sas_clean_code_reports_ok():
    server, _, fake = make()
    out = call(server, "run_sas", code="data work.a; set sashelp.class; run;")
    assert out["status"] == "ok"
    assert out["steps"][0]["obs_out"] == 19
    assert fake.submitted


def test_run_sas_surfaces_suspicious_status():
    """A typo'd variable runs fine but must not be reported as success."""
    server, _, _ = make(log=TYPO_LOG)
    out = call(server, "run_sas", code="data work.b; bmi = weigth / height; run;")
    assert out["status"] == "suspicious"
    assert any(
        n["rule"] == "uninitialized_variable" for n in out["suspicious_notes"]
    )


def test_run_sas_omits_raw_log_by_default():
    server, _, _ = make()
    out = call(server, "run_sas", code="data work.a; run;")
    assert "log" not in out
    assert out["log_line_count"] > 0


def test_run_sas_includes_log_on_request():
    server, _, _ = make()
    out = call(server, "run_sas", code="data work.a; run;", include_log=True)
    assert "NOTE: The data set WORK.A" in out["log"]


# --- policy enforcement ------------------------------------------------------


def test_blocked_code_is_never_sent_to_sas():
    """The whole point of the guard: the dangerous submit must not happen."""
    server, _, fake = make()
    out = call(server, "run_sas", code="proc datasets lib=prod kill; quit;")
    assert out["status"] == "blocked_by_policy"
    assert fake.submitted == []


def test_block_explanation_tells_the_model_how_to_respond():
    server, _, _ = make()
    out = call(server, "run_sas", code="data prod.x; set work.a; run;")
    assert "write_outside_allowlist" in out["explanation"]
    assert "not attempt to bypass" in out["next_step"]


def test_write_permitted_after_policy_widened():
    server, _, fake = make(policy=Policy.from_spec(writable_libs="PROD"))
    out = call(server, "run_sas", code="data prod.x; set work.a; run;")
    assert out["status"] == "ok"
    assert len(fake.submitted) == 1


# --- logs and status ---------------------------------------------------------


def test_get_last_log_before_any_submit():
    server, _, _ = make()
    out = call(server, "get_last_log")
    assert out["log"] == ""


def test_get_last_log_returns_tail_after_submit():
    server, _, _ = make()
    call(server, "run_sas", code="data work.a; run;")
    out = call(server, "get_last_log", max_lines=2)
    assert out["returned_lines"] == 2
    assert out["truncated"] is True


def test_session_status_reports_policy_and_work_tables():
    server, _, _ = make(policy=Policy.from_spec(writable_libs="STAGE"))
    out = call(server, "session_status")
    assert out["connected"] is True
    assert set(out["policy"]["writable_libs"]) == {"WORK", "STAGE"}
    assert out["work_tables"] == ["A", "B"]
    assert "SASHELP" in out["librefs"]


def test_reset_session_clears_work():
    server, _, fake = make()
    out = call(server, "reset_session")
    assert out["status"] == "ok"
    assert any("kill" in c.lower() for c in fake.submitted)


# --- input validation --------------------------------------------------------


@pytest.mark.parametrize("bad", ["not a name", "toolonglibrefname", "1abc", ""])
def test_invalid_libref_rejected_without_touching_sas(bad):
    server, _, fake = make()
    out = call(server, "list_datasets", libref=bad)
    assert out["status"] == "invalid_name"
    assert fake.submitted == []


def test_sample_rows_rejects_bad_table_name():
    server, _, fake = make()
    out = call(server, "sample_rows", libref="WORK", table="a; drop table x")
    assert out["status"] == "invalid_name"
    assert fake.submitted == []


# --- tool surface ------------------------------------------------------------


def test_all_tools_registered():
    server, _, _ = make()
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {
        "sas_doctor", "session_status", "run_sas", "get_last_log",
        "reset_session", "list_libraries", "list_datasets",
        "describe_dataset", "sample_rows", "compare_datasets",
        "run_sas_tests",
    }


def test_read_only_tools_annotated():
    server, _, _ = make()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert tools["describe_dataset"].annotations.read_only_hint is True
    assert tools["run_sas"].annotations.read_only_hint is False
    assert tools["reset_session"].annotations.destructive_hint is True


def test_doctor_runs_without_a_connection():
    server, _, _ = make()
    out = call(server, "sas_doctor", probe_network=False)
    assert "verdict" in out
    assert out["counts"]["pass"] >= 1


# --- validation tools --------------------------------------------------------

IDENTICAL_LOG = CLEAN_LOG + "SASMCP_SYSINFO|0\n"
DIFFER_LOG = CLEAN_LOG + "SASMCP_SYSINFO|4160\n"  # base_obs | value

ASSERT_PASS_LOG = (
    CLEAN_LOG + "SASMCP_ASSERT|PASS|assert_rows|WORK.OUT has 19 rows\n"
)
ASSERT_FAIL_LOG = (
    CLEAN_LOG
    + "SASMCP_ASSERT|PASS|assert_exists|WORK.OUT exists\n"
    + "SASMCP_ASSERT|FAIL|assert_rows|WORK.OUT has 18 rows, expected 19\n"
)


def test_compare_datasets_identical():
    server, _, _ = make(log=IDENTICAL_LOG)
    out = call(server, "compare_datasets", base="work.a", compare="work.b")
    assert out["identical"] is True
    assert out["summary"] == "Data sets are identical."


def test_compare_datasets_reports_specific_differences():
    server, _, _ = make(log=DIFFER_LOG)
    out = call(server, "compare_datasets", base="work.a", compare="work.b")
    assert out["identical"] is False
    assert out["data_differs"] is True
    assert {f["code"] for f in out["findings"]} == {"base_obs", "value"}


def test_compare_datasets_rejects_injection_in_names():
    server, _, fake = make()
    out = call(
        server, "compare_datasets", base="a; quit; proc datasets kill", compare="b"
    )
    assert out["status"] == "invalid_name"
    assert fake.submitted == []


def test_compare_datasets_rejects_injection_in_by_clause():
    server, _, fake = make()
    out = call(
        server, "compare_datasets", base="work.a", compare="work.b",
        by="id; quit; proc datasets lib=prod kill",
    )
    assert out["status"] == "invalid_name"
    assert fake.submitted == []


def test_compare_datasets_handles_missing_return_code():
    server, _, _ = make(log=CLEAN_LOG)  # no SASMCP_SYSINFO marker
    out = call(server, "compare_datasets", base="work.a", compare="work.b")
    assert out["status"] == "error"


def test_run_sas_tests_loads_macros_then_runs():
    server, _, fake = make(log=ASSERT_PASS_LOG)
    out = call(server, "run_sas_tests", code="%assert_rows(work.out, 19);")
    assert out["assertion_summary"]["passed"] == 1
    assert out["assertion_summary"]["failed"] == 0
    assert any("%macro assert_rows" in c for c in fake.submitted)


def test_macros_are_loaded_only_once():
    server, mgr, fake = make(log=ASSERT_PASS_LOG)
    call(server, "run_sas_tests", code="%assert_rows(work.out, 19);")
    call(server, "run_sas_tests", code="%assert_rows(work.out, 19);")
    loads = [c for c in fake.submitted if "%macro assert_rows" in c]
    assert len(loads) == 1


def test_failed_assertion_overrides_clean_log_status():
    """The log has no ERROR, but a failed assertion must not read as success."""
    server, _, _ = make(log=ASSERT_FAIL_LOG)
    out = call(server, "run_sas_tests", code="%assert_rows(work.out, 19);")
    assert out["status"] == "assertions_failed"
    assert out["assertion_summary"]["failed"] == 1


def test_run_sas_tests_respects_policy():
    server, _, fake = make()
    out = call(server, "run_sas_tests", code="proc datasets lib=prod kill; quit;")
    assert out["status"] == "blocked_by_policy"
    assert fake.submitted == []


def test_connect_primes_notes_option():
    """SASPy's IOM sessions start with NONOTES, which blinds the whole triage
    layer. The manager must turn NOTES back on when it connects."""
    mgr = SASSessionManager()
    fake = FakeSAS()
    mgr._sas = fake
    mgr._prime_session()
    assert any("notes" in c and "source" in c for c in fake.submitted)


def test_reset_reasserts_notes_option():
    """PROC DATASETS KILL is followed by re-priming, not by NOSOURCE."""
    server, _, fake = make()
    call(server, "reset_session")
    reset_code = next(c for c in fake.submitted if "kill" in c.lower())
    assert "options notes source;" in reset_code
    assert "nosource" not in reset_code


def test_reset_forces_macro_reload():
    """PROC DATASETS KILL destroys WORK.SASMACR, so macros must be recompiled."""
    server, mgr, fake = make(log=ASSERT_PASS_LOG)
    call(server, "run_sas_tests", code="%assert_rows(work.out, 19);")
    call(server, "reset_session")
    call(server, "run_sas_tests", code="%assert_rows(work.out, 19);")
    loads = [c for c in fake.submitted if "%macro assert_rows" in c]
    assert len(loads) == 2
