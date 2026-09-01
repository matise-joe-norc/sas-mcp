"""Tests for the live connection checks, against a fake SAS session.

The probes are exercised here without a SAS deployment; the real end-to-end
run is `sas-mcp check`.
"""

import pytest

from sas_mcp import connectivity, doctor
from sas_mcp.session import SASConnectionError, SASSessionManager


CLEAN = """\
NOTE: There were 19 observations read from the data set SASHELP.CLASS.
NOTE: The data set WORK._SASMCP_PROBE has 19 observations and 5 variables.
NOTE: DATA statement used (Total process time):
      real time           0.01 seconds
"""

TRIAGE_VISIBLE = CLEAN + "NOTE: Variable weigth is uninitialized.\n"

# A session with NONOTES: the step runs, but the log carries no NOTEs at all.
NO_NOTES = "24         data work._sasmcp_probe; set sashelp.class; run;\n"


class FakeSAS:
    def __init__(self, log=TRIAGE_VISIBLE):
        self.log = log
        self.submitted = []
        self.ended = False
        self.sasver = "9.04.01M8P02222023"
        self.sascei = "utf-8"

    def submit(self, code, results="TEXT", **kw):
        self.submitted.append(code)
        return {"LOG": self.log, "LST": ""}

    def SYSERR(self):
        return 0

    def SYSERRORTEXT(self):
        return ""

    def sasdata2dataframe(self, table=None, libref=None, dsopts=None, **kw):
        import pandas as pd
        return pd.DataFrame({"t": ["café"]})

    def endsas(self):
        self.ended = True


def manager(fake):
    mgr = SASSessionManager()
    mgr._sas = fake
    return mgr


def by_name(checks):
    return {c.name: c for c in checks}


# --- the probe that matters most ---------------------------------------------


def test_log_notes_probe_passes_when_triage_sees_notes():
    fake = FakeSAS(TRIAGE_VISIBLE)
    checks = connectivity._check_log_notes(manager(fake))
    assert checks[0].status == doctor.PASS
    assert "working" in checks[0].message


def test_log_notes_probe_fails_when_the_log_has_no_notes():
    """NONOTES makes every result look like a clean success. The whole point
    of this probe is that a passing submit does not prove triage works."""
    fake = FakeSAS(NO_NOTES)
    checks = connectivity._check_log_notes(manager(fake))
    assert checks[0].status == doctor.FAIL
    assert "BLIND" in checks[0].message


def test_log_notes_probe_submits_code_that_should_be_flagged():
    fake = FakeSAS()
    connectivity._check_log_notes(manager(fake))
    assert "weigth" in fake.submitted[-1]


# --- submit ------------------------------------------------------------------


def test_submit_probe_reports_row_counts():
    checks = connectivity._check_submit(manager(FakeSAS(CLEAN)))
    assert checks[0].status == doctor.PASS
    assert "19 rows" in checks[0].message


def test_submit_probe_fails_when_no_counts_parsed():
    checks = connectivity._check_submit(manager(FakeSAS(NO_NOTES)))
    assert checks[0].status == doctor.FAIL
    assert "NOTEs" in checks[0].message


def test_submit_probe_reports_a_sas_error():
    log = "ERROR: File SASHELP.CLASS.DATA does not exist.\n"
    checks = connectivity._check_submit(manager(FakeSAS(log)))
    assert checks[0].status == doctor.FAIL
    assert "SASHELP" in checks[0].message


def test_unexpected_row_count_warns_rather_than_fails():
    log = CLEAN.replace("has 19 observations", "has 7 observations")
    checks = connectivity._check_submit(manager(FakeSAS(log)))
    assert checks[0].status == doctor.WARN


# --- encoding ----------------------------------------------------------------


def test_encoding_probe_passes_on_clean_round_trip():
    checks = connectivity._check_encoding(manager(FakeSAS()))
    assert checks[0].status == doctor.PASS


def test_encoding_probe_warns_on_mojibake():
    """An encoding mismatch corrupts silently rather than raising."""
    fake = FakeSAS()

    def corrupted(table=None, libref=None, dsopts=None, **kw):
        import pandas as pd
        return pd.DataFrame({"t": ["cafÃ©"]})

    fake.sasdata2dataframe = corrupted
    checks = connectivity._check_encoding(manager(fake))
    assert checks[0].status == doctor.WARN
    assert "did not round-trip" in checks[0].message
    assert "wlatin1" in checks[0].fix


# --- connection failure ------------------------------------------------------


def test_connection_failure_reports_once_and_points_at_doctor(monkeypatch):
    def boom(self):
        raise SASConnectionError("No SAS process attached.")

    monkeypatch.setattr(SASSessionManager, "connect", boom)
    checks = connectivity.run_connection_checks()
    assert len(checks) == 1
    assert checks[0].name == "connect"
    assert checks[0].status == doctor.FAIL
    assert "doctor" in checks[0].fix


# --- summary -----------------------------------------------------------------


def test_summarize_counts_and_verdict():
    checks = [doctor._ok("a", "x"), doctor._warn("b", "y")]
    s = connectivity.summarize(checks)
    assert s["counts"] == {"pass": 1, "warn": 1, "fail": 0}
    assert s["verdict"] == "Connected, with warnings."


def test_summarize_reports_failure():
    s = connectivity.summarize([doctor._fail("a", "x")])
    assert s["verdict"] == "Connection checks FAILED."


# --- run_full_check wiring ---------------------------------------------------


def test_full_check_skips_connecting_when_config_is_broken(monkeypatch):
    """Connecting with a known-bad config yields a confusing second failure
    rather than new information."""
    monkeypatch.setattr(
        doctor, "run_diagnostics",
        lambda **kw: {"verdict": "Blocked", "config_name": None,
                      "counts": {"pass": 0, "warn": 0, "fail": 1},
                      "checks": []},
    )
    called = []
    monkeypatch.setattr(connectivity, "run_connection_checks",
                        lambda **kw: called.append(1) or [])
    report = doctor.run_full_check()
    assert called == []
    assert report["connected"] is False
    assert any(c["name"] == "connection" for c in report["checks"])


def test_full_check_merges_live_results(monkeypatch):
    monkeypatch.setattr(
        doctor, "run_diagnostics",
        lambda **kw: {"verdict": "ok", "config_name": "oda",
                      "counts": {"pass": 2, "warn": 0, "fail": 0},
                      "checks": []},
    )
    monkeypatch.setattr(
        connectivity, "run_connection_checks",
        lambda **kw: [doctor._ok("connect", "Connected"),
                      doctor._ok("log_notes", "working")],
    )
    report = doctor.run_full_check()
    assert report["connected"] is True
    assert report["counts"]["pass"] == 4
    assert report["verdict"] == "Connected to SAS. Everything works."


def test_full_check_verdict_reflects_a_live_failure(monkeypatch):
    monkeypatch.setattr(
        doctor, "run_diagnostics",
        lambda **kw: {"verdict": "ok", "config_name": "oda",
                      "counts": {"pass": 1, "warn": 0, "fail": 0},
                      "checks": []},
    )
    monkeypatch.setattr(
        connectivity, "run_connection_checks",
        lambda **kw: [doctor._fail("log_notes", "BLIND")],
    )
    report = doctor.run_full_check()
    assert report["counts"]["fail"] == 1
    assert "Blocked" in report["verdict"]
    assert report["help"]["troubleshooting"] == doctor.SASPY_TROUBLESHOOTING


# --- CLI ---------------------------------------------------------------------


def test_check_command_connects(monkeypatch, capsys):
    from sas_mcp.cli import main
    seen = {}
    monkeypatch.setattr(
        "sas_mcp.cli.run_full_check",
        lambda **kw: seen.update(kw) or {
            "verdict": "ok", "counts": {"pass": 1, "warn": 0, "fail": 0},
            "checks": [], "config_name": None},
    )
    assert main(["check", "--no-network"]) == 0
    assert seen["probe_network"] is False


def test_doctor_connect_flag_is_an_alias_for_check(monkeypatch):
    from sas_mcp.cli import main
    calls = []
    monkeypatch.setattr(
        "sas_mcp.cli.run_full_check",
        lambda **kw: calls.append("full") or {
            "verdict": "ok", "counts": {"pass": 1, "warn": 0, "fail": 0},
            "checks": [], "config_name": None},
    )
    main(["doctor", "--connect"])
    assert calls == ["full"]


def test_plain_doctor_does_not_connect(monkeypatch):
    """doctor must stay side-effect free: it has to work when the connection
    is what is broken, and it must not spend a SAS session."""
    from sas_mcp.cli import main
    calls = []
    monkeypatch.setattr(
        "sas_mcp.cli.run_full_check",
        lambda **kw: calls.append("full") or {},
    )
    monkeypatch.setattr(
        "sas_mcp.cli.run_diagnostics",
        lambda **kw: {"verdict": "ok", "counts": {"pass": 1, "warn": 0, "fail": 0},
                      "checks": [], "config_name": None},
    )
    main(["doctor", "--no-network"])
    assert calls == []
