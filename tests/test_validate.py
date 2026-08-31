"""Tests for PROC COMPARE decoding and the assertion macro layer."""

import pytest

from sas_mcp import validate
from sas_mcp.validate import (
    decode_sysinfo,
    parse_assertions,
    summarize_assertions,
)


# --- SYSINFO decoding --------------------------------------------------------


def test_zero_sysinfo_means_identical():
    r = decode_sysinfo(0)
    assert r["identical"] is True
    assert r["data_differs"] is False
    assert r["findings"] == []


def test_value_difference_is_a_data_difference():
    r = decode_sysinfo(4096)
    assert r["identical"] is False
    assert r["data_differs"] is True
    assert r["metadata_only"] is False
    assert [f["code"] for f in r["findings"]] == ["value"]


def test_label_difference_is_metadata_only():
    """A differing variable label must not read as 'the data disagrees'."""
    r = decode_sysinfo(32)
    assert r["data_differs"] is False
    assert r["metadata_only"] is True


def test_format_and_length_together_still_metadata_only():
    r = decode_sysinfo(8 | 16)
    assert r["metadata_only"] is True
    assert {f["code"] for f in r["findings"]} == {"format", "length"}


def test_combined_bits_decoded_individually():
    # base has rows the comparison lacks, plus unequal values
    r = decode_sysinfo(64 | 4096)
    assert {f["code"] for f in r["findings"]} == {"base_obs", "value"}
    assert r["data_differs"] is True


def test_fatal_bit_flagged():
    r = decode_sysinfo(32768)
    assert [f["code"] for f in r["findings"]] == ["fatal"]
    assert r["data_differs"] is True


def test_all_bits_have_distinct_codes():
    codes = [c for _, c, _ in validate.SYSINFO_BITS]
    assert len(codes) == len(set(codes))


# --- name qualification ------------------------------------------------------


def test_bare_name_defaults_to_work():
    assert validate._qualify("out") == "WORK.OUT"


def test_two_level_name_preserved():
    assert validate._qualify("sashelp.class") == "SASHELP.CLASS"


@pytest.mark.parametrize("bad", ["a.b.c", "drop table x", "1abc", ""])
def test_invalid_dataset_names_rejected(bad):
    with pytest.raises(ValueError):
        validate._qualify(bad)


def test_by_columns_validated():
    assert validate._validate_columns("id, name") == ["id", "name"]


def test_injected_by_clause_rejected():
    with pytest.raises(ValueError):
        validate._validate_columns("id; quit; proc datasets kill")


# --- sysinfo extraction ------------------------------------------------------


def test_sysinfo_extracted_from_log():
    log = "NOTE: something\nSASMCP_SYSINFO|4096\nNOTE: done\n"
    assert validate._extract_sysinfo(log) == 4096


def test_last_sysinfo_wins():
    log = "SASMCP_SYSINFO|0\nSASMCP_SYSINFO|4096\n"
    assert validate._extract_sysinfo(log) == 4096


def test_missing_sysinfo_returns_none():
    assert validate._extract_sysinfo("NOTE: nothing here\n") is None


# --- assertion parsing -------------------------------------------------------


def test_parse_assertions_reads_markers():
    log = (
        "NOTE: noise\n"
        "SASMCP_ASSERT|PASS|assert_rows|WORK.OUT has 19 rows\n"
        "SASMCP_ASSERT|FAIL|assert_unique|3 duplicate id value(s) in WORK.OUT\n"
    )
    a = parse_assertions(log)
    assert len(a) == 2
    assert a[0]["status"] == "PASS"
    assert a[1]["name"] == "assert_unique"
    assert "3 duplicate" in a[1]["detail"]


def test_parse_assertions_ignores_ordinary_log_lines():
    assert parse_assertions("NOTE: The data set WORK.A has 1 observations.\n") == []


def test_summary_counts_failures():
    a = [
        {"status": "PASS", "name": "a", "detail": ""},
        {"status": "FAIL", "name": "b", "detail": ""},
    ]
    s = summarize_assertions(a)
    assert s == {
        "total": 2, "passed": 1, "failed": 1,
        "verdict": "1 of 2 assertions FAILED.",
    }


def test_summary_of_all_passing():
    a = [{"status": "PASS", "name": "a", "detail": ""}]
    assert summarize_assertions(a)["verdict"] == "All 1 assertions passed."


def test_summary_when_no_assertions_ran_tells_the_model_what_to_do():
    s = summarize_assertions([])
    assert s["total"] == 0
    assert "%assert_rows" in s["verdict"]


# --- macro library sanity ----------------------------------------------------


def test_macro_library_defines_expected_assertions():
    src = validate.ASSERT_MACROS
    for name in [
        "assert_exists", "assert_rows", "assert_not_empty",
        "assert_no_missing", "assert_unique", "assert_equal_datasets",
        "assert_condition",
    ]:
        assert f"%macro {name}(" in src, name
        assert f"%mend {name};" in src, name


def test_macro_library_emits_parseable_markers():
    """Every %put in the library must match the parser's expected shape."""
    import re
    puts = re.findall(r"%put (SASMCP_ASSERT\|[^;]+);", validate.ASSERT_MACROS)
    assert puts
    for p in puts:
        # Substitute macro variable references with sample text, then parse.
        concrete = re.sub(r"&\w+\.?", "x", p)
        assert parse_assertions(concrete), p
