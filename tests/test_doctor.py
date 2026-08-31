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
        ({"iomhost": "h"}, "IOM"),
        ({"url": "https://x"}, "HTTP"),
        ({"ssh": "ssh"}, "SSH"),
        ({"saspath": "/opt/sas"}, "STDIO"),
        ({}, "unknown"),
    ],
)
def test_access_method_inference(cfg, expected):
    assert doctor.access_method(cfg) == expected


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


def test_java_stub_without_runtime_fails(tmp_path, monkeypatch):
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
