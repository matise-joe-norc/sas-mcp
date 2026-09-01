Everything in this release came from running 0.1.0 against real SAS.

```bash
pip install --upgrade sas-mcp
```

## Upgrade if you are on 0.1.0

0.1.0 rejected `proc freq data=sashelp.prdsale` as an illegal write to a
protected library. `DATA=` names a PROC's *input*, but the guard matched
`(?:base|data)=` — wrong even for PROC APPEND, where `BASE=` is the target and
`DATA=` is only the source.

The effect was much broader than one PROC: **every** ordinary PROC against a
library outside `WORK` was blocked, including plain `SASHELP` reads. If you
used 0.1.0 against real data, you hit this.

## Fixed

- **`DATA=` is a read.** Writes come from a `DATA` statement, `OUT=`, and
  PROC APPEND's `BASE=`. Reads are never restricted.
- **PROC DATASETS is no longer assumed to be a write.** Any `lib=` counted, so
  `proc datasets lib=sashelp; contents data=class; quit;` was blocked. It now
  fires only on a mutating statement, and names the verb that made it one.
- **Multiple SAS configurations no longer hang the client.** With several
  defined and none selected, SASPy prompts on stdin for a name — and on a
  stdio MCP server stdin *is* the JSON-RPC stream, so the prompt consumed
  protocol bytes and blocked forever. SASPy is now started with
  `prompt=False`, so it can never read the transport.

## Added

**Choosing between SAS environments.** The choice comes back as data rather
than as a prompt on a stream nobody is reading:

```
run_sas          -> status: "config_required", configs: [oda, wincom]
list_sas_configs -> oda    IOM  odaws01-usw2.oda.sas.com
                    wincom COM  local Windows SAS
use_sas_config   -> ok
```

Pin one in your client config with `--config oda`; the agent then cannot
switch away. `--allow-config-switch` makes it a default it may change. A
single configuration is still used automatically.

**Getting files out of SAS.** SASPy transfers over the SAS connection, so this
works when the two machines share no filesystem — a workbook written by
`PROC EXPORT` on SAS ODA, which runs in AWS, can be fetched to your machine:

```
run_sas:           proc export data=sashelp.class
                     outfile="~/report.xlsx" dbms=xlsx replace; run;
list_sas_files:    ~             -> report.xlsx
download_from_sas: ~/report.xlsx -> ./sas-mcp/files/report.xlsx
```

`upload_to_sas` goes the other way. Both directions are confined to one
transfer directory: a download taking an arbitrary local path could overwrite
anything you can write, and an upload taking one could send any readable file
to a remote server.

**Logs you can actually open.** Previously a failure advised checking the log
without giving you one. Every result now carries `log_file` — a path to the
status, the submitted code, each finding with its explanation and surrounding
context, and then the complete raw log.

**Output in your working folder.** `./sas-mcp/files` and `./sas-mcp/logs`, so
downloads and logs appear in your editor's file tree instead of a temporary
directory. The directory is `.gitignore`d on creation, since SAS output can
contain real data. `--file-dir` and `--log-dir` override.

## Changed

`--config NAME` now **pins** the server to that configuration and refuses
switches — naming one in `mcp.json` reads as "use this", not "start here".
`--allow-config-switch` restores the previous behaviour.

## Verified

| Deployment | Access method | Verified |
| --- | --- | --- |
| SAS OnDemand for Academics | IOM | ✅ macOS + Windows |
| Intranet SAS server | IOM | ✅ Windows |
| Intranet SAS server | SSH | not yet |
| Local Windows install | COM / IOM | not yet |
| Local Linux/UNIX install | STDIO | not yet |

"Not yet" means generated and unit-tested but not exercised against a running
SAS. Run `sas-mcp check` — it connects and verifies the stack end to end, and
if one of these fails for you that's a bug worth
[reporting](https://github.com/matise-joe-norc/sas-mcp/issues).

351 tests, all SAS-free, running in CI on Linux, macOS, and Windows against
Python 3.10, 3.12, and 3.14.
