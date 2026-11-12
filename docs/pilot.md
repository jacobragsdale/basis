# The pilot: what to run at the firm

Step 10.3 of [the plan](../PLAN.md#phase-10-measure-release-pilot), the roadmap's 2.8, is the maintainer's: only the
firm's environment can answer it. This page is the hand-over, one section per item of the plan's list: what to run,
on which machine, and what to send back. Nothing here waits on anything else, and each section stands alone, so run
them in any order, as access allows. Replace the capitalised placeholders (`ACCOUNT`, `SQLHOST`, `DB` and the rest)
with the firm's names, and send back what each section asks, with the exact `ERROR` line of any failure: every basis
failure ends with one line, `ERROR: REASON: FIX`, the last on stderr.

Three spikes already hold checklists of their own, which this page points to rather than repeats:
[Datadog](spikes/datadog.md#work-to-confirm-at-the-port), [Kerberos](spikes/kerberos.md#work-to-run-at-the-firm) and
[the data lake](spikes/data-lake.md#work-to-run-at-the-firm). [SQL columns](spikes/sql-columns.md#work-run-it-on-the-firms-queries)
and [SQL Server's `json`](spikes/sqlserver-json.md#work-to-run-at-the-firm) have one each too, which the plan's list
does not name; run them in the same visit if there is time.

Before any section, on the desktop, in a scratch folder:

```sh
uvx --from basis-job basis-job new repo pilot-etl   # from the firm's feed, as a developer would
cd pilot-etl
uv run basis doctor                                 # uv, git, the hooks, the feed
uv run basis check
```

Send back `basis doctor`'s output, and `basis check`'s last line. If `new repo` itself fails, that is the first
finding: send its `ERROR` line and stop there.

## 1. The desktop: `basis upgrade`, a workbook open in Excel, a UNC share, a non-ASCII user folder

What the code assumes, and what only a Windows desktop shows.

### `basis upgrade` while `basis.exe` runs

`basis upgrade` relocks basis-core and each part's package (`uv lock --upgrade-package`), then runs `uv run --locked
basis sync` and `uv run --locked basis check --full --no-qa` (`core/upgrade.py`). On Windows, `uv` replaces
`.venv\Scripts\basis.exe` while the `basis.exe` that started the upgrade is still running, and Windows does not let a
running executable be overwritten. Nothing in basis handles that today. It needs two releases on the feed within one
repo's range, such as 0.1.0 and a 0.1.1, so it waits for the second. Then, in PowerShell:

```powershell
uvx --from "basis-job==0.1.0" basis-job new repo upgrade-pilot; cd upgrade-pilot
git add --all; git commit -m "First commit"     # upgrade refuses a tree with changes no commit holds
uv run basis upgrade
uv run basis --version
```

**Send back:** whether `basis upgrade` finished, its output's last ten lines, `basis --version` after it, and any
`os error 32` ("being used by another process") or `Access is denied` in the output.

### A workbook open in Excel during the rename

basis writes a workbook to a temporary file beside it and renames it into place (`core/folders.py`, `write_bytes`).
Excel holds an open workbook locked, so the rename should fail; the question is what the user sees. Today a failed
rename raises the operating system's error unwrapped, which may print as a traceback rather than one `ERROR` line.
Make a scratch job whose one output is an `etl.ExcelReport` to a folder `reports` (`uv run basis docs job/reference`,
its `ExcelReport` section), with a `dev.env` that sets `BASIS_FOLDERS__REPORTS__PATH` to a local folder and whatever
else `uv run basis doctor --env dev` asks for. Run it once to make the workbook, open the workbook in Excel, and run
it again:

```powershell
uv run basis run JOB --params jobs/JOB/params/default.json --data jobs/JOB/tests/fixture --env dev --publish
# open the workbook in Excel, leave it open, then:
uv run basis run JOB --params jobs/JOB/params/default.json --data jobs/JOB/tests/fixture --env dev --publish
```

**Send back:** the second run's last twenty lines of stderr, and whether a `.NAME.*.basis-tmp` file was left in the
folder.

### A UNC share

A folder is configured by `BASIS_FOLDERS__NAME__PATH`, which must be absolute, and the unit tests use
`\\server\share` on Windows; no real share has been tried. Repeat the run above with the folder on the share the desk
uses, from Citrix and again from WSL, where the same share is a mount:

```powershell
# in dev.env: BASIS_FOLDERS__REPORTS__PATH=\\SERVER\SHARE\basis-pilot
uv run basis doctor --env dev      # checks each folder is reachable
uv run basis run JOB --params jobs/JOB/params/default.json --data jobs/JOB/tests/fixture --env dev --publish
```

```sh
# in dev.env: BASIS_FOLDERS__REPORTS__PATH=/mnt/SHARE/basis-pilot, however WSL mounts it at the firm
uv run basis doctor --env dev
uv run basis run JOB --params jobs/JOB/params/default.json --data jobs/JOB/tests/fixture --env dev --publish
```

**Send back:** both outputs, the time the run took, and whether the workbook arrived whole (open it).

### A clone under a non-ASCII user folder

git's output is decoded as UTF-8 (`core/clone.py`), which step 8.5 fixed for a locale that is not UTF-8. Only a user
folder such as `C:\Users\Ánna` proves it on the firm's image. With a user whose profile path has a non-ASCII letter,
or a folder made to stand for one:

```powershell
mkdir "C:\Temp\Ánna"; cd "C:\Temp\Ánna"
uvx --from basis-job basis-job new repo pilot-etl; cd pilot-etl
uv run basis check
git add --all; git commit -m "First commit"     # the commit hook runs basis check --staged
```

**Send back:** both commands' last lines, and `chcp`'s output in that console.

## 2. The pipeline and the platform: the checkout, the lake, Kerberos, Datadog

### An Azure Pipelines checkout's depth and tags

The generated `azure-pipelines.yml`'s check stage checks out with `fetchDepth: 0`, the whole history, since a bare
`- checkout: self` in a YAML pipeline is a shallow fetch of depth 1 by default, with no tags, and `basis check --full`'s
mutation testing compares a commit with where it branched, or its parent. Since step 7.12 the API contract check
compares `openapi.json` with the committed `openapi.released.json` and needs no history. Push the pilot repo to the
firm's Azure DevOps, create a pipeline from its `azure-pipelines.yml`, and run it on a branch with one commit beyond
`main`.

**Send back:** the pipeline's log of its checkout step and of `basis check --full`, every `SKIPPED:` line among them,
the mutation record's base commit, and whether the organisation's "Shallow fetch" setting (Project settings, Pipelines,
Settings) overrides the YAML's `fetchDepth`.

### The lake's rename on the firm's account

The [data lake spike's checklist](spikes/data-lake.md#work-to-run-at-the-firm) holds the commands: the account's
hierarchical namespace, the role assignments, DNS, a desktop's `az login`, and a pod with workload identity. basis then
needs, in a repo with a `Lake` output, its settings (`BASIS_LAKES__NAME__ACCOUNT`, `__CONTAINER`, `__AUTH` as `cli` on a
desktop and `workload_identity` in a pod):

```sh
uv run basis doctor --env dev    # lake.check: it lists the container and, where it may write, renames a scratch file
```

**Send back:** the spike's list, and `basis doctor --env dev`'s lines for the lake.

### Kerberos

The [Kerberos spike's checklist](spikes/kerberos.md#work-to-run-at-the-firm) holds the commands and the list to send
back, for SQL Server (`Trusted_Connection=yes` in `BASIS_DATABASES__NAME__CONNECTION_STRING`) from WSL, Citrix and a
pod. Note for the answer: basis's Oracle settings (`__DSN`, `__USER`, `__PASSWORD`, `__MODE`, `__CLIENT_LIB_DIR`) have
no external authentication yet, though the spike planned it, so an Oracle database that only takes Kerberos cannot be
reached until that is built; say which ones do.

### Datadog

The [Datadog spike's checklist](spikes/datadog.md#work-to-confirm-at-the-port) finds the Agent, its OTLP and log
settings, and sends a test span. basis exports traces only where `BASIS_TRACES__EXPORTER=otlp` and
`BASIS_TRACES__ENDPOINT` name the Agent's OTLP endpoint; it reads no `OTEL_EXPORTER_OTLP_*` variable. After the
checklist, deploy the pilot job to QA through its pipeline with those two settings, run it once, and find its trace.

**Send back:** the spike's list, and a screenshot or the JSON of the pilot run's trace and one of its log lines, with
the trace and the log linked or not.

## 3. The drivers' Arrow types: `datetime2(7)`, `datetimeoffset`, `money`, `uniqueidentifier`, and zoned timestamps

basis takes each query's columns as the driver's Arrow reader gives them (`databases/mssql.py`, `cursor.arrow_reader`;
`databases/oracle.py`, `fetch_df_batches`) and conforms them to the job's row. The integration tests cover `money`
against SQL Server 2022 in a container, and neither `datetimeoffset`, `uniqueidentifier` nor Oracle's
`TIMESTAMP WITH TIME ZONE`; the firm's servers and versions are what count. Save each script below and run it from
WSL, or the desktop, with a dev login. Neither writes anything.

`arrow_mssql.py`, with the connection string in `CONNECTION` (the same one basis's settings would hold):

```python
import os

import mssql_python

SQL = """SELECT CAST('2026-11-06T14:30:00.1234567' AS datetime2(7)) AS dt2,
               CAST('2026-11-06T14:30:00.1234567-05:00' AS datetimeoffset(7)) AS dto,
               CAST(1234.5678 AS money) AS money_value,
               CAST(NEWID() AS uniqueidentifier) AS guid,
               CAST(NULL AS datetimeoffset) AS dto_null"""

connection = mssql_python.connect(os.environ["CONNECTION"])
cursor = connection.cursor()
cursor.execute(SQL)
table = cursor.arrow_reader(batch_size=10).read_all()
print(table.schema)
print(table.to_pylist())
```

```sh
uv run --no-project --with mssql-python==1.17.0 --with pyarrow==26.0.1 python arrow_mssql.py
```

`arrow_oracle.py`, with `DSN`, `USER` and `PASSWORD` set:

```python
import os

import oracledb
import pyarrow as pa

SQL = """SELECT TIMESTAMP '2026-11-06 14:30:00.123456 America/New_York' AS tstz,
                CAST(TIMESTAMP '2026-11-06 14:30:00 -05:00' AS TIMESTAMP WITH LOCAL TIME ZONE) AS tsltz,
                CAST(1234.5678 AS NUMBER(19,4)) AS amount
         FROM dual"""

connection = oracledb.connect(user=os.environ["USER"], password=os.environ["PASSWORD"], dsn=os.environ["DSN"])
for batch in connection.fetch_df_batches(SQL, size=10, fetch_decimals=True):
    table = pa.table(batch)
    print(table.schema)
    print(table.to_pylist())
```

```sh
uv run --no-project --with oracledb==26.1.0 --with pyarrow==26.0.1 python arrow_oracle.py
```

**Send back:** both scripts' output, the servers' versions (`SELECT @@VERSION`; `SELECT banner FROM v$version`), and
the session's time zone on Oracle (`SELECT sessiontimezone FROM dual`). What to look for: whether `datetime2(7)` keeps
its seventh digit or rounds to microseconds, whether `datetimeoffset` arrives zoned or as text, whether `money` is a
decimal, whether `uniqueidentifier` is text or bytes, and whether the zoned timestamps keep their zone.

## 4. Managed agent settings (8.9)

basis's agent hooks live in the repo (`.claude/settings.json`, `.cursor/hooks.json`, `.codex/hooks.json`), so a user
can turn them off; only managed settings, which a repo cannot edit, close that ([the agent-hooks
spike](spikes/agent-hooks.md#each-no-and-its-fallback)). They are, per tool:

- Claude Code: `managed-settings.json` in `C:\Program Files\ClaudeCode\` on Windows and `/etc/claude-code/` in WSL,
  with `allowManagedHooksOnly`.
- Cursor: an enterprise `hooks.json` in `C:\ProgramData\Cursor\` and `/etc/cursor/`.
- Codex: `[hooks]` in its `requirements.toml`, with `allow_managed_hooks_only`.

This is a question for the desktop team, not a command: whether the firm can deploy a file to those paths on every
developer's desktop and WSL image, keep users from editing it, and which of the three agents the firm allows.

**Send back:** the answer per tool and per platform (Citrix, WSL), who owns that deployment, and how long a change to
it takes to reach the desktops.
