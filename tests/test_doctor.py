"""Tests for the setup diagnostics.

These cover the failure modes that actually generate support requests:
a JRE that isn't there, an authinfo file the world can read, a mistyped ODA
hostname.
"""

import os
import stat
import sys
from pathlib import Path

import pytest

from sas_mcp import doctor


# --- config parsing ----------------------------------------------------------


def write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "sascfg_personal.py"
    p.write_text(body)
    return p


def test_load_config_extracts_named_configs(tmp_path):
    p = write_cfg(
        tmp_path,
        "SAS_config_names=['oda']\noda={'iomhost':'h','iomport':8591}\n",
    )
    check, configs = doctor.load_config(p)
    assert check.status == doctor.PASS
    assert "oda" in configs


def test_load_config_reports_syntax_errors(tmp_path):
    p = write_cfg(tmp_path, "SAS_config_names=[\n")
    check, configs = doctor.load_config(p)
    assert check.status == doctor.FAIL
    assert configs == {}


def test_load_config_flags_names_without_dicts(tmp_path):
    p = write_cfg(tmp_path, "SAS_config_names=['ghost']\n")
    check, configs = doctor.load_config(p)
    assert check.status == doctor.FAIL


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({"java": "java", "iomhost": "h"}, "IOM"),
        # Local Windows SAS: IOM with no iomhost at all.
        ({"java": "java", "encoding": "windows-1252"}, "IOM"),
        ({"provider": "sas.iomprovider"}, "COM"),
        ({"url": "https://x"}, "HTTP"),
        ({"ssh": "ssh"}, "SSH"),
        ({"saspath": "/opt/sas"}, "STDIO"),
        ({}, "unknown"),
    ],
)
def test_access_method_inference(cfg, expected):
    assert doctor.access_method(cfg) == expected


def test_com_config_rejected_off_windows():
    checks = doctor.check_com({"provider": "sas.iomprovider"})
    if sys.platform.startswith("win"):
        assert all(c.status != doctor.FAIL or c.name == "pywin32" for c in checks)
    else:
        assert checks[0].name == "com_platform"
        assert checks[0].status == doctor.FAIL


def test_local_windows_iom_does_not_demand_credentials(tmp_path, monkeypatch):
    """winlocal has no iomhost and needs no authinfo; warning about a missing
    credential file there would be noise."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    cfg = pkg / "sascfg_personal.py"
    cfg.write_text(
        "SAS_config_names=['winlocal']\n"
        "winlocal={'java':'java','encoding':'windows-1252'}\n"
    )
    monkeypatch.setattr(doctor, "find_config",
                        lambda mod, cfgfile=None: ([doctor._ok("config_file", "x")], cfg))
    report = doctor.run_diagnostics(probe_network=False)
    names = {c["name"] for c in report["checks"]}
    assert "iom_local" in names
    assert "authinfo" not in names


# --- ODA hostnames -----------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "odaws01-usw2.oda.sas.com",
        "odaws02-usw2.oda.sas.com",
        "odaws01-euw1.oda.sas.com",
        "odaws01-apse1-2.oda.sas.com",
    ],
)
def test_valid_oda_hostnames_accepted(host):
    checks = doctor.check_iom_hosts({"iomhost": [host]}, probe=False)
    assert not any(c.name == "oda_hostname" for c in checks)


def test_mistyped_oda_hostname_flagged():
    """The real typo this was written for: odawws01 instead of odaws01."""
    checks = doctor.check_iom_hosts(
        {"iomhost": ["odawws01-usw2.oda.sas.com"]}, probe=False
    )
    bad = [c for c in checks if c.name == "oda_hostname"]
    assert len(bad) == 1
    assert bad[0].status == doctor.FAIL


def test_non_oda_hostname_not_pattern_checked():
    """An intranet SAS server has no naming convention to enforce."""
    checks = doctor.check_iom_hosts(
        {"iomhost": ["sas-prod.corp.internal"]}, probe=False
    )
    assert not any(c.name == "oda_hostname" for c in checks)


# --- authinfo ----------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_missing_authinfo_warns(fake_home):
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert checks[0].name == "authinfo"
    assert checks[0].status == doctor.WARN


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_world_readable_authinfo_fails(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me@example.com password secret\n")
    p.chmod(0o644)
    checks = doctor.check_authinfo({"authkey": "oda"})
    perm = next(c for c in checks if c.name == "authinfo_permissions")
    assert perm.status == doctor.FAIL
    assert "chmod 600" in perm.fix


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX permissions")
def test_correctly_locked_authinfo_passes(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me@example.com password secret\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    perm = next(c for c in checks if c.name == "authinfo_permissions")
    assert perm.status == doctor.PASS


def test_missing_authkey_entry_fails(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("other user me password secret\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    entry = next(c for c in checks if c.name == "authinfo_entry")
    assert entry.status == doctor.FAIL
    assert "other" in entry.message


def test_password_with_a_space_is_rejected(fake_home):
    """SASPy requires exactly 5 whitespace-separated fields, so a password
    containing a space makes it skip the line and report a missing key."""
    p = fake_home / ".authinfo"
    p.write_text("oda user me@example.com password my secret pw\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    bad = next(c for c in checks if c.name == "authinfo_format")
    assert bad.status == doctor.FAIL
    assert "space" in bad.message
    assert "PWENCODE" in bad.fix or "{SAS00x}" in bad.fix


def test_truncated_authinfo_line_is_rejected(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me@example.com\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert any(c.name == "authinfo_format" and c.status == doctor.FAIL
               for c in checks)


def test_trailing_comment_on_the_credential_line_is_rejected(fake_home):
    """Extra tokens break it in the same way, from the other direction."""
    p = fake_home / ".authinfo"
    p.write_text("oda user me password pw  # my server\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert any(c.name == "authinfo_format" for c in checks)


def test_well_formed_line_passes(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me@example.com password nospaceshere\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert not any(c.name == "authinfo_format" for c in checks)
    assert any(c.name == "authinfo_entry" and c.status == doctor.PASS
               for c in checks)


def test_other_keys_with_spaces_do_not_trigger_the_check(fake_home):
    """Only the configured authkey's line matters."""
    p = fake_home / ".authinfo"
    p.write_text(
        "other user someone password has spaces here\n"
        "oda user me password fine\n"
    )
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert not any(c.name == "authinfo_format" for c in checks)


def test_non_ascii_password_is_flagged(fake_home):
    """SASPy sends the password UTF-8 encoded to its Java bridge; a charset
    mismatch there corrupts it with nothing pointing at the password."""
    p = fake_home / ".authinfo"
    p.write_text("oda user me password pàssword\n", encoding="utf-8")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    w = next(c for c in checks if c.name == "authinfo_password_charset")
    assert w.status == doctor.WARN
    assert "U+00E0" in w.message
    assert "pwencode" in w.fix.lower()


def test_non_ascii_warning_never_prints_the_password(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me password sûpersecret\n", encoding="utf-8")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    w = next(c for c in checks if c.name == "authinfo_password_charset")
    assert "supersecret" not in w.message
    assert "sûpersecret" not in w.message


def test_ascii_password_not_flagged(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me password plainAscii123\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert not any(c.name == "authinfo_password_charset" for c in checks)


def test_encoded_password_exempt_from_charset_check(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me password {SAS004}ABCDEF\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert not any(c.name == "authinfo_password_charset" for c in checks)


def test_sas_encoded_password_is_accepted(fake_home):
    """Verified against live SAS ODA: a {SAS00x} password authenticates fine,
    so it must not be reported as a problem."""
    p = fake_home / ".authinfo"
    p.write_text("oda user me@example.com password {SAS004}ABCDEF0123\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert all(c.status == doctor.PASS for c in checks)


def test_plaintext_password_accepted(fake_home):
    p = fake_home / ".authinfo"
    p.write_text("oda user me@example.com password plaintextpw\n")
    p.chmod(0o600)
    checks = doctor.check_authinfo({"authkey": "oda"})
    assert all(c.status == doctor.PASS for c in checks)


# --- java --------------------------------------------------------------------


def test_configured_java_path_missing_fails():
    checks = doctor.check_java({"java": "/definitely/not/here/java"})
    assert checks[0].status == doctor.FAIL
    assert checks[0].name == "java_path"


# These two need a *runnable* stand-in for the java binary. A POSIX shell
# script cannot be one on Windows, and the code under test is plain
# platform-independent Python -- so they are exercised on POSIX, and Windows
# gets test_unrunnable_java_reported_as_failure below instead.
posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="fake java binary is a POSIX shell script",
)


@posix_only
def test_java_stub_without_runtime_fails(tmp_path):
    """macOS ships /usr/bin/java as a stub that exits non-zero with no JRE."""
    fake = tmp_path / "java"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'Unable to locate a Java Runtime' >&2\n"
        "exit 1\n"
    )
    fake.chmod(0o755)
    checks = doctor.check_java({"java": str(fake)})
    assert checks[0].status == doctor.FAIL
    assert "JRE" in checks[0].fix


@posix_only
def test_working_java_passes(tmp_path):
    fake = tmp_path / "java"
    fake.write_text(
        "#!/bin/sh\n"
        "echo 'openjdk version \"17.0.9\"' >&2\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    checks = doctor.check_java({"java": str(fake)})
    assert checks[0].status == doctor.PASS
    assert "17.0.9" in checks[0].message


def test_unrunnable_java_reported_as_failure(tmp_path):
    """A file that exists but cannot be executed must be a clean FAIL, not a
    crash -- this is the path Windows takes for a non-executable stub."""
    fake = tmp_path / "java"
    fake.write_text("not an executable\n")
    checks = doctor.check_java({"java": str(fake)})
    assert checks[0].status == doctor.FAIL
    assert "JRE" in checks[0].fix or "java" in checks[0].message.lower()


# --- encoding ----------------------------------------------------------------


def test_missing_encoding_warns():
    assert doctor.check_encoding({}).status == doctor.WARN


@pytest.mark.parametrize("enc", ["utf-8", "utf8", "wlatin1", "latin1"])
def test_known_encodings_pass(enc):
    assert doctor.check_encoding({"encoding": enc}).status == doctor.PASS


def test_unknown_encoding_warns():
    assert doctor.check_encoding({"encoding": "klingon"}).status == doctor.WARN


# --- saspath -----------------------------------------------------------------


def test_missing_saspath_fails():
    checks = doctor.check_saspath({"saspath": "/no/such/sas"})
    assert checks[0].status == doctor.FAIL


def test_present_saspath_passes(tmp_path):
    p = tmp_path / "sas"
    p.write_text("#!/bin/sh\n")
    assert doctor.check_saspath({"saspath": str(p)})[0].status == doctor.PASS


# --- config discovery --------------------------------------------------------


class FakeSaspy:
    """Stands in for the saspy module's config discovery."""

    def __init__(self, pkg_dir, configs):
        self.__file__ = str(pkg_dir / "sasbase.py")
        self._configs = [str(c) for c in configs]

    def list_configs(self):
        return self._configs


def test_explicit_config_file_wins(tmp_path):
    cfg = tmp_path / "mine.py"
    cfg.write_text("SAS_config_names=[]\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    checks, path = doctor.find_config(FakeSaspy(pkg, []), cfgfile=str(cfg))
    assert path == cfg
    assert checks[0].status == doctor.PASS


def test_explicit_config_file_missing_fails(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    checks, path = doctor.find_config(
        FakeSaspy(pkg, []), cfgfile=str(tmp_path / "nope.py")
    )
    assert path is None
    assert checks[0].status == doctor.FAIL


def test_no_config_anywhere_fails(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    checks, path = doctor.find_config(FakeSaspy(pkg, []))
    assert path is None
    assert checks[0].status == doctor.FAIL
    # Points at the generator, the durable location, and the Windows form --
    # a bare link to the docs is what made this the adoption cliff.
    assert "sas-mcp init" in checks[0].fix
    assert ".config/saspy" in checks[0].fix
    assert "USERPROFILE" in checks[0].fix


def test_shipped_template_is_not_treated_as_a_config(tmp_path):
    """With no personal config, saspy.list_configs() returns the sascfg.py
    template it ships. That has placeholder paths, so reporting it as active
    sends a brand-new user chasing errors in a file they never wrote."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    template = pkg / "sascfg.py"
    template.write_text("SAS_config_names=['default']\ndefault={'saspath':'/nope'}\n")

    checks, path = doctor.find_config(FakeSaspy(pkg, [template]))
    assert path is None
    assert checks[0].status == doctor.FAIL
    assert "sas-mcp init" in checks[0].fix


def test_shadowed_configs_are_reported(tmp_path):
    """Several configs can coexist with the least obvious one winning."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    winner = pkg / "sascfg_personal.py"
    other = tmp_path / "home" / ".config" / "saspy" / "sascfg_personal.py"
    other.parent.mkdir(parents=True)
    for f in (winner, other):
        f.write_text("SAS_config_names=[]\n")

    checks, path = doctor.find_config(FakeSaspy(pkg, [winner, other]))
    assert path == winner
    shadow = next(c for c in checks if c.name == "config_shadowing")
    assert shadow.status == doctor.WARN
    assert str(other) in shadow.message


def test_config_inside_saspy_package_warns_about_durability(tmp_path):
    """A config in site-packages is destroyed when the venv is rebuilt."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    cfg = pkg / "sascfg_personal.py"
    cfg.write_text("SAS_config_names=[]\n")
    checks, path = doctor.find_config(FakeSaspy(pkg, [cfg]))
    loc = next(c for c in checks if c.name == "config_location")
    assert loc.status == doctor.WARN
    assert ".config/saspy" in loc.fix


def test_config_outside_package_not_warned(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    cfg = tmp_path / "home" / ".config" / "saspy" / "sascfg_personal.py"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("SAS_config_names=[]\n")
    checks, path = doctor.find_config(FakeSaspy(pkg, [cfg]))
    assert not any(c.name == "config_location" for c in checks)
    assert not any(c.name == "config_shadowing" for c in checks)


def test_saspy_reads_no_environment_variable_for_config(monkeypatch, tmp_path):
    """SASPY_CFG is not a thing -- saspy reads no env var when resolving a
    config, so the doctor must not imply one works."""
    cfg = tmp_path / "elsewhere.py"
    cfg.write_text("SAS_config_names=[]\n")
    monkeypatch.setenv("SASPY_CFG", str(cfg))
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    checks, path = doctor.find_config(FakeSaspy(pkg, []))
    assert path is None  # the env var must be ignored


# --- IOM / encryption jars ---------------------------------------------------


class FakeSaspyPkg:
    def __init__(self, pkg_dir):
        self.__file__ = str(pkg_dir / "sasbase.py")


def _make_jar_tree(tmp_path, *, encryption=True, core=True, thirdparty=True):
    pkg = tmp_path / "saspy"
    iom = pkg / "java" / "iomclient"
    tp = pkg / "java" / "thirdparty"
    iom.mkdir(parents=True)
    tp.mkdir(parents=True)
    if core:
        for j in doctor._IOMCLIENT_JARS:
            (iom / j).write_text("jar")
    if encryption:
        for j in doctor._ENCRYPTION_JARS:
            (iom / j).write_text("jar")
    if thirdparty:
        for j in doctor._THIRDPARTY_JARS:
            (tp / j).write_text("jar")
    return FakeSaspyPkg(pkg), iom


ODA_CFG = {"iomhost": ["odaws01-usw2.oda.sas.com"], "iomport": 8591}
INTRANET_CFG = {"iomhost": "sas-prod.corp.internal", "iomport": 8591}


def test_complete_jar_set_passes(tmp_path):
    mod, _ = _make_jar_tree(tmp_path)
    checks = doctor.check_iom_jars(mod, ODA_CFG)
    assert [c.status for c in checks] == [doctor.PASS]


def test_missing_encryption_jars_fail_for_oda(tmp_path):
    """ODA always requires an encrypted connection, so this is a hard stop."""
    mod, iom = _make_jar_tree(tmp_path, encryption=False)
    checks = doctor.check_iom_jars(mod, ODA_CFG)
    enc = next(c for c in checks if c.name == "encryption_jars")
    assert enc.status == doctor.FAIL
    assert "sas.rutil.jar" in enc.message
    assert doctor.ENCRYPTION_JAR_DOWNLOAD in enc.fix
    # The destination folder must be spelled out, not described.
    assert str(iom) in enc.fix
    assert iom.name == "iomclient"


def test_missing_encryption_jars_are_informational_for_other_servers(tmp_path):
    """An intranet IOM server may not require encryption; SASPy never ships
    these jars, so their absence is normal and must not read as broken."""
    mod, iom = _make_jar_tree(tmp_path, encryption=False)
    checks = doctor.check_iom_jars(mod, INTRANET_CFG)
    enc = next(c for c in checks if c.name == "encryption_jars")
    assert enc.status == doctor.INFO
    # Still tells them where to get it and where to put it.
    assert doctor.ENCRYPTION_JAR_DOWNLOAD in enc.fix
    assert str(iom) in enc.fix


def test_missing_encryption_jars_do_not_block_a_non_oda_run(tmp_path):
    mod, _ = _make_jar_tree(tmp_path, encryption=False)
    checks = doctor.check_iom_jars(mod, INTRANET_CFG)
    assert not any(c.status == doctor.FAIL for c in checks)


def test_missing_encryption_jars_without_config_are_informational(tmp_path):
    mod, _ = _make_jar_tree(tmp_path, encryption=False)
    checks = doctor.check_iom_jars(mod)
    assert next(c for c in checks
                if c.name == "encryption_jars").status == doctor.INFO


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({"iomhost": ["odaws01-usw2.oda.sas.com"]}, True),
        ({"iomhost": "odaws02-euw1.oda.sas.com"}, True),
        ({"iomhost": "sas.corp.internal"}, False),
        ({}, False),
    ],
)
def test_oda_config_detection(cfg, expected):
    assert doctor.is_oda_config(cfg) is expected


def test_partially_missing_encryption_jars_still_flagged(tmp_path):
    mod, iom = _make_jar_tree(tmp_path)
    (iom / "sastpj.rutil.jar").unlink()
    checks = doctor.check_iom_jars(mod, ODA_CFG)
    enc = next(c for c in checks if c.name == "encryption_jars")
    assert "sastpj.rutil.jar" in enc.message
    assert "sas.rutil.jar," not in enc.message  # only the missing one


def test_bundled_jars_still_pass_when_only_encryption_missing(tmp_path):
    """A fresh install is exactly this state; it must not look broken."""
    mod, _ = _make_jar_tree(tmp_path, encryption=False)
    checks = doctor.check_iom_jars(mod, INTRANET_CFG)
    ok = next(c for c in checks if c.name == "iom_jars")
    assert ok.status == doctor.PASS
    assert "bundled" in ok.message


def test_missing_core_jars_suggest_reinstall(tmp_path):
    """These DO ship with SASPy, so a download link would be wrong advice."""
    mod, _ = _make_jar_tree(tmp_path, core=False)
    checks = doctor.check_iom_jars(mod, ODA_CFG)
    core = next(c for c in checks if c.name == "iom_jars")
    assert core.status == doctor.FAIL
    assert "force-reinstall" in core.fix


def test_missing_thirdparty_jars_reported(tmp_path):
    mod, _ = _make_jar_tree(tmp_path, thirdparty=False)
    checks = doctor.check_iom_jars(mod, ODA_CFG)
    assert any(c.name == "iom_thirdparty_jars" for c in checks)


def test_jar_check_skipped_when_no_java_dir(tmp_path):
    """Not every SASPy layout has a java directory; absence is not a failure."""
    pkg = tmp_path / "saspy"
    pkg.mkdir()
    assert doctor.check_iom_jars(FakeSaspyPkg(pkg)) == []


# --- help links --------------------------------------------------------------


def test_report_includes_help_links_when_something_is_wrong():
    report = doctor.run_diagnostics(probe_network=False)
    if report["counts"]["fail"] or report["counts"]["warn"]:
        assert report["help"]["troubleshooting"] == doctor.SASPY_TROUBLESHOOTING


def test_clean_report_omits_help_links():
    report = {
        "verdict": "All checks passed.", "config_name": None,
        "counts": {"pass": 3, "warn": 0, "fail": 0}, "checks": [],
    }
    assert "Still stuck" not in doctor.format_report(report)


def test_format_report_shows_troubleshooting_url_on_failure():
    report = {
        "verdict": "Blocked", "config_name": None,
        "counts": {"pass": 0, "warn": 0, "fail": 1},
        "checks": [{"name": "java", "status": "fail", "message": "no JRE",
                    "fix": "install it"}],
        "help": {"troubleshooting": doctor.SASPY_TROUBLESHOOTING,
                 "configuration": doctor.SASPY_CONFIGURATION},
    }
    text = doctor.format_report(report)
    assert doctor.SASPY_TROUBLESHOOTING in text
    assert "Still stuck" in text


# --- report ------------------------------------------------------------------


def test_run_diagnostics_returns_report_without_network():
    report = doctor.run_diagnostics(probe_network=False)
    assert set(report) >= {"verdict", "counts", "checks"}
    assert report["counts"]["pass"] >= 1


def test_format_report_shows_fixes_for_failures():
    report = {
        "verdict": "Blocked",
        "config_name": "oda",
        "counts": {"pass": 0, "warn": 0, "fail": 1},
        "checks": [
            {"name": "java", "status": "fail", "message": "no JRE",
             "fix": "install temurin"}
        ],
    }
    text = doctor.format_report(report)
    assert "FAIL" in text and "install temurin" in text
