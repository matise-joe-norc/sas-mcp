"""Tests for the SAS code guardrails."""

import pytest

from sas_mcp.guards import Policy, check, strip_comments


DEFAULT = Policy.from_spec()


def rules(code, policy=DEFAULT):
    return {v.rule for v in check(code, policy).violations}


# --- benign code passes ------------------------------------------------------


def test_plain_work_data_step_allowed():
    assert check("data work.a; set sashelp.class; run;", DEFAULT).allowed


def test_one_level_name_is_work_and_allowed():
    assert check("data a; set sashelp.class; run;", DEFAULT).allowed


def test_reading_from_unwritable_lib_is_allowed():
    """Read-only access to a production libref must not be blocked."""
    assert check("data work.a; set prod.sales; run;", DEFAULT).allowed


def test_proc_sql_select_allowed():
    code = "proc sql; select count(*) from prod.sales; quit;"
    assert check(code, DEFAULT).allowed


# --- write allowlist ---------------------------------------------------------


def test_write_outside_work_blocked():
    r = check("data prod.sales; set work.a; run;", DEFAULT)
    assert not r.allowed
    assert "write_outside_allowlist" in {v.rule for v in r.violations}


def test_write_to_allowlisted_lib_permitted():
    p = Policy.from_spec(writable_libs="PROD")
    assert check("data prod.sales; set work.a; run;", p).allowed


def test_sql_create_table_outside_work_blocked():
    assert "write_outside_allowlist" in rules(
        "proc sql; create table prod.x as select * from work.a; quit;"
    )


def test_sql_insert_outside_work_blocked():
    assert "write_outside_allowlist" in rules(
        "proc sql; insert into prod.x values(1); quit;"
    )


def test_out_option_outside_work_blocked():
    assert "write_outside_allowlist" in rules(
        "proc sort data=work.a out=prod.b; by id; run;"
    )


def test_data_step_multiple_targets_flags_only_bad_one():
    r = check("data work.a prod.b; set work.c; run;", DEFAULT)
    assert [v.matched for v in r.violations] == ["PROD.B"]


def test_writable_libs_list_form_and_case_insensitivity():
    p = Policy.from_spec(writable_libs=["prod", "stage"])
    assert check("data PROD.x; set work.a; run;", p).allowed
    assert check("data Stage.y; set work.a; run;", p).allowed


def test_work_always_writable_even_if_not_listed():
    p = Policy.from_spec(writable_libs="PROD")
    assert "WORK" in p.writable_libs


# --- OS escapes --------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "x 'rm -rf /data';",
        "%sysexec rm -rf /data;",
        "systask command 'ls' wait;",
        "data _null_; call system('ls'); run;",
        "filename cmd pipe 'ls -la';",
        "proc python; submit; import os; endsubmit; quit;",
    ],
)
def test_os_escapes_blocked(code):
    r = check(code, DEFAULT)
    assert not r.allowed, f"should have been blocked: {code}"
    assert any(v.category == "os_escape" for v in r.violations)


def test_os_escape_permitted_when_opted_in():
    p = Policy.from_spec(allow_os_escape=True)
    assert check("x 'ls';", p).allowed


def test_variable_named_x_is_not_an_os_escape():
    """`x = 1` is an assignment, not an X statement."""
    assert check("data work.a; x = 1; run;", DEFAULT).allowed


# --- destructive DDL ---------------------------------------------------------


def test_datasets_kill_blocked():
    assert "datasets_kill" in rules("proc datasets lib=prod kill nolist; quit;")


def test_sql_drop_table_blocked():
    assert "sql_drop" in rules("proc sql; drop table prod.sales; quit;")


def test_proc_delete_blocked():
    assert "proc_delete" in rules("proc delete data=work.a; run;")


def test_destructive_permitted_when_opted_in():
    p = Policy.from_spec(writable_libs="PROD", allow_destructive=True)
    assert check("proc datasets lib=prod kill nolist; quit;", p).allowed


def test_kill_as_variable_name_not_flagged():
    """`kill` outside PROC DATASETS/SQL context is just an identifier."""
    assert check("data work.a; kill = 1; run;", DEFAULT).allowed


# --- comment handling --------------------------------------------------------


def test_block_comment_preserves_line_numbers():
    code = "data work.a;\n/* line two\n   line three */\nset work.b;\nrun;"
    assert strip_comments(code).count("\n") == code.count("\n")


def test_destructive_code_inside_comment_ignored():
    assert check("/* proc datasets lib=prod kill; */ data work.a; run;", DEFAULT).allowed


def test_star_comment_ignored():
    assert check("* proc datasets lib=prod kill;\ndata work.a; run;", DEFAULT).allowed


def test_violation_line_numbers_survive_comments():
    code = "/* header\n   more header */\ndata prod.x;\nrun;"
    r = check(code, DEFAULT)
    assert r.violations[0].line_no == 3


# --- reporting ---------------------------------------------------------------


def test_explain_mentions_how_to_widen_policy():
    r = check("data prod.x; run;", DEFAULT)
    text = r.explain()
    assert "--writable-libs" in text
    assert "PROD.X" in text


def test_duplicate_targets_on_one_line_reported_once():
    r = check("proc append base=prod.x data=prod.x; run;", DEFAULT)
    assert len(r.violations) == 1


# --- regressions: statement boundaries, not line boundaries -------------------


def test_agent_cannot_escape_allowlist_by_defining_its_own_libref():
    """SAS allows several statements per line; the DATA target must still be seen."""
    r = check("libname out '/tmp'; data out.x; set work.a; run;", DEFAULT)
    assert not r.allowed
    assert "write_outside_allowlist" in {v.rule for v in r.violations}


def test_uppercase_data_step_target_checked():
    assert "write_outside_allowlist" in rules("DATA PROD.X; SET WORK.A; RUN;")


def test_x_statement_after_semicolon_on_same_line_blocked():
    assert "x_statement" in rules("data work.a; run; x 'rm -rf /';")


def test_rebinding_allowlisted_libref_blocked():
    p = Policy.from_spec(writable_libs="PROD")
    assert "libref_rebinding" in rules("libname prod '/elsewhere';", p)


def test_rebinding_work_blocked():
    assert "libref_rebinding" in rules("libname work '/tmp';")


def test_libname_clear_is_not_rebinding():
    p = Policy.from_spec(writable_libs="PROD")
    assert check("libname prod clear;", p).allowed


def test_libname_for_readonly_lib_is_not_rebinding():
    """Assigning a libref the policy never made writable is not itself a write."""
    assert check("libname arch '/archive';", DEFAULT).allowed


# --- DATA= is a read, not a write --------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "proc freq data=sashelp.prdsale; tables country; run;",
        "proc means data=sashelp.class; run;",
        "proc print data=prod.sales(obs=10); run;",
        "proc contents data=sashelp.cars; run;",
        "proc univariate data=prod.x; var y; run;",
        "proc summary data=prod.sales nway; run;",
    ],
)
def test_proc_data_option_is_a_read(code):
    """DATA= names the input. Treating it as a write blocked every ordinary
    PROC against any library outside WORK -- including SASHELP."""
    assert check(code, DEFAULT).allowed, code


def test_proc_sort_reading_one_lib_and_writing_work_is_allowed():
    assert check(
        "proc sort data=prod.sales out=work.sorted; by id; run;", DEFAULT
    ).allowed


def test_proc_sort_out_to_protected_lib_still_blocked():
    r = check("proc sort data=work.a out=prod.sorted; by id; run;", DEFAULT)
    assert [v.matched for v in r.violations] == ["PROD.SORTED"]


def test_proc_append_base_is_the_write_target():
    """In PROC APPEND, BASE= is written and DATA= is only read."""
    r = check("proc append base=prod.master data=work.new; run;", DEFAULT)
    assert [v.matched for v in r.violations] == ["PROD.MASTER"]


def test_proc_append_reading_protected_lib_into_work_is_allowed():
    assert check("proc append base=work.a data=prod.b; run;", DEFAULT).allowed


# --- PROC DATASETS: report vs. modify ----------------------------------------


def test_proc_datasets_contents_is_read_only():
    assert check(
        "proc datasets lib=sashelp; contents data=class; quit;", DEFAULT
    ).allowed


def test_bare_proc_datasets_listing_is_read_only():
    assert check("proc datasets lib=prod nolist; quit;", DEFAULT).allowed


@pytest.mark.parametrize("verb", ["delete sales", "modify sales", "rename a=b",
                                  "change x=y", "append data=work.a", "age a b"])
def test_proc_datasets_mutations_are_blocked(verb):
    code = f"proc datasets lib=prod nolist; {verb}; quit;"
    r = check(code, DEFAULT)
    assert not r.allowed, code
    assert any(v.rule == "write_outside_allowlist" for v in r.violations)


def test_proc_datasets_mutation_names_the_verb():
    r = check("proc datasets lib=prod; modify sales; quit;", DEFAULT)
    v = next(x for x in r.violations if x.rule == "write_outside_allowlist")
    assert "MODIFY" in v.matched


def test_proc_datasets_mutation_allowed_in_writable_lib():
    p = Policy.from_spec(writable_libs="PROD")
    assert check("proc datasets lib=prod; modify sales; quit;", p).allowed


def test_delete_needs_destructive_opt_in_even_in_a_writable_lib():
    """Deleting data is gated separately from writing it: an allowlisted
    library still does not authorise DELETE."""
    p = Policy.from_spec(writable_libs="PROD")
    r = check("proc datasets lib=prod; delete old; quit;", p)
    assert not r.allowed
    assert {v.rule for v in r.violations} == {"datasets_delete"}

    p2 = Policy.from_spec(writable_libs="PROD", allow_destructive=True)
    assert check("proc datasets lib=prod; delete old; quit;", p2).allowed
