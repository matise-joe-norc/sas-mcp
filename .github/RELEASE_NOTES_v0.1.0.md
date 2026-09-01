First release of **sas-mcp** — an [MCP](https://modelcontextprotocol.io) server that lets coding agents (Claude Code, Claude Desktop, GitHub Copilot, Cursor) write, run, and validate **SAS 9.4** code through [SASPy](https://sassoftware.github.io/saspy/).

It runs locally as a stdio subprocess. Nothing is hosted, and your code and data never leave your environment — SASPy connects to whatever SAS you already have.

## Why this exists

Wrapping `SASPy.submit()` is about thirty lines. The value is in the layers around it:

**Log triage.** SAS routinely *succeeds while being wrong.* A misspelled variable, a many-to-many merge, a silent character-to-numeric conversion — each returns zero errors and a wrong answer. `run_sas` returns `status: "suspicious"` for exactly that class, so an agent can't mistake it for success. 19 diagnostic rules, each with an explanation of what the NOTE actually means.

**Schema discovery.** Hallucinated column names are the most common way an LLM writes broken SAS. `describe_dataset` removes the guessing.

**Guardrails.** An agent improvising `proc datasets lib=prod kill;` against a production libref is a career event. Writes are restricted to `WORK` by default; OS escapes and destructive DDL are blocked unless explicitly enabled. Blocked code never reaches the SAS session.

**Validation.** `PROC COMPARE`'s `&SYSINFO` bitmask decoded into a structured diff that distinguishes a real data difference from a metadata-only one — plus an assertion macro library, rather than a niche SAS test framework.

## Install

```bash
pip install sas-mcp
sas-mcp init      # generates your SASPy configuration
sas-mcp doctor    # checks the setup without connecting
sas-mcp check     # connects and verifies it actually works
```

Then point your agent at it:

```bash
claude mcp add sas -- sas-mcp serve
```

## Tools

`run_sas` · `run_sas_tests` · `compare_datasets` · `describe_dataset` · `list_datasets` · `list_libraries` · `sample_rows` · `session_status` · `get_last_log` · `reset_session` · `sas_doctor`

## Setup, which is where most SASPy time goes

`sas-mcp init` asks where your SAS runs and writes a correct config — including finding the private JRE that SAS 9.4 for Windows ships, so most Windows users install no Java at all.

`sas-mcp doctor` diagnoses without connecting, so it still works when the connection is what's broken: config discovery and shadowing, Java, `~/.authinfo` permissions and format, the SAS encryption jars, ODA hostnames, reachability, encoding. Every failure comes with its fix.

`sas-mcp check` then connects and verifies the stack end to end. Its most important probe submits code with a deliberate error that *must* be flagged, and fails if it isn't — because SASPy's IOM sessions start with `NONOTES`, which makes the triage layer silently blind while everything still looks like a clean success.

## Verified

| Deployment | Access method | Verified |
| --- | --- | --- |
| SAS OnDemand for Academics | IOM | ✅ macOS + Windows |
| Intranet SAS server | IOM | ✅ Windows |
| Intranet SAS server | SSH | not yet |
| Local Windows install | COM / IOM | not yet |
| Local Linux/UNIX install | STDIO | not yet |

"Not yet" means generated and unit-tested, but not exercised against a running SAS. Run `sas-mcp check` and it will tell you whether your setup works — if one of these fails for you, that's a bug worth [reporting](https://github.com/matise-joe-norc/sas-mcp/issues).

270 tests, all SAS-free, running in CI on Linux, macOS, and Windows against Python 3.10, 3.12, and 3.14.

## Known limitations

- **The guardrails are a defense against model error, not a security boundary.** SAS can generate code at run time via `CALL EXECUTE`, `DOSUBL`, and macro expansion, so no static scan is complete. Use a SAS account whose own permissions match what you want to allow.
- Filesystem writes (`PROC EXPORT ... OUTFILE=`, `ODS` to a path) are not restricted — only SAS library writes.
- Each MCP client starts its own server process and therefore its own SAS session, which counts against concurrency limits on ODA and licensed servers.
- SAS ODA is free, but its terms are **academic and non-commercial use only**.

## License

MIT
