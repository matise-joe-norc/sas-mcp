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


# --- merging into an existing config -----------------------------------------


def test_parse_config_names_reads_the_list():
    assert ic.parse_config_names("SAS_config_names = ['oda', 'winlocal']\n") == [
        "oda", "winlocal",
    ]


def test_parse_config_names_handles_double_quotes_and_spacing():
    assert ic.parse_config_names('SAS_config_names=["a" ,  "b"]') == ["a", "b"]


def test_parse_config_names_returns_none_when_absent():
    assert ic.parse_config_names("# nothing here\n") is None


def test_merge_adds_new_name_and_keeps_the_old_one():
    existing = ic.build_oda_config("us1", name="oda")
    generated = ic.build_stdio_config("/opt/sas/sas", name="local")
    merged = ic.merge_config(existing, generated)
    ns = load(merged)
    assert ns["SAS_config_names"] == ["oda", "local"]
    assert ns["oda"]["iomport"] == 8591
    assert ns["local"]["saspath"] == "/opt/sas/sas"


def test_merge_preserves_hand_written_content():
    """A user's own comments and edits must survive an append."""
    existing = (
        "# my notes about this server\n"
        "SAS_config_names = ['mine']\n"
        "mine = {'saspath': '/opt/sas/sas'}  # trailing comment\n"
    )
    merged = ic.merge_config(existing, ic.build_oda_config("us1"))
    assert "# my notes about this server" in merged
    assert "# trailing comment" in merged
    ns = load(merged)
    assert ns["SAS_config_names"] == ["mine", "oda"]
    assert ns["mine"]["saspath"] == "/opt/sas/sas"


def test_merge_refuses_a_duplicate_name():
    existing = ic.build_oda_config("us1", name="oda")
    with pytest.raises(ic.ConfigExists, match="already defines"):
        ic.merge_config(existing, ic.build_oda_config("eu1", name="oda"))


def test_merge_refuses_a_file_it_cannot_parse():
    with pytest.raises(ic.ConfigExists, match="SAS_config_names"):
        ic.merge_config("total nonsense\n", ic.build_oda_config("us1"))


def test_merged_file_is_valid_python_with_three_configs():
    text = ic.build_oda_config("us1", name="oda")
    text = ic.merge_config(text, ic.build_stdio_config("/opt/sas", name="a"))
    text = ic.merge_config(text, ic.build_wincom_config(name="b"))
    ns = load(text)
    assert ns["SAS_config_names"] == ["oda", "a", "b"]
    assert all(isinstance(ns[n], dict) for n in ns["SAS_config_names"])


# --- the existing-config prompt ----------------------------------------------


def _existing(tmp_path):
    p = tmp_path / "sascfg_personal.py"
    p.write_text(ic.build_oda_config("us1", name="oda"))
    return p


def test_keep_leaves_the_file_untouched(tmp_path, monkeypatch):
    p = _existing(tmp_path)
    before = p.read_text()
    monkeypatch.setattr(ic, "_choose", lambda *a, **k: "keep")
    assert ic.resolve_existing_config(
        p, ic.build_stdio_config("/opt/sas", name="local"), "local"
    ) == "kept"
    assert p.read_text() == before


def test_append_adds_the_new_config_and_backs_up(tmp_path, monkeypatch):
    p = _existing(tmp_path)
    monkeypatch.setattr(ic, "_choose", lambda *a, **k: "append")
    assert ic.resolve_existing_config(
        p, ic.build_stdio_config("/opt/sas", name="local"), "local"
    ) == "appended"
    ns = load(p.read_text())
    assert ns["SAS_config_names"] == ["oda", "local"]
    assert (tmp_path / "sascfg_personal.py.bak").is_file()


def test_replace_overwrites_but_backs_up_first(tmp_path, monkeypatch):
    p = _existing(tmp_path)
    monkeypatch.setattr(ic, "_choose", lambda *a, **k: "replace")
    assert ic.resolve_existing_config(
        p, ic.build_stdio_config("/opt/sas", name="local"), "local"
    ) == "replaced"
    ns = load(p.read_text())
    assert ns["SAS_config_names"] == ["local"]
    backup = load((tmp_path / "sascfg_personal.py.bak").read_text())
    assert backup["SAS_config_names"] == ["oda"]


def test_append_with_colliding_name_offers_replace(tmp_path, monkeypatch):
    p = _existing(tmp_path)
    monkeypatch.setattr(ic, "_choose", lambda *a, **k: "append")
    monkeypatch.setattr(ic, "_confirm", lambda *a, **k: True)
    assert ic.resolve_existing_config(
        p, ic.build_oda_config("eu1", name="oda"), "oda"
    ) == "replaced"
    assert "euw1" in p.read_text()


def test_append_with_colliding_name_can_be_cancelled(tmp_path, monkeypatch):
    p = _existing(tmp_path)
    before = p.read_text()
    monkeypatch.setattr(ic, "_choose", lambda *a, **k: "append")
    monkeypatch.setattr(ic, "_confirm", lambda *a, **k: False)
    assert ic.resolve_existing_config(
        p, ic.build_oda_config("eu1", name="oda"), "oda"
    ) == "cancelled"
    assert p.read_text() == before


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_backup_is_owner_only(tmp_path, monkeypatch):
    """The backup can contain the same hostnames and user names as the
    original, so it must not be left world-readable."""
    p = _existing(tmp_path)
    monkeypatch.setattr(ic, "_choose", lambda *a, **k: "append")
    ic.resolve_existing_config(
        p, ic.build_stdio_config("/opt/sas", name="local"), "local"
    )
    bak = tmp_path / "sascfg_personal.py.bak"
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600


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


# --- Windows path rendering --------------------------------------------------


def test_windows_java_path_rendered_as_raw_string():
    """repr() would double every backslash -- valid, but wrong-looking in a
    file whose whole purpose is to be hand-edited."""
    text = ic.build_oda_config(
        "us1", java=r"C:\Program Files\SASHome\jre\bin\java.exe"
    )
    assert r"r'C:\Program Files\SASHome\jre\bin\java.exe'" in text
    assert "\\\\" not in text


def test_windows_path_config_still_imports_correctly():
    """The raw string must round-trip to the exact original path."""
    p = r"C:\Program Files\Java\jdk-17\bin\java.exe"
    ns = load(ic.build_oda_config("us1", java=p))
    assert ns["oda"]["java"] == p


def test_posix_path_uses_plain_quotes():
    text = ic.build_oda_config("us1", java="/usr/bin/java")
    assert "'/usr/bin/java'" in text
    assert "r'/usr/bin/java'" not in text


def test_list_values_render_each_element():
    ns = load(ic.build_oda_config("us1"))
    assert len(ns["oda"]["iomhost"]) == 2


# --- Java discovery ----------------------------------------------------------


def test_java_from_dir_accepts_the_executable_itself(tmp_path):
    exe = tmp_path / ("java.exe" if sys.platform.startswith("win") else "java")
    exe.write_text("x")
    assert ic.java_from_dir(exe) == exe


def test_java_from_dir_accepts_a_java_home(tmp_path):
    """People paste JAVA_HOME as often as the executable path."""
    name = "java.exe" if sys.platform.startswith("win") else "java"
    exe = tmp_path / "bin" / name
    exe.parent.mkdir()
    exe.write_text("x")
    assert ic.java_from_dir(tmp_path) == exe


def test_java_from_dir_accepts_a_jre_subfolder(tmp_path):
    """SAS's private JRE nests the executable under jre/bin."""
    name = "java.exe" if sys.platform.startswith("win") else "java"
    exe = tmp_path / "jre" / "bin" / name
    exe.parent.mkdir(parents=True)
    exe.write_text("x")
    assert ic.java_from_dir(tmp_path) == exe


def test_java_from_dir_returns_none_when_absent(tmp_path):
    assert ic.java_from_dir(tmp_path) is None


def test_resolve_java_arg_rejects_a_bad_path(tmp_path):
    with pytest.raises(ic.ConfigExists, match="No java executable"):
        ic.resolve_java_arg(str(tmp_path / "nope"))


def test_resolve_java_arg_strips_copied_quotes(tmp_path):
    """Windows 'Copy as path' wraps the path in double quotes."""
    with pytest.raises(ic.ConfigExists, match="No java executable"):
        ic.resolve_java_arg(f'"{tmp_path / "nope"}"')


def test_resolve_java_arg_falls_back_to_detection():
    assert ic.resolve_java_arg(None) == ic.detect_java()


# --- Windows Java discovery (fixture tree, runs on any platform) -------------


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x")
    return p


def test_finds_the_jre_that_ships_with_sas(tmp_path):
    """SAS 9.4 for Windows bundles a private JRE. Anyone with local Windows
    SAS already has Java, so finding this means they install nothing."""
    sashome = tmp_path / "SASHome"
    jre = _touch(
        sashome / "SASPrivateJavaRuntimeEnvironment" / "9.4" / "jre" / "bin"
        / "java.exe"
    )
    found = ic._windows_candidates(sas_homes=[sashome], program_files=[])
    assert str(jre) in found


def test_sas_jre_is_preferred_over_other_jdks(tmp_path):
    sashome = tmp_path / "SASHome"
    sas_jre = _touch(
        sashome / "SASPrivateJavaRuntimeEnvironment" / "9.4" / "jre" / "bin"
        / "java.exe"
    )
    pf = tmp_path / "Program Files"
    _touch(pf / "Java" / "jdk-17" / "bin" / "java.exe")
    found = ic._windows_candidates(sas_homes=[sashome], program_files=[pf])
    assert found[0] == str(sas_jre)


def test_finds_vendor_jdks_in_program_files(tmp_path):
    pf = tmp_path / "Program Files"
    adoptium = _touch(pf / "Eclipse Adoptium" / "jdk-21" / "bin" / "java.exe")
    found = ic._windows_candidates(sas_homes=[], program_files=[pf])
    assert str(adoptium) in found


def test_newer_jdk_versions_come_first(tmp_path):
    pf = tmp_path / "Program Files"
    _touch(pf / "Java" / "jdk-11" / "bin" / "java.exe")
    newer = _touch(pf / "Java" / "jdk-21" / "bin" / "java.exe")
    found = ic._windows_candidates(sas_homes=[], program_files=[pf])
    assert found[0] == str(newer)


def test_windows_search_tolerates_missing_directories(tmp_path):
    assert ic._windows_candidates(
        sas_homes=[tmp_path / "nothing"], program_files=[tmp_path / "nope"]
    ) == []


# --- guidance when Java is not found -----------------------------------------


def test_unverified_java_help_names_the_file_to_edit(tmp_path):
    target = tmp_path / "sascfg_personal.py"
    msg = ic.unverified_java_help(target)
    assert str(target) in msg
    assert "'java'" in msg
    assert "doctor" in msg


def test_prompt_for_java_accepts_a_verified_path(tmp_path, monkeypatch):
    exe = tmp_path / "java"
    exe.write_text("x")
    monkeypatch.setattr(ic, "_java_runs", lambda p: True)
    monkeypatch.setattr(ic, "java_from_dir", lambda p: exe)
    assert ic.prompt_for_java(ask=lambda _: str(exe)) == str(exe)


def test_prompt_for_java_skips_on_blank_input():
    assert ic.prompt_for_java(ask=lambda _: "") is None


def test_prompt_for_java_gives_up_after_repeated_bad_input(tmp_path):
    """Must not loop forever when the user keeps mistyping."""
    assert ic.prompt_for_java(ask=lambda _: str(tmp_path / "nope")) is None


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
