"""Lifecycle for the one SAS session a server process owns.

SAS sessions are stateful -- WORK, librefs, macro variables, and options all
persist between submits -- and that is a feature here: it lets an agent build
up intermediate results across turns the way an analyst would. The cost is
that state can go stale or get polluted, so the session is explicitly
resettable and always inspectable.

Connection configuration is deliberately *not* reinvented. SASPy's
``sascfg_personal.py`` already covers the four deployments this targets
(local Unix STDIO, local Windows IOM, remote intranet IOM/SSH, SAS ODA over
IOM), and SAS administrators already know it. We take a config name.

One hazard worth naming: SASPy prints status and error text to *stdout*
("Using SAS Config named: ...", socket failures), which on a stdio MCP server
is the JSON-RPC transport. This is safe only because the MCP SDK's stdio
transport claims fd 1 onto a private descriptor at startup and points fd 1 at
stderr, so library prints are diverted. Verified end to end; do not add a
manual stdout redirect on top of it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .guards import GuardResult, Policy, check
from .logparse import LogTriage, parse_log


class SASConnectionError(RuntimeError):
    """Raised when a SAS session cannot be established."""


class ConfigLocked(RuntimeError):
    """Raised when switching configurations is disallowed by the operator."""


@dataclass
class SubmitResult:
    """Everything a caller needs about one submit, minus the raw log."""

    triage: LogTriage
    output: str
    log: str
    syserr: int | None = None
    syserrortext: str = ""

    def to_dict(self, include_log: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.triage.status,
            "summary": self.triage.summary,
            "errors": [vars(e) for e in self.triage.errors],
            "warnings": [vars(w) for w in self.triage.warnings],
            "suspicious_notes": [vars(n) for n in self.triage.suspicious_notes],
            "steps": [vars(s) for s in self.triage.steps],
            "output": self.output,
            "log_line_count": self.triage.log_line_count,
        }
        if self.syserr is not None:
            d["syserr"] = self.syserr
        if self.syserrortext:
            d["syserrortext"] = self.syserrortext
        if include_log:
            d["log"] = self.log
        return d


class SASSessionManager:
    """Owns a single lazily-created SASPy session, guarded by a lock.

    SASPy session objects are not safe to drive from several threads at once,
    and the MCP server may dispatch tool calls concurrently, so every submit
    serializes on ``_lock``.
    """

    def __init__(
        self,
        cfgname: str | None = None,
        policy: Policy | None = None,
        cfgfile: str | None = None,
        lock_config: bool = False,
    ):
        self.cfgname = cfgname
        # When an operator pins a configuration in the MCP client's config,
        # they usually mean "use this one" -- not "start here". Locking makes
        # that binding actually hold against an agent that decides otherwise.
        self.lock_config = lock_config and bool(cfgname)
        # An explicit config path is the only unambiguous way to select a
        # config: SASPy otherwise searches its own package directory first,
        # then the working directory, and only then ~/.config/saspy, so a
        # stray sascfg_personal.py silently wins.
        self.cfgfile = cfgfile
        self.policy = policy or Policy.from_spec()
        self._sas: Any = None
        self._lock = threading.RLock()
        self._last_log = ""
        self._macros_loaded = False

    # --- configuration selection ---------------------------------------------

    def available_configs(self) -> list[dict[str, Any]]:
        """Configurations defined in sascfg_personal.py, without connecting."""
        from .doctor import list_available_configs

        return list_available_configs(self.cfgfile)

    def _resolve_cfgname(self) -> str | None:
        """Pick a config when none was given, or explain why we cannot.

        One config is unambiguous, so use it. Several is a real choice that
        only the caller can make -- and SASPy would otherwise block on stdin
        asking for it.
        """
        configs = self.available_configs()
        if len(configs) == 1:
            return configs[0]["name"]
        if len(configs) > 1:
            listing = "\n".join(
                f"  - {c['name']}  ({c['access_method']}"
                + (f" -> {c['target']}" if c["target"] else "")
                + ")"
                for c in configs
            )
            raise SASConnectionError(
                f"This SASPy installation defines {len(configs)} "
                f"configurations, so one has to be chosen before connecting:\n"
                f"{listing}\n\n"
                f"Select one with the use_sas_config tool, or start the server "
                f"with --config <name>."
            )
        return None  # let SASPy fall back to its own defaults

    def set_config(self, name: str) -> dict[str, Any]:
        """Switch the active configuration, ending any current session."""
        if self.lock_config:
            raise ConfigLocked(
                f"This server is pinned to the {self.cfgname!r} configuration "
                f"and cannot switch to {name!r}. The restriction comes from "
                f"the MCP server's own startup options, not from anything the "
                f"user said in this conversation, so it cannot be worked "
                f"around here."
            )
        configs = self.available_configs()
        names = [c["name"] for c in configs]
        if names and name not in names:
            raise SASConnectionError(
                f"No configuration named {name!r}. Available: "
                f"{', '.join(names)}."
            )
        with self._lock:
            was_connected = self._sas is not None
            if was_connected:
                self.disconnect()
            self.cfgname = name
            self._macros_loaded = False
        return {
            "config_name": name,
            "previous_session_ended": was_connected,
            "details": next((c for c in configs if c["name"] == name), None),
        }

    # --- lifecycle -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sas is not None

    def connect(self) -> Any:
        """Return the live session, creating it on first use."""
        with self._lock:
            if self._sas is not None:
                return self._sas
            try:
                import saspy
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise SASConnectionError(
                    "saspy is not installed. Install it with: pip install saspy"
                ) from exc

            # prompt=False is not optional here. SASPy prompts on stdin when
            # it needs a config name or credentials, and on a stdio MCP server
            # stdin IS the JSON-RPC stream -- prompting would consume protocol
            # bytes and hang the client forever. With prompting off, SASPy
            # raises instead, which we can turn into an actionable message.
            kwargs: dict[str, Any] = {"results": "TEXT", "prompt": False}
            cfgname = self.cfgname or self._resolve_cfgname()
            if cfgname:
                kwargs["cfgname"] = cfgname
            if self.cfgfile:
                kwargs["cfgfile"] = str(Path(self.cfgfile).expanduser())
            try:
                self._sas = saspy.SASsession(**kwargs)
            except Exception as exc:
                raise SASConnectionError(
                    f"Could not start a SAS session"
                    f"{f' with config {self.cfgname!r}' if self.cfgname else ''}: "
                    f"{exc}\n\nRun `sas-mcp doctor` to diagnose configuration, "
                    f"Java, and credential problems."
                ) from exc
            self._prime_session()
            return self._sas

    # Options the triage layer depends on. SASPy's IOM sessions start with
    # NONOTES, which suppresses every NOTE: line -- and the NOTEs are precisely
    # where "ran fine but the answer is wrong" lives. Without this, triage is
    # blind on IOM connections (SAS ODA, local Windows, most intranet servers).
    # SOURCE keeps the echoed submission in the log, which is what gives error
    # messages their surrounding context.
    PRIME_OPTIONS = "options notes source;"

    def _prime_session(self) -> None:
        """Force the log to carry what triage needs."""
        try:
            self._sas.submit(self.PRIME_OPTIONS, results="TEXT")
        except Exception:  # pragma: no cover - non-fatal
            pass

    def disconnect(self) -> None:
        with self._lock:
            if self._sas is not None:
                try:
                    self._sas.endsas()
                except Exception:  # pragma: no cover - best effort teardown
                    pass
                self._sas = None

    def reset(self) -> str:
        """Clear WORK and reset librefs/options without a full reconnect."""
        with self._lock:
            if self._sas is None:
                self.connect()
                return "Session started fresh; nothing to reset."
            self.submit_raw(
                f"proc datasets library=work kill nolist; quit; "
                f"{self.PRIME_OPTIONS}"
            )
            # KILL also removes WORK.SASMACR, so the assertion macros are gone.
            self._macros_loaded = False
            return "WORK cleared."

    # --- submitting ----------------------------------------------------------

    def submit_raw(self, code: str) -> tuple[str, str]:
        """Submit without policy checks. Internal use only.

        Returns ``(log, listing)``.
        """
        sas = self.connect()
        with self._lock:
            res = sas.submit(code, results="TEXT")
            self._last_log = res.get("LOG", "")
            return self._last_log, res.get("LST", "")

    def check_policy(self, code: str) -> None:
        """Raise ``PermissionError`` if policy forbids this code.

        Exposed separately so callers can reject before doing any setup work:
        a blocked request should produce no SAS traffic at all.
        """
        guard: GuardResult = check(code, self.policy)
        if not guard.allowed:
            raise PermissionError(guard.explain())

    def submit(self, code: str, max_events: int = 25) -> SubmitResult:
        """Policy-check, submit, and triage.

        Raises ``PermissionError`` carrying an explanation if policy blocks the
        code; the caller is expected to surface that text to the model.
        """
        self.check_policy(code)
        return self._submit_unchecked(code, max_events)

    def _submit_unchecked(self, code: str, max_events: int = 25) -> SubmitResult:
        log, listing = self.submit_raw(code)
        triage = parse_log(log, max_events=max_events)

        syserr: int | None = None
        syserrortext = ""
        try:
            sas = self.connect()
            syserr = int(sas.SYSERR())
            syserrortext = sas.SYSERRORTEXT() or ""
        except Exception:
            # SYSERR is a nice-to-have; the log is the authoritative signal.
            pass

        return SubmitResult(
            triage=triage,
            output=listing.strip(),
            log=log,
            syserr=syserr,
            syserrortext=syserrortext,
        )

    def ensure_macros(self) -> None:
        """Compile the assertion macro library into the session, once."""
        with self._lock:
            if self._macros_loaded:
                return
            from .validate import ASSERT_MACROS

            self.submit_raw(ASSERT_MACROS)
            self._macros_loaded = True

    # --- introspection -------------------------------------------------------

    def last_log(self) -> str:
        return self._last_log

    def status(self) -> dict[str, Any]:
        """Where the session stands: connection, librefs, WORK contents."""
        if self._sas is None:
            return {
                "connected": False,
                "cfgname": self.cfgname,
                "policy": self._policy_dict(),
            }
        sas = self._sas
        info: dict[str, Any] = {
            "connected": True,
            "cfgname": self.cfgname,
            "policy": self._policy_dict(),
        }
        try:
            info["sas_version"] = sas.sasver if hasattr(sas, "sasver") else None
            info["encoding"] = getattr(sas, "sascei", None) or getattr(
                sas, "encoding", None
            )
        except Exception:
            pass
        try:
            info["librefs"] = sorted(sas.assigned_librefs())
        except Exception:
            info["librefs"] = []
        try:
            info["work_tables"] = sorted(sas.list_tables("WORK", results="pandas")
                                         ["MEMNAME"].tolist())
        except Exception:
            info["work_tables"] = []
        return info

    def _policy_dict(self) -> dict[str, Any]:
        return {
            "writable_libs": sorted(self.policy.writable_libs),
            "allow_os_escape": self.policy.allow_os_escape,
            "allow_destructive": self.policy.allow_destructive,
            "config_locked": self.lock_config,
        }
