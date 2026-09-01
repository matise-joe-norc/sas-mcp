"""Generate a working sascfg_personal.py, so nobody has to hand-write one.

SASPy ships a 225-line commented template and no way to produce a config from
it. For an audience of SAS developers -- many of whom are not Python
developers -- "copy this file and edit the right stanza" is the step where
adoption stops. This asks a few questions instead and writes a correct file to
the durable home location.

The pure parts (region lookup, Java detection, config rendering) are separated
from the prompting so they can be tested without a terminal.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_NAME = "sascfg_personal.py"


def default_config_dir() -> Path:
    """Where SASPy looks in the user's home, on every platform.

    SASPy calls ``expanduser("~/.config/saspy/")`` unconditionally, so Windows
    resolves this under %USERPROFILE% rather than AppData.
    """
    return Path.home() / ".config" / "saspy"


def default_config_path() -> Path:
    return default_config_dir() / CONFIG_NAME


def authinfo_path() -> Path:
    """SASPy reads _authinfo on Windows and .authinfo elsewhere."""
    name = "_authinfo" if sys.platform.startswith("win") else ".authinfo"
    return Path.home() / name


# --- SAS OnDemand for Academics regions --------------------------------------


@dataclass(frozen=True)
class ODARegion:
    key: str
    label: str
    hosts: tuple[str, ...]


ODA_REGIONS: tuple[ODARegion, ...] = (
    ODARegion("us1", "United States (Home Region 1)",
              ("odaws01-usw2.oda.sas.com", "odaws02-usw2.oda.sas.com")),
    ODARegion("us2", "United States (Home Region 2)",
              ("odaws01-usw2-2.oda.sas.com", "odaws02-usw2-2.oda.sas.com")),
    ODARegion("eu1", "Europe (Home Region 1)",
              ("odaws01-euw1.oda.sas.com", "odaws02-euw1.oda.sas.com")),
    ODARegion("ap1", "Asia Pacific (Home Region 1)",
              ("odaws01-apse1.oda.sas.com", "odaws02-apse1.oda.sas.com")),
    ODARegion("ap2", "Asia Pacific (Home Region 2)",
              ("odaws01-apse1-2.oda.sas.com", "odaws02-apse1-2.oda.sas.com")),
)

ODA_REGIONS_BY_KEY = {r.key: r for r in ODA_REGIONS}


# --- Java discovery ----------------------------------------------------------


def _java_runs(exe: str) -> bool:
    """A path existing is not the same as a JRE being installed.

    macOS ships /usr/bin/java as a stub that exits non-zero when no runtime is
    present, so the only reliable test is running it.
    """
    try:
        p = subprocess.run([exe, "-version"], capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "Unable to locate a Java Runtime" not in (
        p.stderr or ""
    )


def _java_exe_names() -> tuple[str, ...]:
    return ("java.exe",) if sys.platform.startswith("win") else ("java",)


def java_from_dir(path: Path) -> Path | None:
    """Accept a JAVA_HOME, a JDK folder, or the executable itself.

    People reasonably paste any of the three, so resolve all of them rather
    than rejecting two out of three.
    """
    if path.is_file():
        return path
    for name in _java_exe_names():
        for cand in (path / name, path / "bin" / name,
                     path / "jre" / "bin" / name):
            if cand.is_file():
                return cand
    return None


JDK_VENDOR_DIRS = ("Java", "Eclipse Adoptium", "Amazon Corretto", "Zulu",
                   "Microsoft", "AdoptOpenJDK", "OpenJDK", "JetBrains")


def _windows_candidates(
    sas_homes: list[Path] | None = None,
    program_files: list[Path] | None = None,
    exe: str = "java.exe",
) -> list[str]:
    """Java locations to try on Windows, best guess first.

    Arguments exist so the search can be tested against a fixture tree on any
    platform; production calls use the real locations.
    """
    out: list[str] = []

    if sas_homes is None:
        sas_homes = [Path(r"C:\Program Files\SASHome"),
                     Path(r"C:\Program Files (x86)\SASHome")]
        if env_home := os.environ.get("SASHOME"):
            sas_homes.insert(0, Path(env_home))

    # SAS 9.4 for Windows ships a private JRE. Anyone running local Windows
    # SAS already has this one, so it is the highest-yield guess and goes
    # first -- it turns "install Java" into no work at all.
    for home in sas_homes:
        for jre in sorted(
            home.glob(f"SASPrivateJavaRuntimeEnvironment/*/jre/bin/{exe}"),
            reverse=True,
        ):
            out.append(str(jre))

    if program_files is None:
        program_files = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
        ]

    for pf in program_files:
        for vendor in JDK_VENDOR_DIRS:
            base = pf / vendor
            if not base.is_dir():
                continue
            for jdk in sorted(base.glob("*"), reverse=True):
                for cand in (jdk / "bin" / exe, jdk / "jre" / "bin" / exe):
                    if cand.is_file():
                        out.append(str(cand))
    return out


def _darwin_candidates() -> list[str]:
    out: list[str] = []
    # Homebrew and manual installs often are not registered with
    # /usr/libexec/java_home even though they run fine.
    try:
        p = subprocess.run(["/usr/libexec/java_home"], capture_output=True,
                           text=True, timeout=20)
        if p.returncode == 0 and p.stdout.strip():
            out.append(str(Path(p.stdout.strip()) / "bin" / "java"))
    except (OSError, subprocess.TimeoutExpired):
        pass
    for base in sorted(Path("/Library/Java/JavaVirtualMachines").glob("*"),
                       reverse=True):
        out.append(str(base / "Contents" / "Home" / "bin" / "java"))
    out += ["/opt/homebrew/opt/openjdk/bin/java",
            "/usr/local/opt/openjdk/bin/java"]
    return out


def _linux_candidates() -> list[str]:
    out: list[str] = []
    for base in sorted(Path("/usr/lib/jvm").glob("*"), reverse=True):
        for cand in (base / "bin" / "java", base / "jre" / "bin" / "java"):
            if cand.is_file():
                out.append(str(cand))
    return out


def detect_java() -> str | None:
    """Find a Java executable that actually works, or None.

    Checks JAVA_HOME and the platform's usual install locations, not just
    PATH -- a JRE is very often installed without being on PATH, especially
    on Windows.
    """
    candidates: list[str] = []

    if home := os.environ.get("JAVA_HOME"):
        if found := java_from_dir(Path(home)):
            candidates.append(str(found))

    if sys.platform.startswith("win"):
        candidates += _windows_candidates()
    elif sys.platform == "darwin":
        candidates += _darwin_candidates()
    else:
        candidates += _linux_candidates()

    if which := shutil.which("java"):
        candidates.append(which)

    for exe in candidates:
        if Path(exe).exists() and _java_runs(exe):
            return exe
    return None


# --- config rendering --------------------------------------------------------

_HEADER = """\
# SASPy configuration generated by `sas-mcp init`.
#
# Docs: https://sassoftware.github.io/saspy/configuration.html
# Re-run `sas-mcp doctor` after editing to check this file.

SAS_config_names = [{names}]

"""


def _render_value(v: Any) -> str:
    """Render a config value as readable Python source.

    Windows paths are emitted as raw strings: repr() would double every
    backslash, which is valid but looks wrong to anyone opening the file to
    edit it -- and this file exists to be edited.
    """
    if isinstance(v, str):
        if "\\" in v and not v.endswith("\\") and "'" not in v:
            return f"r'{v}'"
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_render_value(i) for i in v) + "]"
    return repr(v)


def _render_dict(name: str, entries: dict[str, Any]) -> str:
    lines = [f"{name} = {{"]
    for k, v in entries.items():
        lines.append(f"    {k!r}: {_render_value(v)},")
    lines.append("}")
    return "\n".join(lines) + "\n"


def build_oda_config(region: str, name: str = "oda",
                     java: str | None = None) -> str:
    """Config for SAS OnDemand for Academics (IOM over the public internet)."""
    if region not in ODA_REGIONS_BY_KEY:
        raise ValueError(
            f"Unknown ODA region {region!r}. Choose one of: "
            f"{', '.join(ODA_REGIONS_BY_KEY)}"
        )
    entries: dict[str, Any] = {
        "java": java or "java",
        "iomhost": list(ODA_REGIONS_BY_KEY[region].hosts),
        "iomport": 8591,
        "authkey": name,
        "encoding": "utf-8",
    }
    return _HEADER.format(names=repr(name)) + _render_dict(name, entries)


def build_stdio_config(saspath: str, name: str = "local",
                       encoding: str = "utf-8") -> str:
    """Config for SAS installed locally on Linux/UNIX (no Java needed)."""
    return _HEADER.format(names=repr(name)) + _render_dict(
        name, {"saspath": saspath, "encoding": encoding}
    )


def build_ssh_config(host: str, saspath: str, user: str | None = None,
                     name: str = "remote", encoding: str = "utf-8") -> str:
    """Config for SAS on a remote UNIX host reached over SSH.

    SSH must be key-based: SASPy cannot answer a password prompt.
    """
    entries: dict[str, Any] = {
        "ssh": "ssh",
        "host": host,
        "saspath": saspath,
        "encoding": encoding,
    }
    if user:
        entries["luser"] = user
    return _HEADER.format(names=repr(name)) + _render_dict(name, entries)


def build_iom_config(host: str, port: int = 8591, name: str = "iomserver",
                     java: str | None = None, encoding: str = "utf-8") -> str:
    """Config for a SAS Workspace Server on an intranet, over IOM."""
    entries: dict[str, Any] = {
        "java": java or "java",
        "iomhost": host,
        "iomport": int(port),
        "authkey": name,
        "encoding": encoding,
    }
    return _HEADER.format(names=repr(name)) + _render_dict(name, entries)


def build_winlocal_config(name: str = "winlocal", java: str | None = None,
                          encoding: str = "windows-1252") -> str:
    """Config for SAS installed locally on Windows, over IOM.

    Local mode is selected by the *absence* of 'iomhost' -- SASPy starts the
    local SAS itself, so no host, port, or classpath is involved. Matches the
    ``winlocal`` stanza in SASPy's own shipped template.
    """
    return _HEADER.format(names=repr(name)) + _render_dict(
        name, {"java": java or "java", "encoding": encoding}
    )


def build_wincom_config(name: str = "wincom",
                        encoding: str = "windows-1252") -> str:
    """Config for local Windows SAS over the COM interface.

    COM needs no Java at all, which removes the single most common setup
    failure on Windows. It requires pywin32 and works only on Windows.
    """
    return _HEADER.format(names=repr(name)) + _render_dict(
        name, {"provider": "sas.iomprovider", "encoding": encoding}
    )


# --- writing files -----------------------------------------------------------


class ConfigExists(FileExistsError):
    """Raised rather than silently overwriting an existing config."""


_NAMES_RE = re.compile(
    r"^(?P<indent>[ \t]*)SAS_config_names\s*=\s*\[(?P<body>[^\]]*)\]",
    re.MULTILINE,
)


def parse_config_names(text: str) -> list[str] | None:
    """Read SAS_config_names out of a config file.

    Returns None when the declaration cannot be found, so callers can fall
    back rather than corrupting a file they do not understand.
    """
    m = _NAMES_RE.search(text)
    if not m:
        return None
    names = []
    for raw in m.group("body").split(","):
        raw = raw.strip().strip("'\"")
        if raw:
            names.append(raw)
    return names


def merge_config(existing: str, generated: str) -> str:
    """Add the generated configuration to an existing file, keeping both.

    The new name is added to SAS_config_names and its dict appended; nothing
    already in the file is modified.
    """
    new_names = parse_config_names(generated) or []
    if not new_names:  # pragma: no cover - we generate this ourselves
        raise ConfigExists("Could not read the generated configuration.")
    new_name = new_names[0]

    current = parse_config_names(existing)
    if current is None:
        raise ConfigExists(
            "Could not find a SAS_config_names list in the existing file, so "
            "it cannot be merged automatically. Choose replace, or edit the "
            "file by hand."
        )
    if new_name in current:
        raise ConfigExists(
            f"The existing file already defines a configuration named "
            f"{new_name!r}. Choose a different name, or replace the file."
        )

    # Everything after the SAS_config_names line is the generated dict.
    m = _NAMES_RE.search(generated)
    block = generated[m.end():].strip()

    merged_names = current + [new_name]
    rendered = ", ".join(repr(n) for n in merged_names)
    updated = _NAMES_RE.sub(
        lambda mm: f"{mm.group('indent')}SAS_config_names = [{rendered}]",
        existing,
        count=1,
    )
    return updated.rstrip() + "\n\n" + block + "\n"


def write_config(text: str, path: Path | None = None,
                 force: bool = False) -> Path:
    """Write the config, refusing to clobber an existing one unless forced."""
    target = Path(path) if path else default_config_path()
    target = target.expanduser()
    if target.exists() and not force:
        raise ConfigExists(
            f"{target} already exists. Pass --force to overwrite it, or edit "
            f"it directly."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _restrict(target)
    return target


def _restrict(path: Path) -> None:
    """Owner-only permissions. A no-op on Windows, which ignores POSIX bits."""
    if not sys.platform.startswith("win"):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def authinfo_has_key(key: str, path: Path | None = None) -> bool:
    p = (path or authinfo_path()).expanduser()
    if not p.is_file():
        return False
    for line in p.read_text(errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.split()[0] == key:
            return True
    return False


def write_authinfo_entry(key: str, user: str, password: str,
                         path: Path | None = None,
                         force: bool = False) -> Path:
    """Append a credential line, and lock the file down.

    The password is written to disk in the clear because that is what SASPy
    reads; file permissions are the only protection, so they are enforced here
    rather than left to the user.
    """
    p = (path or authinfo_path()).expanduser()
    if authinfo_has_key(key, p) and not force:
        raise ConfigExists(
            f"{p} already has an entry named {key!r}. Edit it directly, or "
            f"re-run with --force."
        )

    existing = ""
    if p.is_file():
        existing = p.read_text(errors="replace")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        if force:
            kept = [
                ln for ln in existing.splitlines()
                if not (ln.strip() and not ln.lstrip().startswith("#")
                        and ln.split()[0] == key)
            ]
            existing = "\n".join(kept) + ("\n" if kept else "")

    p.write_text(f"{existing}{key} user {user} password {password}\n")
    _restrict(p)
    return p


# --- interactive flow --------------------------------------------------------

DEPLOYMENTS = (
    ("oda", "SAS OnDemand for Academics (free, cloud)"),
    ("unix", "SAS installed locally on this Linux/UNIX machine"),
    ("wincom", "SAS installed locally on this Windows machine (COM, no Java)"),
    ("winlocal", "SAS installed locally on this Windows machine (IOM, needs Java)"),
    ("iom", "SAS server on my network (IOM / Workspace Server)"),
    ("ssh", "SAS on a remote UNIX host over SSH"),
)


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default


def _choose(prompt: str, options: list[tuple[str, str]]) -> str:
    print(f"\n{prompt}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    while True:
        raw = input(f"Choice [1-{len(options)}]: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  Please enter a number from the list.")


def _confirm(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    raw = input(f"{prompt} [{hint}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


_INSTALL_HINT = {
    "darwin": "brew install --cask temurin",
    "win32": "download Temurin from https://adoptium.net",
}.get(sys.platform, "install your distribution's OpenJDK package")


def prompt_for_java(ask=input) -> str | None:
    """Ask for a Java path and verify it before accepting it.

    Accepts the executable, a JAVA_HOME, or a JDK folder, and tolerates the
    surrounding quotes Windows' "Copy as path" adds.
    """
    print(
        "\n  No working Java runtime was found automatically.\n"
        "  If Java is installed, enter its path now (the java executable, or\n"
        "  the JDK/JAVA_HOME folder). On Windows, SAS ships one at:\n"
        r"    C:\Program Files\SASHome\SASPrivateJavaRuntimeEnvironment\9.4\jre\bin\java.exe"
    )
    for _ in range(3):
        raw = ask("  Java path (blank to skip): ").strip().strip('"').strip("'")
        if not raw:
            return None
        found = java_from_dir(Path(raw).expanduser())
        if found is None:
            print(f"    Not found: {raw}")
            continue
        if not _java_runs(str(found)):
            print(f"    {found} exists but did not run as a Java runtime.")
            continue
        print(f"    Verified: {found}")
        return str(found)
    return None


def resolve_java_arg(supplied: str | None) -> str | None:
    """Resolve an explicitly supplied --java value, else autodetect.

    A path the user names is verified rather than trusted: a wrong path
    written into the config fails later with a far less obvious message.
    """
    if supplied:
        found = java_from_dir(Path(supplied.strip().strip('"').strip("'"))
                              .expanduser())
        if found is None:
            # Quoted plainly, not repr'd: a Windows path shown with doubled
            # backslashes reads like the path itself is wrong.
            raise ConfigExists(  # surfaced as a CLI error
                f'No java executable found at "{supplied}". Give the path to '
                f"java{'.exe' if sys.platform.startswith('win') else ''}, or "
                f"to a JAVA_HOME/JDK folder."
            )
        if not _java_runs(str(found)):
            raise ConfigExists(
                f"{found} exists but did not run as a Java runtime."
            )
        return str(found)
    return detect_java()


def unverified_java_help(config_path: Path) -> str:
    """Exact file and line to edit when Java could not be verified."""
    if sys.platform.startswith("win"):
        example = (
            r"    'java': r'C:\Program Files\SASHome"
            r"\SASPrivateJavaRuntimeEnvironment\9.4\jre\bin\java.exe',"
        )
        exe = "java.exe"
    else:
        example = "    'java': '/usr/lib/jvm/temurin-17/bin/java',"
        exe = "java"
    return (
        f"\n  Java was not verified, so the config says just 'java'.\n"
        f"  If SAS cannot start, edit this file:\n"
        f"    {config_path}\n"
        f"  and set the 'java' entry to the full path of {exe}:\n"
        f"{example}\n"
        f"  Note the leading r, which keeps Windows backslashes literal.\n"
        f"  Then re-run `sas-mcp doctor`."
    )


def _java_or_warn(interactive: bool = True) -> str | None:
    java = detect_java()
    if java:
        print(f"  Found a working Java runtime: {java}")
        return java
    if interactive:
        if java := prompt_for_java():
            return java
    print(
        f"  WARNING: continuing without a verified Java runtime. The config\n"
        f"  will say 'java', which works only if a JRE is on your PATH.\n"
        f"  To fix later, {_INSTALL_HINT}, then set the 'java' entry in the\n"
        f"  config file this command writes (the path is printed below)."
    )
    return None


def resolve_existing_config(target: Path, text: str, new_name: str) -> str:
    """Ask what to do about a config file that already exists.

    An existing config is a completely normal situation -- a second SAS
    deployment, or a re-run of init -- so it is a choice, not an error.
    Returns "appended", "replaced", "kept", or "cancelled".
    """
    existing = target.read_text(errors="replace")
    current = parse_config_names(existing)

    print(f"\n  A SASPy configuration already exists at:\n    {target}")
    if current:
        print(f"  It defines: {', '.join(current)}")

    options = [
        ("append", f"Add {new_name!r} to it, keeping what is already there"),
        ("replace", "Replace the whole file with the new configuration"),
        ("keep", "Leave the file completely unchanged"),
    ]
    choice = _choose("What would you like to do?", options)

    if choice == "keep":
        return "kept"

    if choice == "replace":
        backup = target.with_suffix(target.suffix + ".bak")
        backup.write_text(existing)
        _restrict(backup)
        print(f"  Previous version saved to {backup}")
        write_config(text, path=target, force=True)
        return "replaced"

    # append
    try:
        merged = merge_config(existing, text)
    except ConfigExists as exc:
        print(f"\n  Cannot merge: {exc}")
        if _confirm("  Replace the whole file instead?", default=False):
            backup = target.with_suffix(target.suffix + ".bak")
            backup.write_text(existing)
            _restrict(backup)
            print(f"  Previous version saved to {backup}")
            write_config(text, path=target, force=True)
            return "replaced"
        return "cancelled"

    backup = target.with_suffix(target.suffix + ".bak")
    backup.write_text(existing)
    _restrict(backup)
    target.write_text(merged)
    _restrict(target)
    print(f"  Added {new_name!r}; previous version saved to {backup}")
    return "appended"


def interactive_init(force: bool = False,
                     path: Path | None = None) -> dict[str, Any]:
    """Ask what is needed, write the config, and report what was written."""
    print("\nsas-mcp init -- create a SASPy configuration\n" + "=" * 46)

    kind = _choose("Where does your SAS run?", list(DEPLOYMENTS))
    name = "oda" if kind == "oda" else _ask(
        "Name for this configuration", "sasconfig"
    )
    result: dict[str, Any] = {"deployment": kind, "config_name": name}
    java: str | None = None
    needs_java = kind in {"oda", "iom", "winlocal"}

    if kind == "oda":
        region = _choose(
            "Which ODA home region? (shown at welcome.oda.sas.com)",
            [(r.key, r.label) for r in ODA_REGIONS],
        )
        java = _java_or_warn()
        text = build_oda_config(region, name=name, java=java)
        result["region"] = region

    elif kind == "unix":
        saspath = _ask("Full path to the SAS startup script",
                       "/opt/sasinside/SASHome/SASFoundation/9.4/sas")
        text = build_stdio_config(saspath, name=name)
        result["saspath"] = saspath

    elif kind == "wincom":
        print(
            "  COM needs no Java, but does need pywin32:  pip install pywin32"
        )
        text = build_wincom_config(name=name)

    elif kind == "winlocal":
        java = _java_or_warn()
        text = build_winlocal_config(name=name, java=java)

    elif kind == "iom":
        host = _ask("SAS server hostname")
        port = _ask("IOM port", "8591")
        java = _java_or_warn()
        text = build_iom_config(host, int(port), name=name, java=java)
        result["host"] = host

    else:  # ssh
        host = _ask("Remote hostname")
        saspath = _ask("Full path to the SAS startup script on that host",
                       "/opt/sasinside/SASHome/SASFoundation/9.4/sas")
        user = _ask("SSH username (blank to use your local username)", "")
        print(
            "  Note: SSH must be key-based and passwordless -- SASPy cannot\n"
            "  answer a password prompt."
        )
        text = build_ssh_config(host, saspath, user or None, name=name)
        result["host"] = host

    target = (Path(path) if path else default_config_path()).expanduser()
    action = "written"

    if target.exists() and not force:
        action = resolve_existing_config(target, text, name)
        result["existing_config_action"] = action
        if action == "cancelled":
            print("\nLeaving the configuration alone.")
            return result
    else:
        write_config(text, path=target, force=True)

    result["config_path"] = str(target)
    if action == "kept":
        print(f"\nKept {target} unchanged.")
    else:
        print(f"\nWrote {target}")

    if needs_java and java is None:
        print(unverified_java_help(target))

    # Credentials, for the connection types that authenticate.
    if kind in {"oda", "iom"}:
        if authinfo_has_key(name) and not force:
            print(f"{authinfo_path()} already has an entry named {name!r}; "
                  f"leaving it alone.")
        elif _confirm(f"\nSave your SAS credentials to {authinfo_path()} now?"):
            import getpass

            user = _ask("SAS user name (for ODA, your SAS Profile email)")
            password = getpass.getpass("SAS password (not echoed): ")
            if password:
                p = write_authinfo_entry(name, user, password, force=force)
                print(f"Wrote credentials to {p} (permissions set to owner-only)")
                result["authinfo_path"] = str(p)
            else:
                print("No password entered; skipping.")

    print("\nNext: run `sas-mcp doctor` to verify the setup.\n")
    return result
