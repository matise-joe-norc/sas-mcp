"""Tests for config generation.

The generated file has to be valid Python that SASPy can import, and the
credential file has to end up owner-only -- a config written with loose
permissions is worse than no config at all.
"""

import stat
import sys
from pathlib import Path

import pytest

from sas_mcp import init_config as ic


def load(text: str) -> dict:
    """Exec generated config text the way SASPy would import it."""
    ns: dict = {}
    exec(compile(text, "sascfg_personal.py", "exec"), ns)  # noqa: S102
    return ns


# --- generated configs are importable and correct ----------------------------


def test_oda_config_is_valid_python_with_expected_shape():
    ns = load(ic.build_oda_config("us1"))
    assert ns["SAS_config_names"] == ["oda"]
    cfg = ns["oda"]
    assert cfg["iomport"] == 8591
    assert cfg["authkey"] == "oda"
    assert cfg["encoding"] == "utf-8"
    assert cfg["iomhost"] == [
        "odaws01-usw2.oda.sas.com", "odaws02-usw2.oda.sas.com",
    ]


@pytest.mark.parametrize("region", [r.key for r in ic.ODA_REGIONS])
def test_every_oda_region_generates_valid_hostnames(region):
    """Guards against the odawws01-style typo that broke the original config."""
    import re
    ns = load(ic.build_oda_config(region))
    pattern = re.compile(
        r"^odaws\d{2}-(usw2|euw1|apse1)(-\d)?\.oda\.sas\.com$"
    )
    hosts = ns[ns["SAS_config_names"][0]]["iomhost"]
    assert hosts
    for h in hosts:
        assert pattern.match(h), h


def test_unknown_oda_region_rejected():
    with pytest.raises(ValueError, match="Unknown ODA region"):
        ic.build_oda_config("mars")


def test_stdio_config_needs_no_java():
    ns = load(ic.build_stdio_config("/opt/sas/sas"))
    cfg = ns["local"]
    assert cfg["saspath"] == "/opt/sas/sas"
    assert "java" not in cfg


def test_ssh_config_shape():
    ns = load(ic.build_ssh_config("sas.corp", "/opt/sas/sas", user="me"))
    cfg = ns["remote"]
    assert cfg["ssh"] == "ssh"
    assert cfg["host"] == "sas.corp"
    assert cfg["luser"] == "me"


def test_ssh_config_omits_user_when_not_given():
    ns = load(ic.build_ssh_config("sas.corp", "/opt/sas/sas"))
    assert "luser" not in ns["remote"]


def test_iom_config_shape():
    ns = load(ic.build_iom_config("sas.corp", 8592, name="prod"))
    cfg = ns["prod"]
    assert cfg["iomhost"] == "sas.corp"
    assert cfg["iomport"] == 8592
    assert cfg["authkey"] == "prod"


def test_winlocal_config_matches_saspys_own_stanza():
    """Local Windows IOM is selected by the ABSENCE of iomhost. SASPy starts
    the local session itself, so no host, port, or classpath belongs here."""
    ns = load(ic.build_winlocal_config())
    cfg = ns["winlocal"]
    assert cfg["encoding"] == "windows-1252"
    assert "java" in cfg
    assert "iomhost" not in cfg
    assert "iomport" not in cfg
    assert "classpath" not in cfg


def test_wincom_config_needs_no_java():
    """COM's whole advantage on Windows is skipping the Java dependency."""
    ns = load(ic.build_wincom_config())
    cfg = ns["wincom"]
    assert cfg["provider"] == "sas.iomprovider"
    assert "java" not in cfg
    assert "classpath" not in cfg


def test_custom_config_name_used_throughout():
    ns = load(ic.build_oda_config("eu1", name="myoda"))
    assert ns["SAS_config_names"] == ["myoda"]
    assert ns["myoda"]["authkey"] == "myoda"


def test_java_path_embedded_when_supplied():
    ns = load(ic.build_oda_config("us1", java="/opt/jdk/bin/java"))
    assert ns["oda"]["java"] == "/opt/jdk/bin/java"


def test_java_defaults_to_path_lookup():
    assert load(ic.build_oda_config("us1"))["oda"]["java"] == "java"


# --- writing the config ------------------------------------------------------


def test_write_creates_parent_directories(tmp_path):
    target = tmp_path / "nested" / "deeper" / "sascfg_personal.py"
    ic.write_config(ic.build_oda_config("us1"), target)
    assert target.is_file()


def test_write_refuses_to_clobber(tmp_path):
    target = tmp_path / "sascfg_personal.py"
    ic.write_config(ic.build_oda_config("us1"), target)
    with pytest.raises(ic.ConfigExists):
        ic.write_config(ic.build_oda_config("eu1"), target)


def test_force_overwrites(tmp_path):
    target = tmp_path / "sascfg_personal.py"
    ic.write_config(ic.build_oda_config("us1"), target)
    ic.write_config(ic.build_oda_config("eu1"), target, force=True)
    assert "euw1" in target.read_text()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_written_config_is_owner_only(tmp_path):
    target = tmp_path / "sascfg_personal.py"
    ic.write_config(ic.build_oda_config("us1"), target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# --- authinfo ----------------------------------------------------------------


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_authinfo_written_owner_only(tmp_path):
    """The password is stored in the clear, so permissions are the only
    protection and must not be left to chance."""
    p = tmp_path / ".authinfo"
    ic.write_authinfo_entry("oda", "me@example.com", "hunter2", path=p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_authinfo_entry_format_matches_what_saspy_reads(tmp_path):
    p = tmp_path / ".authinfo"
    ic.write_authinfo_entry("oda", "me@example.com", "hunter2", path=p)
    assert p.read_text() == "oda user me@example.com password hunter2\n"


def test_authinfo_append_preserves_existing_entries(tmp_path):
    p = tmp_path / ".authinfo"
    p.write_text("other user someone password secret\n")
    ic.write_authinfo_entry("oda", "me@example.com", "pw", path=p)
    text = p.read_text()
    assert "other user someone password secret" in text
    assert "oda user me@example.com password pw" in text


def test_authinfo_handles_file_without_trailing_newline(tmp_path):
    p = tmp_path / ".authinfo"
    p.write_text("other user someone password secret")  # no newline
    ic.write_authinfo_entry("oda", "me", "pw", path=p)
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2


def test_authinfo_refuses_duplicate_key(tmp_path):
    p = tmp_path / ".authinfo"
    ic.write_authinfo_entry("oda", "me", "pw", path=p)
    with pytest.raises(ic.ConfigExists):
        ic.write_authinfo_entry("oda", "other", "pw2", path=p)


def test_authinfo_force_replaces_only_the_named_entry(tmp_path):
    p = tmp_path / ".authinfo"
    p.write_text("keep user a password b\noda user old password oldpw\n")
    ic.write_authinfo_entry("oda", "new", "newpw", path=p, force=True)
    text = p.read_text()
    assert "keep user a password b" in text
    assert "oldpw" not in text
    assert "oda user new password newpw" in text


def test_authinfo_has_key_ignores_comments(tmp_path):
    p = tmp_path / ".authinfo"
    p.write_text("# oda user commented password out\n")
    assert ic.authinfo_has_key("oda", p) is False


def test_authinfo_has_key_on_missing_file(tmp_path):
    assert ic.authinfo_has_key("oda", tmp_path / "nope") is False


# --- platform paths ----------------------------------------------------------


def test_default_config_path_is_the_home_location():
    p = ic.default_config_path()
    assert p.name == "sascfg_personal.py"
    assert p.parent.name == "saspy"
    assert p.parent.parent.name == ".config"


def test_authinfo_filename_matches_platform():
    """SASPy reads _authinfo on Windows and .authinfo everywhere else."""
    expected = "_authinfo" if sys.platform.startswith("win") else ".authinfo"
    assert ic.authinfo_path().name == expected


# --- CLI wiring --------------------------------------------------------------


def test_cli_init_writes_config_non_interactively(tmp_path):
    from sas_mcp.cli import main

    target = tmp_path / "sascfg_personal.py"
    rc = main(["init", "--deployment", "oda", "--region", "eu1",
               "--path", str(target)])
    assert rc == 0
    ns = load(target.read_text())
    assert "euw1" in ns["oda"]["iomhost"][0]


def test_cli_init_refuses_existing_config_without_force(tmp_path, capsys):
    from sas_mcp.cli import main

    target = tmp_path / "sascfg_personal.py"
    main(["init", "--deployment", "oda", "--region", "us1", "--path", str(target)])
    rc = main(["init", "--deployment", "oda", "--region", "us1",
               "--path", str(target)])
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_cli_init_force_overwrites(tmp_path):
    from sas_mcp.cli import main

    target = tmp_path / "sascfg_personal.py"
    main(["init", "--deployment", "oda", "--region", "us1", "--path", str(target)])
    rc = main(["init", "--deployment", "oda", "--region", "ap1",
               "--path", str(target), "--force"])
    assert rc == 0
    assert "apse1" in target.read_text()


def test_cli_init_generated_config_is_importable_by_saspys_rules(tmp_path):
    """The generated file must be plain importable Python with the two names
    SASPy looks for."""
    from sas_mcp.cli import main

    target = tmp_path / "sascfg_personal.py"
    main(["init", "--deployment", "oda", "--region", "us1", "--path", str(target)])
    ns = load(target.read_text())
    names = ns["SAS_config_names"]
    assert names and all(isinstance(ns[n], dict) for n in names)
