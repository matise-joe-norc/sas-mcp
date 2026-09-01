# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-08-31

First release.

### Added

- **`run_sas`** — submits SAS code and returns a triaged result rather than a
  log: status (`ok` / `suspicious` / `error`), extracted errors and warnings,
  per-step row counts, and the listing output. `suspicious` covers the case
  that matters most — code that runs cleanly while producing a wrong answer,
  such as an uninitialized variable, a many-to-many merge, or a silent
  character-to-numeric conversion. 19 diagnostic rules.
- **Schema discovery** — `list_libraries`, `list_datasets`, `describe_dataset`,
  and `sample_rows`, so an agent can read real column names instead of
  guessing them.
- **Guardrails** — writes restricted to `WORK` by default; OS escapes
  (`X`, `%SYSEXEC`, `FILENAME PIPE`, `CALL SYSTEM`, `PROC PYTHON`) and
  destructive DDL (`PROC DATASETS KILL`, `DROP TABLE`, `PROC DELETE`) blocked
  unless explicitly enabled, along with rebinding an allowlisted libref.
  Blocked code never reaches the SAS session.
- **`compare_datasets`** — decodes `PROC COMPARE`'s `&SYSINFO` bitmask into a
  structured diff that distinguishes a real data difference from a
  metadata-only one (label, format, length).
- **`run_sas_tests`** — runs code with an assertion macro library
  (`%assert_rows`, `%assert_unique`, `%assert_no_missing`,
  `%assert_equal_datasets`, and others) and reports each assertion's result.
  A failed assertion sets `status: "assertions_failed"` even when the log is
  clean.
- **`sas-mcp init`** — generates a SASPy configuration by asking where SAS
  runs, detecting a working Java runtime, and writing the file to
  `~/.config/saspy/`. Optionally stores credentials in `~/.authinfo` with
  owner-only permissions.
- **`sas-mcp doctor`** — diagnoses setup without connecting to SAS: config
  discovery and shadowing, Java runtime, `~/.authinfo` presence and
  permissions, ODA hostname validity, network reachability, and encoding.
  Every failure reports its fix.
- **`session_status`**, **`get_last_log`**, and **`reset_session`** for
  managing the stateful SAS session.
- `--config-file` to pin a SASPy configuration explicitly, avoiding SASPy's
  search-order ambiguity.

### Notes

- Verified end to end against live SAS 9.4M8 on SAS OnDemand for Academics,
  from both macOS and Windows clients. Not yet exercised against a running
  SAS: local Windows SAS (COM and IOM), local UNIX SAS, and intranet IOM or
  SSH servers. Those paths are generated and unit-tested, and `sas-mcp check`
  will tell you whether they work on your setup.
- SASPy's IOM sessions start with `NONOTES`, which suppresses the very NOTEs
  the triage layer depends on. The session sets `options notes source;` on
  connect; without it, triage is blind on IOM connections.
- The guardrails are a defense against model error, not a security boundary.
  SAS can generate code at run time, so no static scan is complete.

[Unreleased]: https://github.com/matise-joe-norc/sas-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/matise-joe-norc/sas-mcp/releases/tag/v0.1.0
