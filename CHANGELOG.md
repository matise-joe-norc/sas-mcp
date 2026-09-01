# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-09-01

Everything in this release came from running 0.1.0 against real SAS.

### Fixed

- **`DATA=` is no longer treated as a write.** `proc freq data=sashelp.prdsale`
  was rejected as an illegal write to a protected library. The rule matched
  `(?:base|data)=`, which is wrong even for PROC APPEND, where `BASE=` is the
  target and `DATA=` is only the source. The effect was severe: **every**
  ordinary PROC against a library outside `WORK` was blocked, including plain
  `SASHELP` reads. Anyone using 0.1.0 against real data hit this.
- **PROC DATASETS is no longer assumed to be a write.** Any `lib=` counted,
  so `proc datasets lib=sashelp; contents data=class; quit;` was blocked. It
  now fires only when the block contains a mutating statement, and the
  violation names the verb.
- **Multiple SAS configurations no longer hang the client.** With more than
  one configuration defined and none selected, SASPy prompts on stdin for a
  name — and on a stdio MCP server stdin is the JSON-RPC stream, so the
  prompt consumed protocol bytes and blocked forever. SASPy is now started
  with `prompt=False` so it can never read the transport.

### Added

- **`list_sas_configs` and `use_sas_config`.** The configuration choice is
  returned as data (`status: "config_required"`) rather than asked for on a
  stream nobody is reading. A single configuration is still used
  automatically.
- **`download_from_sas`, `upload_to_sas`, and `list_sas_files`.** SASPy
  transfers over the SAS connection, so a workbook written by `PROC EXPORT`
  on SAS ODA — which runs in AWS and cannot see your disk — can be fetched to
  your machine. Both directions are confined to one transfer directory: a
  download taking an arbitrary local path could overwrite anything you can
  write, and an upload taking one could send any readable file to a remote
  server.
- **`log_file` on every result.** Previously a failure advised checking the
  log without providing one. Each submission is now written to a file holding
  the status, the submitted code, every finding with its explanation and
  context, and the complete raw log; failures name that path in `next_step`.
- **Output lands in the working folder.** `./sas-mcp/files` and
  `./sas-mcp/logs`, so downloads and logs appear in the editor's file tree
  rather than a temporary directory. The directory is `.gitignore`d on
  creation, since SAS output can contain real data. `--file-dir` and
  `--log-dir` override; both fall back to a temporary directory when the
  working directory cannot be written.

### Changed

- **`--config NAME` now pins the server to that configuration** and refuses
  switches, on the reasoning that naming one in `mcp.json` means "use this",
  not "start here". `--allow-config-switch` restores the previous behaviour.

### Verified

- SAS OnDemand for Academics from macOS and Windows clients.
- An intranet SAS server over IOM from a Windows client.
- Still not exercised against a running SAS: local Windows SAS (COM and IOM),
  local UNIX SAS, and SSH. `sas-mcp check` will tell you whether they work on
  your setup.

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

[Unreleased]: https://github.com/matise-joe-norc/sas-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/matise-joe-norc/sas-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/matise-joe-norc/sas-mcp/releases/tag/v0.1.0
