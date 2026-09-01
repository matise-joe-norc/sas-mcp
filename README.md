# sas-mcp

An [MCP](https://modelcontextprotocol.io) server that lets coding agents —
Claude Code, Claude Desktop, GitHub Copilot, Cursor — write, run, and validate
**SAS 9.4** code through [SASPy](https://sassoftware.github.io/saspy/).

It runs locally as a stdio subprocess on your own machine. Nothing is hosted,
and your code and data never leave your environment: SASPy connects to
whatever SAS you already have.

## Why not just let the agent call `sas.submit()`?

Wrapping `submit()` is thirty lines. The reason this package exists is the
three layers around it:

- **Log triage.** SAS logs are enormous and the signal is buried. Worse, SAS
  routinely *succeeds* while being semantically wrong. A misspelled variable,
  a many-to-many merge, a silent character-to-numeric conversion — all of
  these return zero errors and a wrong answer. `run_sas` returns
  `status: "suspicious"` for exactly that class, so an agent can't mistake it
  for success.
- **Schema discovery.** Hallucinated column names are the most common way an
  LLM writes broken SAS. `describe_dataset` removes the guessing.
- **Guardrails.** An agent improvising `proc datasets lib=prod kill;` against
  a production libref is a career event. Writes are restricted to `WORK` by
  default.

## Install

```bash
pip install sas-mcp     # or: uv tool install sas-mcp
```

## First run: create your SAS configuration

SASPy needs a `sascfg_personal.py` describing where your SAS lives. If you
already run SASPy you have one and can skip ahead — this uses it as-is.

If you don't, **don't hand-write it.** Run:

```bash
sas-mcp init
```

It asks where your SAS runs, finds a working Java runtime for you, and writes
a correct config to the right place:

```
Where does your SAS run?
  1. SAS OnDemand for Academics (free, cloud)
  2. SAS installed locally on this Linux/UNIX machine
  3. SAS installed locally on this Windows machine
  4. SAS server on my network (IOM / Workspace Server)
  5. SAS on a remote UNIX host over SSH
Choice [1-5]: 1

Which ODA home region? (shown at welcome.oda.sas.com)
  1. United States (Home Region 1)
  ...
  Found a working Java runtime: /Library/Java/.../bin/java

Wrote /Users/you/.config/saspy/sascfg_personal.py
Save your SAS credentials to /Users/you/.authinfo now? [Y/n]:
```

For ODA and intranet IOM servers it also offers to write your credentials to
`~/.authinfo` (`_authinfo` on Windows). The password is prompted without echo
and the file is created owner-only — SASPy needs it in the clear, so
permissions are the only thing protecting it, and `init` sets them rather than
trusting you to remember.

Then verify:

```bash
sas-mcp doctor   # is the configuration sane? (never connects)
sas-mcp check    # does it actually work? (starts a real SAS session)
```

`doctor` is deliberately offline, so it still works when the connection is
exactly what's broken. `check` runs those same checks and then connects:

```
[PASS] connect: Connected (SAS 9.04.01M8P02222023, encoding utf-8)
[PASS] submit: DATA step ran; WORK._SASMCP_PROBE = 19 rows.
[PASS] log_notes: Log triage is working: flagged suspicious
       (missing_values_generated, uninitialized_variable).
[PASS] schema: Schema discovery works (5 columns, 19 rows).
[PASS] encoding: Non-ASCII round-trip is clean ('café').
```

The `log_notes` probe is the important one. It submits code with a misspelled
variable — code that *must* be flagged — and fails if it isn't. A session
with `NONOTES` set runs everything successfully while the triage layer sees
nothing, so results look like clean successes while being wrong. A passing
`submit` does not prove triage works; only this does.

The `encoding` probe round-trips a non-ASCII string, since an encoding
mismatch corrupts character data silently rather than raising.

`check` uses a real SAS session, which counts against concurrency limits on
ODA and licensed servers, and it cleans up the WORK tables it creates.
`sas-mcp doctor --connect` is the same thing.

It can also run unattended, for scripted or team setup:

```bash
sas-mcp init --deployment oda --region us1
```

Regions are `us1`, `us2`, `eu1`, `ap1`, `ap2`; your home region is shown at
[welcome.oda.sas.com](https://welcome.oda.sas.com). `init` never overwrites an
existing config or credential entry without `--force`.

### Where the config goes

`init` writes to your home directory, which works the same on every platform:

| Platform | Location |
| --- | --- |
| macOS / Linux | `~/.config/saspy/sascfg_personal.py` |
| Windows | `%USERPROFILE%\.config\saspy\sascfg_personal.py` |

SASPy calls `expanduser("~/.config/saspy/")` on all platforms, so Windows uses
that same `.config` folder under your user directory — not `AppData`.

Be aware that this is the **lowest-priority** location SASPy searches:

1. An explicit `cfgfile` path (what `--config-file` passes)
2. The **saspy package directory** inside site-packages
3. The working directory (`sys.path[0]`)
4. `~/.config/saspy/`

So a `sascfg_personal.py` left in site-packages or in your project folder will
silently win over your home copy. Two consequences worth knowing:

- A config inside site-packages **is deleted when you rebuild your virtualenv
  or reinstall saspy.** Keep it in your home directory instead.
- There is **no environment variable** for this. SASPy reads none when
  resolving a config, so `--config-file` is the only unambiguous way to pin it:

  ```bash
  sas-mcp serve --config-file ~/.config/saspy/sascfg_personal.py --config oda
  ```

`sas-mcp doctor` reports which file actually wins, warns when several exist,
and flags a config living somewhere a reinstall will destroy.

## Check your setup first

```bash
sas-mcp doctor
```

This is the fastest way past the usual configuration problems, and it runs
without connecting to SAS — so it still works when the connection is what's
broken. It checks the config file and access method, the Java runtime that IOM
requires, `~/.authinfo` presence and permissions, ODA hostname validity,
network reachability, and encoding. Every failure comes with the fix.

```
[FAIL] java: /usr/bin/java exists but no Java runtime is installed.
         fix: On macOS /usr/bin/java is only a stub. Install a real JRE, e.g.
              `brew install --cask temurin`, then set 'java' to the full path
              from `/usr/libexec/java_home`.
[FAIL] authinfo_permissions: ~/.authinfo is readable by group or others
       (mode 0o644). SASPy refuses to use it and your SAS password is exposed
       to other local accounts.
         fix: chmod 600 ~/.authinfo
```

When anything fails, the report also links
[SASPy's troubleshooting guide](https://sassoftware.github.io/saspy/troubleshooting.html),
which covers the IOM, Java, and encryption problems this tool can detect but
not fix for you.

### SAS encryption jars (needed for ODA)

**SASPy does not ship the SAS encryption jars** — `sas.rutil.jar`,
`sas.rutil.nls.jar`, and `sastpj.rutil.jar` are absent from a clean install,
though SASPy puts them on the IOM classpath regardless. They are a manual
download.

Whether you need them depends on the server:

- **SAS ODA always requires an encrypted connection.** Without these jars it
  fails even when everything else is correct — as a Java error that never
  mentions a missing file. `sas-mcp doctor` reports this as a **failure**.
- **An intranet IOM server** may not require encryption, so doctor reports it
  as **information** rather than treating a fresh install as broken.

Either way, doctor names the missing files, links the
[SAS download](https://sassoftware.github.io/saspy/configuration.html#attn-as-of-saspy-version-3-3-3-the-classpath-is-no-longer-required),
and prints the exact destination. They must go in SASPy's own
`saspy/java/iomclient/` directory — that path is hardcoded where SASPy builds
the IOM classpath, so no other location will be found. Doctor prints the
resolved absolute path for your install.

## Connect your agent

**Claude Code**

```bash
claude mcp add sas -- sas-mcp serve
```

**Claude Desktop** — in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sas": { "command": "sas-mcp", "args": ["serve"] }
  }
}
```

**VS Code / GitHub Copilot** — in `.vscode/mcp.json`:

```json
{
  "servers": {
    "sas": { "type": "stdio", "command": "sas-mcp", "args": ["serve"] }
  }
}
```

Add policy flags to `args` as needed, e.g.
`["serve", "--config", "oda", "--writable-libs", "STAGE"]`.

## Tools

| Tool | Purpose |
| --- | --- |
| `sas_doctor` | Diagnose configuration without connecting |
| `session_status` | Connection, SAS version, librefs, WORK contents, policy |
| `run_sas` | Submit code; returns triaged status, findings, row counts, output |
| `get_last_log` | Full raw log, when triage isn't enough |
| `reset_session` | Clear WORK |
| `list_libraries` | Assigned librefs with paths and writability |
| `list_datasets` | Tables in a library with row/column counts |
| `describe_dataset` | Columns with type, length, format, label |
| `sample_rows` | First N rows as records |
| `compare_datasets` | PROC COMPARE as a structured diff |
| `run_sas_tests` | Run code with assertion macros; report pass/fail |

### What `run_sas` returns

Not a log — a verdict:

```json
{
  "status": "suspicious",
  "summary": "Ran, but results may be wrong: 2 suspicious notes; created WORK.B=19 obs.",
  "suspicious_notes": [
    {
      "rule": "uninitialized_variable",
      "text": "NOTE: Variable weigth is uninitialized.",
      "explanation": "Variable was read before being assigned, so it evaluates to missing. Almost always a misspelled variable name.",
      "line_no": 5
    }
  ],
  "steps": [{"step": "DATA statement", "dataset": "WORK.B", "obs_out": 19}],
  "output": "..."
}
```

`status` is `ok`, `suspicious`, or `error`. **`suspicious` means the code ran
and the answer is probably wrong** — it is not a success.

## Validating code

Rather than teach an agent a niche SAS test framework, this builds on the tool
SAS developers already use — and which happens to be machine-readable.
`PROC COMPARE` sets `&SYSINFO` to a bitmask where each bit names a specific
*kind* of difference, so `compare_datasets` can distinguish "the values
disagree" from "only a format differs":

```json
{
  "identical": false,
  "data_differs": true,
  "metadata_only": false,
  "findings": [
    {"code": "base_obs", "meaning": "Base data set has observations not in comparison"},
    {"code": "value",    "meaning": "At least one value comparison was unequal"}
  ],
  "summary": "Data sets differ: ..."
}
```

That makes it a real assertion an agent can iterate against — the natural way
to verify that a rewritten step reproduces the original result.

`run_sas_tests` adds a small assertion macro library, loaded into the session
on first use:

```sas
%assert_exists(work.out);
%assert_rows(work.out, 19);
%assert_not_empty(work.out);
%assert_no_missing(work.out, age);
%assert_unique(work.out, id);
%assert_equal_datasets(work.expected, work.out);
%assert_condition(&n > 0, detail=n must be positive);
```

Each writes a marker to the log that comes back as structured pass/fail. A
failed assertion sets `status: "assertions_failed"` even when the log itself
is clean — a green log with a red assertion is not a pass.

## Safety

Writes are restricted to `WORK` by default. Also blocked unless you opt in:

- **OS escapes** — `X`, `%SYSEXEC`, `SYSTASK COMMAND`, `CALL SYSTEM`,
  `FILENAME PIPE`, `PROC PYTHON/LUA/GROOVY`
- **Destructive DDL** — `PROC DATASETS KILL`/`DELETE`, `DROP TABLE`,
  `PROC DELETE`, `FDELETE`
- **Libref rebinding** — re-pointing an allowlisted libref somewhere else

Reads are never restricted; an agent can `set prod.sales` freely.

```bash
sas-mcp serve --writable-libs STAGE,SCRATCH   # widen the write allowlist
sas-mcp serve --allow-destructive             # permit DROP/KILL/DELETE
sas-mcp serve --allow-os-escape               # permit X, PIPE, etc.
```

### Scope of the guarantee

**This is a defense against model error, not a security boundary.** SAS can
generate code at run time through `CALL EXECUTE`, `DOSUBL`, and macro
expansion, so no static scan can be complete, and a determined bypass is
always possible. It reliably stops the common accident. Do not rely on it as
your only control on a system where an agent could do real damage — use a SAS
account whose own permissions match what you want to allow.

Two known gaps, stated plainly:

- Filesystem writes (`PROC EXPORT ... OUTFILE=`, `ODS` to a path) are **not**
  restricted, only SAS library writes.
- Code assembled at run time from fragments that are individually innocuous
  will not be caught.

## Supported deployments

All four SAS 9.4 setups SASPy handles, selected by your `sascfg_personal.py`:

| Deployment | Access method | Needs |
| --- | --- | --- |
| Local Linux/UNIX install | STDIO | `saspath` |
| Local Windows install | IOM | Java |
| Intranet SAS server | IOM or SSH | Java, or SSH keys |
| SAS OnDemand for Academics | IOM | Java, `~/.authinfo` |

SAS ODA is free but its terms are **academic and non-commercial use only**.

Note that each MCP client starts its own server process and therefore its own
SAS session, which counts against concurrent-session limits on both ODA and
licensed servers.

## Development

```bash
uv pip install -e ".[dev]"
pytest
```

The log parser and guardrails are pure functions with no SAS dependency, and
the server tests run against a fake session — so the full suite runs anywhere.
CI runs them on Linux, macOS, and Windows against Python 3.10, 3.12, and 3.14.

### Testing on Windows

CI runs the suite on Windows, but that cannot reach a SAS installation. To
test against real Windows SAS, on the Windows machine:

```powershell
# No PyPI release needed -- install straight from the repo
pip install git+https://github.com/matise-joe-norc/sas-mcp

sas-mcp init      # choose the COM option first; it needs no Java
sas-mcp doctor
```

Local Windows SAS has two access methods, and **COM is worth trying first**:
it needs no Java at all, which removes the most common Windows setup failure.
It does need `pip install pywin32`. The IOM option is the fallback if COM
gives trouble.

Then exercise the server end to end:

```powershell
python -c "from sas_mcp.session import SASSessionManager as M; m=M(cfgname='wincom'); r=m.submit('data work.a; set sashelp.class; run;'); print(r.triage.status, r.triage.summary)"
```

Expect `ok  Ran: created WORK.A=19 obs.` A result of `ok` with **no steps
reported** means the log has no NOTEs — see the `options notes` note below.

The two things most likely to differ from the verified Linux/ODA path:

- **Encoding.** Windows SAS 9.4 typically runs `wlatin1`, not UTF-8. A
  mismatch shows up as mojibake in character columns rather than an error.
  `sas-mcp doctor` reports the configured value.
- **`NONOTES`.** SASPy's IOM sessions suppress the `NOTE:` lines that log
  triage depends on. The session manager sets `options notes source;` on
  connect; if a Windows session somehow overrides that, `run_sas` would
  return `ok` with empty `steps` and no `suspicious_notes`.

To confirm triage is really working rather than silently blind, run something
that *should* be flagged:

```powershell
python -c "from sas_mcp.session import SASSessionManager as M; m=M(cfgname='wincom'); r=m.submit('data work.b; set sashelp.class; bmi=weigth/height; run;'); print(r.triage.status, [n.rule for n in r.triage.suspicious_notes])"
```

Expect `suspicious ['uninitialized_variable', 'missing_values_generated']`.
If that returns `ok` with an empty list, triage is not seeing NOTEs and the
result is untrustworthy — report it as a bug.

### Releasing

The version lives in exactly one place: `__version__` in
[`src/sas_mcp/__init__.py`](src/sas_mcp/__init__.py). Packaging metadata reads
it from there, so the two can't drift.

1. Bump `__version__` and add a `CHANGELOG.md` entry.
2. Commit, and confirm CI is green on `main`.
3. Publish a GitHub Release tagged `vX.Y.Z`.

That triggers [`release.yml`](.github/workflows/release.yml), which builds,
runs `twine check`, installs the built wheel into a clean virtualenv to
confirm it actually runs, **verifies the tag matches `__version__`**, and only
then publishes. The tag check matters because PyPI will not let you re-upload
a filename — a mismatched tag is not recoverable.

`workflow_dispatch` runs the same build and verification without publishing,
if you want a dry run first.

#### One-time PyPI setup

Publishing uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
so there is no API token to store or rotate. Before the first release, add a
*pending publisher* at
[pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/):

| Field | Value |
| --- | --- |
| PyPI project name | `sas-mcp` |
| Owner | `matise-joe-norc` |
| Repository name | `sas-mcp` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Then create a `pypi` environment under the repository's
Settings → Environments. Adding a required reviewer there gives you a manual
approval gate before anything reaches PyPI.

## License

MIT
