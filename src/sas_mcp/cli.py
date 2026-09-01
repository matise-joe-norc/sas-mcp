"""Command line entry point: `sas-mcp serve` and `sas-mcp doctor`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, init_config
from .doctor import format_report, run_diagnostics, run_full_check
from .guards import Policy


def _policy_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config",
        dest="cfgname",
        default=None,
        metavar="NAME",
        help="SASPy configuration name from sascfg_personal.py. When set, the "
             "server is pinned to it and the agent cannot switch away; pass "
             "--allow-config-switch to permit switching. When unset, the agent "
             "selects a configuration (automatically if only one exists).",
    )
    p.add_argument(
        "--log-dir",
        default=None,
        metavar="PATH",
        help="Directory for saved SAS logs (default: a temporary directory). "
             "Each submission is written there with its findings and full log, "
             "and the path is returned with the result.",
    )
    p.add_argument(
        "--file-dir",
        default=None,
        metavar="PATH",
        help="Directory used to transfer files to and from the SAS server "
             "(default: a temporary directory). Downloads land here, and "
             "uploads may only read from here.",
    )
    p.add_argument(
        "--allow-config-switch",
        action="store_true",
        help="With --config, treat it as a starting point rather than a "
             "restriction, letting the agent switch configurations.",
    )
    p.add_argument(
        "--config-file",
        dest="cfgfile",
        default=None,
        metavar="PATH",
        help="Full path to sascfg_personal.py. Recommended: SASPy otherwise "
             "searches its own package directory and the working directory "
             "before ~/.config/saspy, so the wrong file can win silently.",
    )
    p.add_argument(
        "--writable-libs",
        default=None,
        metavar="LIBS",
        help="Comma-separated librefs the agent may write to. WORK is always "
             "included. Default: WORK only.",
    )
    p.add_argument(
        "--allow-destructive",
        action="store_true",
        help="Permit DROP TABLE, PROC DATASETS KILL/DELETE, and similar. Off by "
             "default.",
    )
    p.add_argument(
        "--allow-os-escape",
        action="store_true",
        help="Permit X, %%SYSEXEC, FILENAME PIPE, and other operating-system "
             "escapes. Off by default.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sas-mcp",
        description="MCP server for writing, running, and validating SAS code "
                    "via SASPy.",
    )
    parser.add_argument("--version", action="version", version=f"sas-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the MCP server on stdio (default).")
    _policy_args(serve)

    ini = sub.add_parser(
        "init",
        help="Create a SASPy configuration file by answering a few questions.",
    )
    ini.add_argument(
        "--path", default=None, metavar="PATH",
        help="Where to write the config "
             "(default: ~/.config/saspy/sascfg_personal.py).",
    )
    ini.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing config or credential entry.",
    )
    ini.add_argument(
        "--deployment", default=None,
        choices=[k for k, _ in init_config.DEPLOYMENTS],
        help="Skip the first question and select the deployment type.",
    )
    ini.add_argument(
        "--region", default=None,
        choices=list(init_config.ODA_REGIONS_BY_KEY),
        help="ODA home region, for --deployment oda.",
    )
    ini.add_argument("--name", default=None, metavar="NAME",
                     help="Configuration name (default: oda, or sasconfig).")
    ini.add_argument(
        "--java", default=None, metavar="PATH",
        help="Path to the java executable, or a JAVA_HOME/JDK folder. "
             "Skips autodetection. Accepts a Windows path with backslashes; "
             "it is written to the config as a raw string.",
    )

    doc = sub.add_parser(
        "doctor",
        help="Diagnose SASPy configuration without connecting to SAS.",
    )
    doc.add_argument("--config", dest="cfgname", default=None, metavar="NAME")
    doc.add_argument("--config-file", dest="cfgfile", default=None, metavar="PATH")
    doc.add_argument("--json", action="store_true", help="Emit JSON.")
    doc.add_argument(
        "--no-network",
        action="store_true",
        help="Skip DNS and TCP reachability checks.",
    )
    doc.add_argument(
        "--connect",
        action="store_true",
        help="Also start a real SAS session and verify it works "
             "(same as `sas-mcp check`). Uses a SAS session, which counts "
             "against concurrency limits on ODA and licensed servers.",
    )

    chk = sub.add_parser(
        "check",
        help="Run the configuration checks, then connect to SAS and verify "
             "the connection actually works.",
    )
    chk.add_argument("--config", dest="cfgname", default=None, metavar="NAME")
    chk.add_argument("--config-file", dest="cfgfile", default=None, metavar="PATH")
    chk.add_argument("--json", action="store_true", help="Emit JSON.")
    chk.add_argument(
        "--no-network",
        action="store_true",
        help="Skip DNS and TCP reachability checks.",
    )

    args = parser.parse_args(argv)
    command = args.command or "serve"

    if command == "init":
        path = Path(args.path) if args.path else None
        try:
            if args.deployment == "oda" and args.region:
                # Fully non-interactive: scriptable for team setup.
                name = args.name or "oda"
                java = init_config.resolve_java_arg(args.java)
                text = init_config.build_oda_config(
                    args.region, name=name, java=java
                )
                target = init_config.write_config(text, path, force=args.force)
                print(f"Wrote {target}")
                if java is None:
                    print(init_config.unverified_java_help(target))
                print("Add your credentials to "
                      f"{init_config.authinfo_path()} as:\n"
                      f"  {name} user YOUR_USER password YOUR_PASSWORD")
                print("then run `sas-mcp doctor`.")
            else:
                init_config.interactive_init(force=args.force, path=path)
        except init_config.ConfigExists as exc:
            print(f"error: {exc}", file=sys.stderr)
            if "already exists" in str(exc):
                print(
                    "Run `sas-mcp init` without --deployment to choose "
                    "interactively whether to append to it, replace it, or "
                    "leave it alone. Or pass --force to overwrite.",
                    file=sys.stderr,
                )
            return 1
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.", file=sys.stderr)
            return 130
        return 0

    if command in {"doctor", "check"}:
        # `check` is the primary spelling; `doctor --connect` is the alias.
        connect = command == "check" or getattr(args, "connect", False)
        runner = run_full_check if connect else run_diagnostics
        report = runner(
            cfgname=args.cfgname,
            probe_network=not args.no_network,
            cfgfile=args.cfgfile,
        )
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(format_report(report))
        return 1 if report["counts"]["fail"] else 0

    # serve
    if not hasattr(args, "writable_libs"):  # bare `sas-mcp`
        args = parser.parse_args(["serve"])

    policy = Policy.from_spec(
        writable_libs=args.writable_libs,
        allow_os_escape=args.allow_os_escape,
        allow_destructive=args.allow_destructive,
    )

    lock_config = bool(args.cfgname) and not args.allow_config_switch

    # Stdout is the MCP transport; anything printed there corrupts the protocol.
    cfg_note = ""
    if args.cfgname:
        cfg_note = f", config: {args.cfgname}" + ("" if lock_config else " (switchable)")
    print(
        f"sas-mcp {__version__} starting (writable: "
        f"{', '.join(sorted(policy.writable_libs))}{cfg_note})",
        file=sys.stderr,
    )

    from .server import build_server

    build_server(
        cfgname=args.cfgname, policy=policy, cfgfile=args.cfgfile,
        lock_config=lock_config, log_dir=args.log_dir,
        file_dir=args.file_dir,
    ).run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
