"""Tests for SAS log triage, using log text in the shape SAS 9.4 actually emits."""

from sas_mcp.logparse import parse_log


CLEAN_LOG = """\
1          data work.a;
2             set sashelp.class;
3          run;

NOTE: There were 19 observations read from the data set SASHELP.CLASS.
NOTE: The data set WORK.A has 19 observations and 5 variables.
NOTE: DATA statement used (Total process time):
      real time           0.01 seconds
      cpu time            0.01 seconds
"""

SYNTAX_ERROR_LOG = """\
1          data work.a;
2             set sashelp.clas;
3          run;

ERROR: File SASHELP.CLAS.DATA does not exist.
NOTE: The SAS System stopped processing this step because of errors.
WARNING: The data set WORK.A may be incomplete.  When this step was stopped there were 0 observations and 0 variables.
NOTE: DATA statement used (Total process time):
      real time           0.00 seconds
"""

UNINITIALIZED_LOG = """\
1          data work.b;
2             set sashelp.class;
3             bmi = weigth / (height**2);
4          run;

NOTE: Variable weigth is uninitialized.
NOTE: Missing values were generated as a result of performing an operation on missing values.
      Each place is given by: (Number of times) at (Line):(Column).
      19 at 3:19
NOTE: There were 19 observations read from the data set SASHELP.CLASS.
NOTE: The data set WORK.B has 19 observations and 6 variables.
NOTE: DATA statement used (Total process time):
      real time           0.02 seconds
"""

MANY_TO_MANY_LOG = """\
1          data work.c;
2             merge work.x work.y;
3             by id;
4          run;

NOTE: MERGE statement has more than one data set with repeats of BY values.
NOTE: There were 10 observations read from the data set WORK.X.
NOTE: The data set WORK.C has 10 observations and 4 variables.
NOTE: DATA statement used (Total process time):
      real time           0.01 seconds
"""


def test_clean_log_is_ok():
    r = parse_log(CLEAN_LOG)
    assert r.status == "ok"
    assert not r.errors and not r.suspicious_notes


def test_clean_log_captures_step_counts():
    r = parse_log(CLEAN_LOG)
    assert len(r.steps) == 1
    step = r.steps[0]
    assert step.dataset == "WORK.A"
    assert step.obs_out == 19
    assert step.vars_out == 5
    assert step.obs_read == 19
    assert step.real_time_sec == 0.01
    assert step.step == "DATA statement"


def test_error_log_reports_error_status():
    r = parse_log(SYNTAX_ERROR_LOG)
    assert r.status == "error"
    assert len(r.errors) == 1
    assert "SASHELP.CLAS.DATA does not exist" in r.errors[0].text


def test_error_context_includes_offending_source():
    r = parse_log(SYNTAX_ERROR_LOG)
    ctx = "\n".join(r.errors[0].context)
    assert "sashelp.clas" in ctx


def test_incomplete_dataset_warning_is_classified():
    r = parse_log(SYNTAX_ERROR_LOG)
    rules = {w.rule for w in r.warnings}
    assert "incomplete_dataset" in rules


def test_uninitialized_variable_makes_clean_run_suspicious():
    """The step succeeds and writes 19 rows; only the NOTE reveals the typo."""
    r = parse_log(UNINITIALIZED_LOG)
    assert r.status == "suspicious"
    assert not r.errors
    rules = {n.rule for n in r.suspicious_notes}
    assert "uninitialized_variable" in rules
    assert "missing_values_generated" in rules


def test_uninitialized_note_carries_explanation():
    r = parse_log(UNINITIALIZED_LOG)
    note = next(n for n in r.suspicious_notes if n.rule == "uninitialized_variable")
    assert "misspelled" in note.explanation.lower()


def test_many_to_many_merge_flagged():
    r = parse_log(MANY_TO_MANY_LOG)
    assert r.status == "suspicious"
    assert {n.rule for n in r.suspicious_notes} == {"many_to_many_merge"}


def test_banner_notes_are_ignored():
    log = (
        "NOTE: Copyright (c) 2016 by SAS Institute Inc., Cary, NC, USA.\n"
        "NOTE: SAS (r) Proprietary Software 9.4 (TS1M7)\n"
        "NOTE: This session is executing on the Linux platform.\n"
    )
    r = parse_log(log)
    assert r.status == "ok"
    assert not r.suspicious_notes


def test_zero_observations_flagged():
    log = (
        "NOTE: The data set WORK.EMPTY has 0 observations and 3 variables.\n"
        "NOTE: DATA statement used (Total process time):\n"
        "      real time           0.00 seconds\n"
    )
    r = parse_log(log)
    assert r.status == "suspicious"
    assert {n.rule for n in r.suspicious_notes} == {"zero_observations"}
    assert r.steps[0].obs_out == 0


def test_unresolved_macro_variable_flagged_from_warning():
    log = "WARNING: Apparent symbolic reference CUTOFF not resolved.\n"
    r = parse_log(log)
    assert r.warnings[0].rule == "symbolic_not_resolved"


def test_event_lists_are_capped():
    log = "NOTE: Variable x is uninitialized.\n" * 500
    r = parse_log(log, max_events=10)
    assert len(r.suspicious_notes) == 10
    # The summary still reports the true total, not the truncated count.
    assert "500" in r.summary


def test_mmss_real_time_parsed():
    log = (
        "NOTE: The data set WORK.BIG has 5 observations and 2 variables.\n"
        "NOTE: DATA statement used (Total process time):\n"
        "      real time           1:03.50 seconds\n"
    )
    r = parse_log(log)
    assert r.steps[0].real_time_sec == 63.5


def test_summary_mentions_created_datasets():
    r = parse_log(CLEAN_LOG)
    assert "WORK.A=19 obs" in r.summary


def test_empty_log_does_not_crash():
    r = parse_log("")
    assert r.status == "ok"
    assert r.log_line_count == 0


# --- regressions found against a live SAS 9.4 ODA session --------------------

# Real IOM logs are paginated: a form-feed-class control character and a page
# header precede the first line of each page.
PAGINATED_LOG = (
    "\x1447                          The SAS System"
    "                    Monday, August 31, 2026 07:08:00 PM\n"
    "\n"
    "263        \n"
    "264        data work.b; set sashelp.class; bmi = weigth / height; run;\n"
    "\x14NOTE: Variable weigth is uninitialized.\n"
    "NOTE: The data set WORK.B has 19 observations and 6 variables.\n"
    "NOTE: DATA statement used (Total process time):\n"
    "      real time           0.02 seconds\n"
)


def test_note_after_page_break_is_still_matched():
    """A control character prefix must not hide a NOTE from triage."""
    r = parse_log(PAGINATED_LOG)
    assert r.status == "suspicious"
    assert {n.rule for n in r.suspicious_notes} == {"uninitialized_variable"}


def test_page_headers_excluded_from_error_context():
    log = (
        "\x1447                          The SAS System"
        "                    Monday, August 31, 2026 07:08:00 PM\n"
        "\n"
        "264        data work.c; set sashelp.nosuchtable; run;\n"
        "ERROR: File SASHELP.NOSUCHTABLE.DATA does not exist.\n"
    )
    ctx = parse_log(log).errors[0].context
    assert any("nosuchtable" in c for c in ctx)
    assert not any("The SAS System" in c for c in ctx)


def test_steps_parsed_from_paginated_log():
    r = parse_log(PAGINATED_LOG)
    assert r.steps[0].dataset == "WORK.B"
    assert r.steps[0].obs_out == 19
