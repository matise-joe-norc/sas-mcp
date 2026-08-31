"""Diagnostics for SASPy setup.

Nearly all of the support burden for a package like this is configuration, not
code: a missing Java runtime for IOM, an ``~/.authinfo`` the OS lets everyone
read, a mistyped ODA hostname, an encoding mismatch that turns results into
mojibake. Each has a crisp signature and a one-line fix, so the doctor checks
them directly and says what to do rather than surfacing a stack trace.

Every check runs without connecting to SAS, so this works when the connection
is exactly what is broken.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"

# ODA's documented workspace servers, e.g. odaws01-usw2.oda.sas.com
_ODA_HOST_RE = re.compile(
    r"^odaws\d{2}-(?:usw2|euw1|apse1)(?:-\d)?\.oda\.sas\.com$", re.IGNORECASE
)


@dataclass
class Check:
    name: str
    status: str
    message: str
    fix: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ok(n, m, fix=""):
    return Check(n, PASS, m, fix)


def _warn(n, m, fix=""):
    return Check(n, WARN, m, fix)


def _fail(n, m, fix=""):
    return Check(n, FAIL, m, fix)


# --- individual checks -------------------------------------------------------


def check_python() -> Check:
    v = sys.version_info
    if v < (3, 10):
        return _fail(
            "python", f"Python {v.major}.{v.minor} is too old.",
            "sas-mcp needs Python 3.10 or newer.",
        )
    return _ok("python", f"Python {v.major}.{v.minor}.{v.micro}")


def check_saspy() -> tuple[Check, Any]:
    try:
        import saspy
    except ImportError:
        return (
            _fail("saspy", "saspy is not installed.", "pip install saspy"),
            None,
        )
    return _ok("saspy", f"saspy {saspy.__version__}"), saspy


def check_pandas() -> Check:
    try:
        import pandas
    except ImportError:
        return _fail(
            "pandas", "pandas is not installed.",
            "pip install pandas -- needed to return data as records.",
        )
    return _ok("pandas", f"pandas {pandas.__version__}")


def find_config(
    saspy_mod: Any, cfgfile: str | None = None
) -> tuple[list[Check], Path | None]:
    """Locate sascfg_personal.py the way SASPy itself would.

    SASPy searches its own package directory first, then the working
    directory, and only then ``~/.config/saspy`` -- so several config files can
    coexist with the least obvious one winning. Note there is no environment
    variable for this: SASPy reads no environment at all when resolving a
    config, so only an explicit ``cfgfile`` is unambiguous.
    """
    if cfgfile:
        path = Path(cfgfile).expanduser()
        if path.is_file():
            return [_ok("config_file", f"Using explicit config file {path}")], path
        return (
            [
                _fail(
                    "config_file",
                    f"--config-file points at {path}, which does not exist.",
                    "Pass the full path to your sascfg_personal.py.",
                )
            ],
            None,
        )

    found: list[Path] = []
    try:
        found = [Path(p) for p in saspy_mod.list_configs()]
    except Exception:  # pragma: no cover - older saspy without list_configs
        for cand in (
            Path(saspy_mod.__file__).parent / "sascfg_personal.py",
            Path.cwd() / "sascfg_personal.py",
            Path.home() / ".config" / "saspy" / "sascfg_personal.py",
        ):
            if cand.is_file():
                found.append(cand)

    # When no personal config exists anywhere, SASPy falls back to the
    # template it ships (sascfg.py). That is not a usable configuration -- it
    # holds placeholder paths -- so treat it as "nothing configured" rather
    # than reporting it as the active config and then cascading confusing
    # errors about its dummy entries.
    found = [p for p in found if p.name == "sascfg_personal.py"]

    if not found:
        return (
            [
                _fail(
                    "config_file",
                    "No sascfg_personal.py found in any location SASPy searches.",
                    "Run `sas-mcp init` to create one -- it asks where your "
                    "SAS runs and writes the file to "
                    "~/.config/saspy/sascfg_personal.py (on Windows: "
                    "%USERPROFILE%\\.config\\saspy\\). To write it by hand "
                    "instead, see "
                    "https://sassoftware.github.io/saspy/configuration.html",
                )
            ],
            None,
        )

    winner = found[0]
    checks = [_ok("config_file", f"Using {winner}")]

    if len(found) > 1:
        others = ", ".join(str(p) for p in found[1:])
        checks.append(
            _warn(
                "config_shadowing",
                f"{len(found)} config files exist; {winner} takes precedence "
                f"and the rest are ignored: {others}",
                "Keep exactly one, or pass --config-file to name the one you "
                "mean explicitly.",
            )
        )

    if _is_inside_package(winner, saspy_mod):
        checks.append(
            _warn(
                "config_location",
                f"The active config lives inside the saspy package "
                f"({winner}). Reinstalling or rebuilding the environment will "
                f"delete it.",
                "Move it to ~/.config/saspy/sascfg_personal.py (Windows: "
                "%USERPROFILE%\\.config\\saspy\\), which survives reinstalls. "
                "Delete the copy in the package directory, or it will keep "
                "taking precedence.",
            )
        )

    return checks, winner


def _is_inside_package(path: Path, saspy_mod: Any) -> bool:
    try:
        pkg = Path(saspy_mod.__file__).parent.resolve()
        return pkg in path.resolve().parents or path.resolve().parent == pkg
    except Exception:  # pragma: no cover
        return False


def load_config(path: Path) -> tuple[Check, dict[str, Any]]:
    """Exec the config file and pull out the named connection dicts."""
    namespace: dict[str, Any] = {}
    try:
        compiled = compile(path.read_text(), str(path), "exec")
        exec(compiled, namespace)  # noqa: S102 - the user's own config file
    except Exception as exc:
        return (
            _fail("config_parse", f"Could not read {path}: {exc}",
                  "Fix the Python syntax in the config file."),
            {},
        )

    names = namespace.get("SAS_config_names", [])
    configs = {n: namespace[n] for n in names if isinstance(namespace.get(n), dict)}
    if not configs:
        return (
            _fail(
                "config_parse",
                f"{path} defines no usable configurations "
                f"(SAS_config_names = {names!r}).",
                "Ensure SAS_config_names lists dicts defined in the same file.",
            ),
            {},
        )
    return _ok("config_parse", f"Configurations: {', '.join(configs)}"), configs


def access_method(cfg: dict[str, Any]) -> str:
    """Infer which SASPy access method a config selects."""
    if "iomhost" in cfg:
        return "IOM"
    if "url" in cfg:
        return "HTTP"
    if "ssh" in cfg:
        return "SSH"
    if "saspath" in cfg:
        return "STDIO"
    return "unknown"


def check_java(cfg: dict[str, Any]) -> list[Check]:
    """IOM needs a working JRE. A path that exists is not the same as a JRE."""
    out: list[Check] = []
    configured = cfg.get("java")
    exe = configured or shutil.which("java")

    if not exe:
        return [
            _fail(
                "java",
                "No Java runtime found, and IOM requires one.",
                "Install a JRE (e.g. `brew install --cask temurin` on macOS, or "
                "your platform's OpenJDK), then set 'java' in the config to its "
                "full path.",
            )
        ]

    if configured and not Path(configured).exists():
        out.append(
            _fail("java_path", f"Configured java path does not exist: {configured}",
                  "Point 'java' at a real java executable, or remove it to use PATH.")
        )
        return out

    try:
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [_fail("java", f"Could not run {exe}: {exc}", "Install a working JRE.")]

    banner = (proc.stderr or proc.stdout or "").strip().splitlines()
    first = banner[0] if banner else ""

    if proc.returncode != 0 or "Unable to locate a Java Runtime" in (
        proc.stderr or ""
    ):
        out.append(
            _fail(
                "java",
                f"{exe} exists but no Java runtime is installed "
                f"({first or 'no version output'}).",
                "On macOS /usr/bin/java is only a stub. Install a real JRE, e.g. "
                "`brew install --cask temurin`, then set 'java' to the full path "
                "from `/usr/libexec/java_home`.",
            )
        )
    else:
        out.append(_ok("java", f"{exe}: {first}"))
    return out


def check_authinfo(cfg: dict[str, Any]) -> list[Check]:
    """Presence, permissions, and a matching entry for the config's authkey."""
    out: list[Check] = []
    win = sys.platform.startswith("win")
    path = Path.home() / ("_authinfo" if win else ".authinfo")

    if not path.exists():
        alt = Path.home() / (".authinfo" if win else "_authinfo")
        if alt.exists():
            path = alt
        else:
            return [
                _warn(
                    "authinfo",
                    f"No {path} found.",
                    "Create it with a line like: "
                    "`oda user YOUR_EMAIL password YOUR_PASSWORD`, then "
                    "chmod 600 it.",
                )
            ]

    if not win:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            out.append(
                _fail(
                    "authinfo_permissions",
                    f"{path} is readable by group or others "
                    f"(mode {oct(mode & 0o777)}). SASPy refuses to use it and "
                    f"your SAS password is exposed to other local accounts.",
                    f"chmod 600 {path}",
                )
            )
        else:
            out.append(_ok("authinfo_permissions", f"{path} mode {oct(mode & 0o777)}"))

    authkey = cfg.get("authkey")
    if authkey:
        try:
            text = path.read_text(errors="replace")
        except OSError as exc:
            out.append(_warn("authinfo_entry", f"Could not read {path}: {exc}"))
            return out

        keys = [
            ln.split()[0]
            for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        if authkey in keys:
            # Note: {SAS00x}-encoded passwords were previously flagged here as
            # unusable. That was wrong -- verified against SAS ODA, which
            # authenticates an encoded password fine. No check.
            out.append(_ok("authinfo_entry", f"Found entry {authkey!r}."))
        else:
            out.append(
                _fail(
                    "authinfo_entry",
                    f"Config uses authkey {authkey!r} but {path} has no such "
                    f"entry (found: {', '.join(keys) or 'none'}).",
                    f"Add a line starting with `{authkey} user ... password ...`.",
                )
            )
    return out


def check_iom_hosts(cfg: dict[str, Any], probe: bool = True) -> list[Check]:
    """Sanity-check ODA hostnames and, optionally, reachability."""
    out: list[Check] = []
    hosts = cfg.get("iomhost")
    if not hosts:
        return out
    if isinstance(hosts, str):
        hosts = [hosts]
    port = int(cfg.get("iomport", 8591))

    for host in hosts:
        if "oda.sas.com" in host.lower() and not _ODA_HOST_RE.match(host):
            out.append(
                _fail(
                    "oda_hostname",
                    f"{host!r} does not match the SAS ODA host pattern "
                    f"(odaws01-usw2.oda.sas.com and similar). Looks like a typo.",
                    "Use the hostnames for your ODA home region exactly as "
                    "listed at https://welcome.oda.sas.com -- e.g. "
                    "odaws01-usw2.oda.sas.com.",
                )
            )
            continue

        if not probe:
            out.append(Check("iomhost", INFO, f"{host}:{port} (not probed)"))
            continue

        try:
            socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            out.append(
                _warn(
                    "iomhost_dns",
                    f"{host} does not resolve ({exc.strerror or exc}).",
                    "Check the hostname, your DNS, and whether this machine "
                    "needs a VPN to reach the SAS server.",
                )
            )
            continue

        try:
            with socket.create_connection((host, port), timeout=6):
                out.append(_ok("iomhost", f"{host}:{port} reachable"))
        except OSError as exc:
            out.append(
                _warn(
                    "iomhost_tcp",
                    f"{host} resolves but port {port} is not reachable ({exc}).",
                    "Check firewall/VPN, and that iomport matches your server "
                    "(ODA uses 8591).",
                )
            )
    return out


def check_encoding(cfg: dict[str, Any]) -> Check:
    enc = str(cfg.get("encoding", "")).lower().replace("-", "").replace("_", "")
    if not enc:
        return _warn(
            "encoding",
            "No 'encoding' set. SASPy will guess, and a mismatch shows up as "
            "mojibake in character columns.",
            "Set 'encoding' to match your SAS session -- 'utf-8' for ODA and "
            "most Linux servers, 'wlatin1' for many Windows SAS 9.4 installs.",
        )
    if enc in {"utf8", "latin1", "wlatin1", "latin9", "wlatin2", "shiftjis", "euckr"}:
        return _ok("encoding", f"encoding = {cfg['encoding']}")
    return _warn(
        "encoding", f"Unrecognized encoding {cfg['encoding']!r}.",
        "Common values: utf-8, wlatin1, latin1.",
    )


def check_saspath(cfg: dict[str, Any]) -> list[Check]:
    """For local STDIO connections, the SAS executable must actually be there."""
    path = cfg.get("saspath")
    if not path:
        return []
    if Path(path).exists():
        return [_ok("saspath", f"{path} exists")]
    return [
        _fail(
            "saspath", f"saspath does not exist: {path}",
            "Point 'saspath' at the SAS startup script, typically "
            "/opt/sasinside/SASHome/SASFoundation/9.4/sas or similar.",
        )
    ]


# --- orchestration -----------------------------------------------------------


def run_diagnostics(
    cfgname: str | None = None,
    probe_network: bool = True,
    cfgfile: str | None = None,
) -> dict[str, Any]:
    """Run every check and return a structured report."""
    checks: list[Check] = [check_python()]

    saspy_check, saspy_mod = check_saspy()
    checks.append(saspy_check)
    checks.append(check_pandas())

    config: dict[str, Any] = {}
    chosen: str | None = None

    if saspy_mod is not None:
        cfg_checks, cfg_path = find_config(saspy_mod, cfgfile)
        checks.extend(cfg_checks)
        if cfg_path is not None:
            parse_check, configs = load_config(cfg_path)
            checks.append(parse_check)
            if configs:
                chosen = cfgname or next(iter(configs))
                if chosen not in configs:
                    checks.append(
                        _fail(
                            "config_name",
                            f"Requested config {chosen!r} is not defined "
                            f"(available: {', '.join(configs)}).",
                            "Pass --config with one of the available names.",
                        )
                    )
                    chosen = next(iter(configs))
                config = configs[chosen]
                method = access_method(config)
                checks.append(
                    Check("access_method", INFO,
                          f"Config {chosen!r} uses the {method} access method.")
                )
                if method == "IOM":
                    checks.extend(check_java(config))
                    checks.extend(check_authinfo(config))
                    checks.extend(check_iom_hosts(config, probe=probe_network))
                elif method == "STDIO":
                    checks.extend(check_saspath(config))
                elif method in {"SSH", "HTTP"}:
                    checks.extend(check_authinfo(config))
                checks.append(check_encoding(config))

    counts = {
        "pass": sum(c.status == PASS for c in checks),
        "warn": sum(c.status == WARN for c in checks),
        "fail": sum(c.status == FAIL for c in checks),
    }
    if counts["fail"]:
        verdict = "Blocked: fix the failures below before connecting."
    elif counts["warn"]:
        verdict = "Probably usable, but some checks need attention."
    else:
        verdict = "All checks passed."

    return {
        "verdict": verdict,
        "config_name": chosen,
        "counts": counts,
        "checks": [c.to_dict() for c in checks],
    }


def format_report(report: dict[str, Any]) -> str:
    """Human-readable rendering for the CLI."""
    icons = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL", INFO: "INFO"}
    lines = ["", "sas-mcp doctor", "=" * 60]
    for c in report["checks"]:
        lines.append(f"[{icons.get(c['status'], '????')}] {c['name']}: {c['message']}")
        if c.get("fix") and c["status"] in {WARN, FAIL}:
            lines.append(f"         fix: {c['fix']}")
    counts = report["counts"]
    lines += [
        "=" * 60,
        f"{counts['pass']} passed, {counts['warn']} warnings, "
        f"{counts['fail']} failures",
        report["verdict"],
        "",
    ]
    return "\n".join(lines)
