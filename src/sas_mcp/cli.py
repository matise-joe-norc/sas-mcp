"""Command line entry point: `sas-mcp serve` and `sas-mcp doctor`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, init_config
from .doctor import format_report, run_diagnostics
from .guards import Policy


def _policy_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config",
        dest="cfgname",
        default=None,
        metavar="NAME",
        help="SASPy configuration name from sascfg_personal.py "
             "(default: SASPy's own default).",
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

    doc = sub.add_parser("doctor", help="Diagnose SASPy configuration and exit.")
    doc.add_argument("--config", dest="cfgname", default=None, metavar="NAME")
    doc.add_argument("--config-file", dest="cfgfile", default=None, metavar="PATH")
    doc.add_argument("--json", action="store_true", help="Emit JSON.")
    doc.add_argument(
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
                text = init_config.build_oda_config(
                    args.region, name=name, java=init_config.detect_java()
                )
                target = init_config.write_config(text, path, force=args.force)
                print(f"Wrote {target}")
                print("Add your credentials to "
                      f"{init_config.authinfo_path()} as:\n"
                      f"  {name} user YOUR_USER password YOUR_PASSWORD")
                print("then run `sas-mcp doctor`.")
            else:
                init_config.interactive_init(force=args.force, path=path)
        except init_config.ConfigExists as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.", file=sys.stderr)
            return 130
        return 0

    if command == "doctor":
        report = run_diagnostics(
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

    # Stdout is the MCP transport; anything printed there corrupts the protocol.
    print(
        f"sas-mcp {__version__} starting (writable: "
        f"{', '.join(sorted(policy.writable_libs))})",
        file=sys.stderr,
    )

    from .server import build_server

    build_server(
        cfgname=args.cfgname, policy=policy, cfgfile=args.cfgfile
    ).run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
