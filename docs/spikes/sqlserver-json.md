# Spike 0.6: SQL Server JSON

mssql-python handles SQL Server's native `json` type as text, in reads, writes and `CREATE TABLE`. A `json` column reads
as `str` and takes a bound `str`. The staging path basis writes through works with it unchanged, and so do every JSON
function and JSON indexes. Step 5.5 can build on it with four rules this spike found:

- encode each value to text before binding it;
- write decimals as JSON strings, and floats in plain notation;
- never key on, match on or compare a `json` column;
- fetch char data as UTF-8, because the driver's default Arrow path loses characters outside Unicode's Basic
  Multilingual Plane.

Since step 5.0b, basis's frames are pandas, not polars: the polars calls below are what this spike ran, and step 5.5
encodes and decodes in pandas, to the same four rules.

Run on 2026-11-07 against a SQL Server 2025 container on Linux, under both of mssql-python's native providers. Where a
fact about the firm was missing, this result assumes the firm runs SQL Server 2025 or Azure SQL with the type, as the
maintainer was told it has been added. Each question ends with a **best guess**, the behaviour basis should build to
now. The checklist at the end confirms it at the port.

## Versions

- SQL Server 2025 (RTM-CU10) (KB5125471), 17.0.5035.5, Enterprise Developer Edition on Linux (Ubuntu 24.04.5), from
  `mcr.microsoft.com/mssql/server:2025-latest` (digest `sha256:245f6b1c8903…`), run with `--memory 2560m`. The database
  was at compatibility level 170, with collation `SQL_Latin1_General_CP1_CI_AS`.
- mssql-python 1.17.0, as `uv.lock` pins it, with its two native providers:
  - `msodbcsql18`, the default: mssql-python-odbc 18.6.2.2, whose driver reports 18.06.0002;
  - `mssql-odbc`, the preview Rust driver in mssql-python-rs 0.4.0, which reports 18.6.2.1.
- Python 3.14.7, polars 2.1.0, pyarrow 26.0.1 and uv 0.13.6, on Debian 13.

"Verified here" means this run showed it, under both providers unless the answer names one. "From docs" means
Microsoft's documentation or mssql-python's wiki, issues and releases, read on 2026-11-07.

## Answers

| Q | Question | Answer | Source |
|---|---|---|---|
| 1 | Does SQL Server 2025 have the native `json` type? | Yes, generally available. | from docs; verified here on 17.0.5035.5 |
| 1 | Does Azure SQL? | Yes: Azure SQL Database, and Azure SQL Managed Instance on the SQL Server 2025 or Always-up-to-date update policy. SQL Server 2022 and earlier, no. | from docs |
| 1 | Does it need a preview flag or a compatibility level? | No. It works at every compatibility level, with nothing to switch on. | from docs; levels 100, 130, 150, 160 and 170 verified here |
| 2 | What does a `json` column read as? | `str`, and NULL as `None`, through `fetch*` and through `arrow_reader` (Arrow `large_string`, polars `String`). It is never bytes, and never an unsupported-type error. | verified here |
| 2 | Does `cursor.description` say it is `json`? | No. It says `str` with size 0, the same as `varchar(max)`. `sys.columns` says `json`, and basis's `describe` reads that. | verified here |
| 2 | Is the Arrow read exact? | No, under the default provider. It drops some 4-byte UTF-8 characters, such as emoji, from `json` and `varchar(max)` UTF-8 values, with no error. | verified here |
| 2 | Is `json` in mssql-python's docs or changelog? | The wiki lists `json` as not yet supported, its page on the preview driver says that driver returns it as `str`, and no release note mentions it. | from docs |
| 3 | Can a bound `str` write a `json` column? | Yes, through `execute`, `executemany`, `?` and `%(name)s`, up to 4 MB, and through basis's own staging path. | verified here |
| 3 | Can a `dict` or `list` be bound? | No: `TypeError`. Only bulk copy, which basis does not use, takes them. | verified here |
| 3 | Does the server validate the JSON? | Yes: error 13609 for text that is not a JSON object or array. | verified here |
| 3 | Does `executemany` bind every value exactly, and count it? | No, under the driver's own typing: a column whose longest value is ASCII loses what the database's code page cannot hold, without an error, and a batch holding a string past 4,000 UTF-16 units counts one row. basis-core gives each column's type, and sends such a row by itself. | verified after the spike, on SQL Server 2022 |
| 4 | Do `JSON_VALUE`, `JSON_QUERY`, `OPENJSON` and `JSON_OBJECTAGG` work on the type? | Yes, and so do `JSON_ARRAYAGG`, `JSON_OBJECT`, `JSON_MODIFY`, `.modify`, `ISJSON`, `JSON_PATH_EXISTS` and `JSON_CONTAINS`. | verified here |
| 4 | Is a `json` column indexable? | Not as the key of an index or a primary key. It can be an included column or a filtered index's condition, and it takes `CREATE JSON INDEX` and an indexed computed column. | verified here; the indexes were created and queried, but their plans not inspected |
| 4 | Can a `json` value be compared? | No: `=`, `GROUP BY`, `ORDER BY`, `DISTINCT` and `UNION` are refused. `UNION ALL` and `IS NULL` work. | verified here |
| 5 | Does a polars struct or list round-trip? | Yes, for decimals as JSON strings, every integer, plain-notation floats, dates, datetimes, unicode and nesting. No for floats in exponent form, for decimals written as JSON numbers once polars reads them back, and for 4-byte characters through the default Arrow read. | verified here |

## 1. Which SQL Server versions have the type

The type's page lists SQL Server 2025 (17.x), Azure SQL Database, Azure SQL Managed Instance and SQL database in Fabric.
It says the type and JSON indexes are generally available in each, the Managed Instance "with the **SQL Server 2025** or
**Always-up-to-date** update policy". It also says the type "is available under all database compatibility levels". This
run created, filled and queried a `json` column at levels 100, 130, 150, 160 and 170, with the container's default
settings and no preview or database-scoped configuration. On a server without the type, `CREATE TABLE ... json` fails as
an unknown type. That message, error 2715, "Cannot find data type json", is from memory and was not run here.

**Best guess:** build to SQL Server 2025 and Azure SQL, and gate nothing on a version in code. A server without the type
refuses `basis ddl`'s `CREATE TABLE` with its own error, and `describe` finds no `json` column there. Step 5.5's
integration test runs `mcr.microsoft.com/mssql/server:2025-latest`, which used under 900 MB of its 2560 MB limit each
time it was measured during this run.

## 2. Reading

A `json` column arrives as `str` under both providers, through `fetchall` and `fetchone`, and through `arrow_reader` as
Arrow `large_string`, which `pl.from_arrow` makes a polars `String`. NULL arrives as `None`. Values of 100, 3,000,
3,136, 3,137, 4,000, 4,001, 8,000 and 8,001 characters read back equal, as did 64 KB, 1 MB and 4 MB, and 2-byte
characters past 8,000 bytes. So issue [#261](https://github.com/microsoft/mssql-python/issues/261), open since 2025,
does not reproduce here. It reports that a `json` value longer than 3,136 characters broke the connection, against
Fabric on 0.x and 1.0.

The metadata does not name the type:

- `cursor.description` gives `('doc', str, None, 0, 0, 0, True)`, as for any `varchar(max)`. The driver's ODBC type code
  (`cursor._column_sql_types`, a private field) is 12, `SQL_VARCHAR`, under `msodbcsql18`, and -10, `SQL_WLONGVARCHAR`,
  under `mssql-odbc`.
- `msodbcsql18` does not negotiate the TDS `JSONSUPPORT` feature. So the server sends a `json` column to it as
  `varchar(max)` with collation `Latin1_General_100_BIN2_UTF8`, as the type's page says it does for such drivers, and
  `sys.dm_exec_describe_first_result_set` says `varchar(max)` on that connection. `mssql-odbc` negotiates the feature,
  and the same query says `json`. ODBC Driver 18's release notes, up to 18.7.1, never mention `json`.
- `sys.columns` joined to `sys.types`, as basis's `describe` queries it, gives type `json` with `max_length` -1. Its
  `_column` makes that `DestColumn(type="JSON", length=None)`.

The server normalises the text it returns:

- it drops insignificant whitespace;
- of duplicate keys, it keeps the first;
- it decodes escapes such as `\/` and `é`, and re-escapes control characters, as `\u001F`;
- it keeps key order and trailing zeros, as in `12.50` and `1.0`.

So a value reads back equal, but not byte for byte: `{ "a" : 1 , "a" : 2 , "b" : "\/é" }` comes back as
`{"a":1,"b":"/é"}`.

mssql-python's own word on it: the wiki's "Known Issues and Unsupported Features" lists `json` among the "SQL Data Types
(the support of these types are in the roadmap for future releases)". Its page on the preview `mssql-odbc` driver says
that driver "returns `xml`, `json`, and `vector` values as `str`". No GitHub release from 1.0.0 to 1.17.0 mentions JSON.

**A defect in the Arrow read.** Under `msodbcsql18`, the default provider, `arrow_reader` drops some characters outside
Unicode's Basic Multilingual Plane, those that take 4 bytes in UTF-8, such as emoji and CJK Extension B, from `json` and
`varchar(max)` UTF-8 values. It raises no error. `["😀"]` arrives as `[""]`. A document of 300,000 emoji arrives one
short, and one of 1,000 `a😀é日` arrives two short. These read exactly:

- `varchar(100)` UTF-8 and `nvarchar(max)` columns;
- `fetchone` of the same values;
- the `mssql-odbc` provider.

None of mssql-python's issues reports it; searches for "arrow utf-8", "arrow emoji", "surrogate", "arrow truncat", "utf8
varchar arrow" and "supplementary" found nothing. It reaches past `json`: basis-core loses these characters today from
any `varchar(max)` column with a UTF-8 collation. Three workarounds were verified:

- `connection.setdecoding(mssql_python.SQL_CHAR, encoding="utf-8", ctype=mssql_python.SQL_CHAR)` before the query.
  `arrow_batch` takes its C type from that setting, so Arrow then fetches char data as UTF-8 rather than UTF-16, and
  every case above read exactly. A `varchar` column in the CP1252 collation (`café £`) still read correctly. This was on
  Linux, where the driver converts char data to the locale's charset; basis's image, `python:3.14-slim`, sets
  `LANG=C.UTF-8`. On Windows, char data converts to the ANSI code page, so this setting is unverified there, and likely
  wrong.
- `CAST(doc AS nvarchar(max))` in the query.
- The `mssql-odbc` provider.

**Best guess:**

- basis reads a `json` column as text through Arrow, as it reads every column today. It then decodes it with polars'
  `str.json_decode(dtype=...)` into the struct or list dtype the job declares for that column. The driver cannot say
  which columns are `json`, so the declaration is the only source of the dtype.
- basis-core's read connections set the UTF-8 char fetch above on Linux (pods and WSL) now, for every read and not only
  `json`. An integration test reads an emoji from a `json` column and from a `varchar(max)` UTF-8 column. On Windows,
  basis keeps the default until the checklist shows the setting is exact there, and basis's `json` docs tell a job to
  select a `json` column as `CAST(col AS nvarchar(max)) AS col`, which is exact everywhere.
- basis stays on the default `msodbcsql18` provider. The Rust driver is a preview, untested with threads, and has the
  binding defect in section 3.
- The defect goes upstream with the repro under "Upstream" below.

**Found by the fix, after the spike.** basis-core's fix, run on 2026-11-07 against SQL Server 2022 (16.0.4305.5) on
Linux with the same driver, found the default loses more than the above. `fetchall` and `fetchone` drop a 4-byte
character that straddles the driver's chunks of 4,095 UTF-16 units in a `varchar(max)` UTF-8 value, as in
`"a" * 4094 + "😀b"`. Arrow dropped `["😀"]`'s emoji from a `varchar(200)` UTF-8 column too, wherever the result had a
`varchar(max)` or `nvarchar(max)` column beside it. The UTF-8 char fetch read each exactly, and code page 1252 `varchar`
and `nvarchar` text as well. Under an ISO-8859-1 locale on Linux it does not: Arrow raises `UnicodeDecodeError`, and
`fetchall` returns `bytes`, with each character Latin-1 lacks replaced. So basis-core sets it on each connection on
Linux, where the locale is UTF-8 or glibc's C (`fetches_char_as_utf8` in
`packages/core/src/basis/core/databases/mssql.py`), and keeps the driver's default on Windows and macOS, both
unverified, and under any other locale. `packages/job/tests/integration/test_text.py` reads and publishes such text, a
`json` column's on SQL Server 2025, and code page 1252 `varchar(10)` columns filled with `é` and with `€`, whose UTF-8
is 20 and 30 bytes: through Arrow and `fetchall`, the UTF-8 char fetch cut none of them.

**In basis's image.** A job repo's image, built from the managed Dockerfile on `python:3.14-slim` and run under its
own environment, has `LANG=C.UTF-8` from that base, so its connections fetch char data as UTF-8, and read `["😀"]`
exactly. With `LANG` unset, or `LANG=C`, Python's locale coercion (PEP 538) sets `LC_CTYPE=C.UTF-8`, with the same
result. `LC_ALL=C`, `LC_ALL=POSIX` or `PYTHONCOERCECLOCALE=0` leaves the C locale, whose charset is ASCII
(`ANSI_X3.4-1968`). The driver does not convert to ASCII, and sends UTF-8 instead: its programming guidelines for
Linux and macOS say that for a locale whose charset is not in its list, "it defaults to UTF-8" (from docs, read
2026-11-07). The UTF-8 char fetch read exactly there too, in the image, so the rule takes that charset as well, by
glibc's name for it alone. With it, each of these six environments read the emoji exactly through basis in the image;
under a rule of UTF-8 alone, the last three lost it. CI's job-image reads one from a `varchar(max)` through basis in
the image, under the image's own locale, and fails if the rule is off there or the emoji is lost.

## 3. Writing

basis writes neither through bulk copy nor through table-valued parameters. `mssql.py` on `main` (6d29ec6) stages a
frame in `#etl_stage` with `SELECT TOP 0 ... INTO` from the destination, fills it with `executemany("INSERT ... VALUES
(?, ...)")`, then runs an `UPDATE ... FROM` and an `INSERT ... SELECT ... WHERE NOT EXISTS`. The probe ran exactly that
sequence against a table with a `json` column, in one transaction:

- `SELECT TOP 0 ... INTO` made the stage's column `json` too;
- the `executemany` bound `str`;
- the `UPDATE` changed the existing row, and the `INSERT` added the new one.

A bound `str` writes a `json` column, by `?` and by `%(name)s`, through `execute` (up to 4 MB) and `executemany` (mixing
7 B, 5 KB, 100 KB and 4-byte characters, with `None`). The server converts the parameter implicitly. mssql-python binds
an ASCII-only `str` as `varchar` and others as `nvarchar`, and both convert.

What does not bind:

- A `dict` or `list` raises `TypeError: Unsupported parameter type: The driver cannot safely convert it to a SQL type`.
  Bulk copy takes both, and serialises them itself.
- A string with a lone surrogate cannot be encoded by the driver, which raises a `RuntimeError` from pybind11.
- Under `mssql-odbc` alone: when every value of a `json` parameter in one call is `None`, the call raises
  `ProgrammingError: Invalid precision or scale value`. A `None` alongside a `str` binds, and `msodbcsql18` binds both.

Bulk copy is not on basis's path, but it was checked. `cursor.bulkcopy` takes `str`, `dict` and `list` into a `json`
column, and refuses invalid JSON on the client with `ValueError: Invalid JSON string: ...`. `bulkcopy_arrow` takes an
Arrow string column.

The server validates every value. It refuses each of these with error 13609, raised as `mssql_python.ProgrammingError`
(SQLSTATE 42000), "JSON text is not properly formatted. Unexpected character 'X' is found at position N.":

- truncated JSON;
- a bare scalar, such as `1` or `"x"`;
- the literal `null`;
- an empty string;
- `NaN`, as `json.dumps` writes it;
- a trailing comma;
- single quotes.

The type takes only an object or an array at the top. In a transaction, an `executemany` of three rows with one invalid
in the middle raised, and left none of the three rows in the table.

Other errors this run raised, numbered as `ERROR_NUMBER()` gave them:

| Error | Raised as | Cause |
|---|---|---|
| 1007 | `DataError` | "The number '1e38' is out of the range for numeric representation (maximum precision 38)": see section 5 |
| 13645 | `ProgrammingError` | "Nested level of JSON document exceeds limit 128." |
| 13646 | `ProgrammingError` | "Key string length exceeds limit 7998 bytes in JSON type." |
| 13636 | `ProgrammingError` | "The JSON data type cannot be compared or sorted, except when using the IS NULL operator." |

basis's delete by values raises 13636 when it matches on a `json` column, since it joins `t.col = m.col`. A `json`
primary key is refused: "is of a type that is invalid for use as a key column in an index". So an upsert keyed on a
`json` column cannot exist.

**Best guess:**

- basis encodes each struct and list value to JSON text itself, by the rules in section 5, and binds the text. A null
  value binds as SQL NULL, never as the text `null`, which the type refuses. polars' `struct.json_encode()` writes the
  text `null` for a null struct, so the null has to be kept explicitly.
- The write path stays as it is: no bulk copy, no table-valued parameters, and the stage. basis-core now gives each
  column's type to `executemany`, every string as `nvarchar`, as the next paragraph says.
- The check of a plan refuses a `json` column as an upsert key, or in a delete by values, before anything connects.
- basis turns errors 13609, 1007, 13645 and 13646 into one ERROR line naming the table and the column. One bad row fails
  its whole `executemany` call, so the server's error cannot name the row; basis's encoder checks the limits that can
  fail (section 5) first, and names the row itself.

**Found by basis-core's write fix, after the spike.** This was run on 2026-11-07 against SQL Server 2022 (16.0.4305.5)
on Linux, with mssql-python 1.17.0 and `msodbcsql18` 18.6.2.2, in a database collated `SQL_Latin1_General_CP1_CI_AS`.
It went through basis's own publish, by `Append`, `ReplaceWhere` and `Upsert`, into `nvarchar(max)`, into `varchar(max)`
with a UTF-8 collation, and into `varchar` in code page 1252. The probe above bound a `json` column's values alone or in
small batches. In a batch, `executemany` types each column from one value, the longest by characters, and four defects
follow:

- **Silent loss.** When that value is ASCII, the whole column binds as `varchar`. The server converts every value
  through the database's code page, whatever the column's own type. A publish of `hello world`, `日本`, `Ω` and `😀`
  committed `hello world`, `??`, `?` and `:-D`, with status `published`, into `nvarchar(max)` and UTF-8 `varchar(max)`
  alike. Text the code page holds, such as `Zoë` and `€`, arrives intact. That is why `test_text.py`'s batches passed:
  their longest values were not ASCII.
- **A raise.** A value with more UTF-16 units than that longest value raises `RuntimeError: Input string exceeds allowed
  column size at parameter index 1`. Six emoji (12 units) beside `hello world` (11) are enough.
- **A count of one.** When that value is longer than 4,000 UTF-16 units (the driver's `MAX_INLINE_CHAR`), the driver
  streams the column as `nvarchar(max)`. It then falls back to `execute` for each row and keeps only the last row's
  `rowcount`.
  So it writes every row and reports 1, and basis's count check refused the publish: "wrote 1 rows, and the run
  computed 5: nothing is committed". The cut-off is exactly 4,000 units, measured on the client whatever the column:
  `"a" * 4000` and 2,000 emoji published; `"a" * 4001`, `"é" * 4001`, 2,001 emoji and `"a" * 3999 + "😀"` (4,000
  characters, 4,001 units) did not. `nvarchar(max)` and UTF-8 `varchar(max)` behaved the same.
- **A decimal.** A decimal whose text (`format(value, "f")`) is longer than 38 characters raises `Invalid precision or
  scale value`. 38 digits and a decimal point are enough.

basis-core now gives each column's type through `setinputsizes`, for every statement:

- a string as `nvarchar`, sized to the longest the statement binds in UTF-16 units, or `nvarchar(max)` past 4,000;
- a decimal as its text (`varchar(41)`), which the server converts exactly;
- an integer as a `bigint`;
- a float, bit, date or datetime as the driver types it.

Rows go in the frame's order. Each run of rows whose strings all bind as `nvarchar(n)` is one `executemany`. Each row
holding a string longer than 4,000 units goes as a statement of its own, in its place, with the same types: so a plain
insert's identity column numbers the rows as the frame orders them. The driver's own typing of a batch took a string
starting `POINT`, `LINESTRING` or `POLYGON` for geometry, and sized it by its characters; the types given leave no
such guess. Every count is a statement's own. The server counts each stage, with `SELECT COUNT_BIG(*)`, which is exact
because the `#temp` table is the session's alone. Each run of rows is a span: a batch's records its
`db.operation.batch.size`, and a run of rows sent one by one its `basis.statements`, so a trace shows what each such
row costs. These are medians of five runs in a local container, each rolled back:

| Case | Before | After |
|---|---|---|
| 50,000 short rows, insert | 0.91 s | 0.84 s |
| 50,000 short rows, upsert, with the stage's count | 1.20 s | 1.07 s |
| 50,000 rows, one of 5,400 characters, insert | 8.84 s, refused | 1.09 s |
| 2,000 rows of 5,400 characters each, insert | 0.68 s, refused | 0.65 s |
| 2,000 rows of 5,400 characters each, upsert | 0.69 s, refused | 0.80 s |

A row past 4,000 units costs one round trip, 0.33 ms here, as it did inside the driver. A `json` table whose documents
are mostly that long publishes one round trip per row, so 50,000 such rows take about 50,000 times the firm's latency.

A `varchar` whose collation's code page is not UTF-8 replaces a character it cannot hold, as `日` with `?`, without an
error, and it does so from an `nvarchar` parameter too. So `describe` reads the code page of each `char`, `varchar`
and `text` column (`COLLATIONPROPERTY(collation_name, 'CodePage')`), and `check_values` refuses any value that the
code page's Python codec cannot encode, before anything is written. A column of an alias type, as
`CREATE TYPE code FROM varchar(50)`, is described as the system type it is an alias of (`TYPE_NAME(system_type_id)`),
so it takes text and is checked as a `varchar`; `check_table` refused it before, as a type it did not know. Every code
page SQL Server 2022 has is one of Python's codecs, except 65001, which is UTF-8, and 0, which only Unicode-only
collations use and no `varchar` can take. In a code page 1252 `varchar`, the server kept 255 of the Basic Multilingual
Plane's 63,487 characters exactly: the 250 that Python's `cp1252` encodes, and U+0081, U+008D, U+008F, U+0090 and
U+009D, which Python refuses. So among the characters the column can hold, the check refuses only those five
controls.

A string is measured in its column's own units too: `nvarchar(n)` holds n UTF-16 units, so `nvarchar(6)` holds three
emoji and not six, and `varchar(n)` n bytes of its code page, so a UTF-8 `varchar(10)` holds five `é` and a code page
932 one `日本語abcd`. The check counted characters before, and the server raised such a value, once the rows before it
were written. The check encodes each distinct value once, and runs once for each table a publish writes: the publish
describes the table again in its transaction, and checks the values only where the table is not the one the run
checked them against. 50,000 distinct values beyond ASCII take 38 ms to check against code page 1252.

What the count check cannot see: each count is SQL Server's own, for its statement, and a trigger can change it. An
`INSTEAD OF INSERT` trigger that drops a row and counts its own insert makes the counts differ, and the publish is
refused. A trigger that sets `NOCOUNT ON`, as most do, leaves the outer statement's count, so the check passes a row
the table never got. An index with `IGNORE_DUP_KEY = ON` drops a duplicate with only a warning, and the server counts
that row as unwritten. `test_text_writes.py` shows the publish refused in each place such a row can go: in a batch,
by itself, and through the upsert's stage.

The same batches went through Oracle too, by python-oracledb 26.1.0 in thin mode, into Oracle Free 23ai's AL32UTF8
database (`MAX_STRING_SIZE` `STANDARD`), and lost nothing. The batches whose longest value is ASCII, and six emoji
beside `hello world`, published exactly into `VARCHAR2(4000 CHAR)` and `CLOB`, as did values either side of 4,000
units into `CLOB`. Two were refused, with an error:

- `VARCHAR2(4000 CHAR)` holds 4,000 bytes under `STANDARD`, so `"a" * 3998 + "😀"` (3,999 characters, 4,002 bytes)
  raised ORA-12899, since basis's check counted characters. `describe` now reads `data_length` and `char_used`: a
  column in characters holds its bytes too, and one in bytes is measured in them, so the check refuses that value
  before anything is written.
- basis binds Oracle's writes as frames through Arrow, which binds strings as `VARCHAR2`. So a `CLOB` value over 16,000
  UTF-8 bytes raises ORA-01461, through every write mode. Bound as Python tuples, 100,000 characters insert. But a
  `MERGE` from `dual` refuses them even then, with `setinputsizes(DB_TYPE_CLOB)`, with `TO_CLOB(:2)`, or with
  `CAST(:2 AS CLOB)`. The fix left to try is temporary LOBs from `connection.createlob`, and it needs its own change,
  since a LOB must replace every value of the column in the batch.

Oracle replaces a character its character set cannot hold without an error, as SQL Server's code pages do. So
`describe` reads the database's sets from `nls_database_parameters`: `NLS_CHARACTERSET` for `CHAR`, `VARCHAR2` and
`CLOB`, and `NLS_NCHAR_CHARACTERSET` for `NCHAR`, `NVARCHAR2` and `NCLOB`. `CHARSETS` in
`packages/core/src/basis/core/databases/oracle.py` gives Python's codec for the sets basis knows: AL32UTF8 and UTF8 hold
any text, as does AL16UTF16, WE8MSWIN1252 is `cp1252`, WE8ISO8859P1 `latin-1` and US7ASCII `ascii`. `check_values`
refuses a value its column's set cannot hold, and, in a set basis does not know, any value beyond ASCII, naming the
set. Oracle's UTF8 is CESU-8, which takes 6 bytes for a character outside the Basic Multilingual Plane where UTF-8
takes 4, so a byte limit there is checked short of the server's. CI's Oracle is AL32UTF8, so no live test of a
non-Unicode set is possible there, and unit tests hold the mapping and the refusals.

## 4. Functions, indexes and limits

Every JSON function tried works on a `json` column, as the type's page says ("All JSON functions support the **json**
data type without requiring code changes"):

- `JSON_VALUE`, with `RETURNING int` too;
- `JSON_QUERY`;
- `OPENJSON`, by default and `WITH` a schema, with `AS JSON` into `nvarchar(max)` or `json`;
- `JSON_OBJECTAGG` and `JSON_ARRAYAGG`, which nest `json` values as JSON, not as escaped strings;
- `JSON_OBJECT('k': doc)`, which nests the same way;
- `JSON_MODIFY`, and `UPDATE ... SET doc.modify('$.a', ...)`;
- `ISJSON`, `JSON_PATH_EXISTS` and `JSON_CONTAINS`;
- a `CHECK (JSON_PATH_EXISTS(doc, '$.a') = 1)`, which refused a document without `$.a`;
- `DEFAULT ('{}')`.

Functions that return JSON (`JSON_QUERY`, `JSON_OBJECTAGG`, `JSON_ARRAYAGG`, `JSON_MODIFY`, `JSON_OBJECT`) are described
as `varchar(max)` UTF-8 to `msodbcsql18` and as `json` to `mssql-odbc`, and reach Python as `str` either way. `CAST(doc
AS nvarchar(max))` works, while a cast to a string type too short for the value fails with 13639 rather than truncate.

The type cannot be compared: `=`, `GROUP BY`, `ORDER BY`, `DISTINCT`, and `UNION`, `INTERSECT` or `EXCEPT` all fail.
`UNION ALL` and `IS NULL` work, and comparing `CAST(... AS nvarchar(max))` on both sides works. That compares text, so
key order counts.

Indexes:

- a `json` column cannot be a key column of an index ("invalid for use as a key column in an index or statistics"), and
  so cannot be in a primary key;
- `CREATE INDEX ... (id) INCLUDE (doc)` works;
- `CREATE INDEX ... WHERE doc IS NOT NULL` works;
- `CREATE JSON INDEX jx ON t (doc)` and `... FOR ('$.a')` work;
- a `PERSISTED` computed column of `CAST(JSON_VALUE(doc, '$.a') AS int)` takes an index.

The index probes created each index, wrote through it and queried the table; the plans were not inspected.

Limits:

- 128 levels of nesting are accepted and 129 refused (13645), for arrays and for objects;
- a key of 7,998 bytes is accepted and one of 7,999 refused (13646);
- an array of 65,536 elements and an object of 32,001 distinct keys were accepted, though the type's page gives 65,535
  elements and 32,000 unique keys as limits;
- from the type's page, not checked here: up to 2 GB stored, string values up to 536,870,911 bytes, and up to 65,535
  properties in one object.

`DATALENGTH` of a 43-character document was 143 bytes, so the binary form is larger than the text for small documents.

DDL, verified here:

- `ALTER TABLE ... ADD doc json NULL` works;
- `ALTER COLUMN` from `nvarchar(max)` to `json` works when every row is valid, and fails whole (13609) when one row is
  not;
- `ALTER COLUMN` from `json` back to `nvarchar(max)` is refused, as the type's page says.

**Best guess:**

- `basis ddl` writes a struct or list column as `name JSON NULL` (or `NOT NULL`), with no `CHECK`, never in the primary
  key, and in no index of its own.
- `describe` maps `sys.types` name `json` to a new kind, `json`, which holds struct and list columns. The destination
  check accepts `JSON` for that kind, and `NVARCHAR` with no length as the fallback below.
- No SQL that basis generates compares a `json` column. A comparison basis makes, as in a regress, decodes both sides in
  polars and compares the values.
- An index on what a document holds is the job owner's own DDL, a `CREATE JSON INDEX` or an indexed computed column of
  `JSON_VALUE`, which `basis ddl` does not write.

## 5. Round trip through polars

The `json` type stores a JSON number as a `decimal` of precision 38, so a number keeps its digits only within that
range:

| JSON number as written | Stored as |
|---|---|
| Plain, up to 38 digits: `12345678901234567890.1234567890`, a 38-digit decimal, `100.10`, `2^53+1`, int64's minimum and maximum, a 38-digit integer, `0.1`, `0.30000000000000004`, `0.000012345678901234567`, and `0.` followed by 37 zeros and a 1 (38 places) | exactly, trailing zeros kept |
| Plain, over 38 digits, with the excess in the integer part | refused, 1007 |
| Plain, over 38 digits, with the excess in the fraction: `1.123456789012345678901234567890123456789`, and `1.2345678901234567e-30` written out in 46 places | silently cut to 10 places: `1.1234567890`, `0.0000000000` |
| Exponent notation | parsed as a float64, then stored with 10 places: `1.5E+3` becomes `1500.0000000000`, `1.2345678901234567e-05` becomes `0.0000123457`, `1e27` becomes `1000000000000000013287555072.0000000000`, and `1e-38` and `5e-324` become `0.0000000000` |
| Exponent notation at 1e38 or more: `9.99e37`, `1e38`, `1.7976931348623157e308` | refused, 1007 |
| `-0.0` | `0.0` |

That matters because both encoders use exponent notation:

- polars' `struct.json_encode()` writes small and large floats in exponent form: `1e-7`, `1.2345678901234566e-7`,
  `1e-30`, `1e+16` and `1.5e+20`, as observed. It writes moderate ones plainly, such as `0.000012345678901234568` and
  `123456789.123`. It writes NaN and infinity as `null`, without error.
- Python's `json.dumps` uses exponent form under 1e-4 and from 1e16, and writes `NaN`, which the server refuses.

Decimals have the opposite problem, on the read. polars' `str.json_decode` parses a JSON number into a `Decimal` field
through float64, so `12345678901234567890.1234567890` reads back as `12345678901234567787.7719597056`. A JSON string
decodes exactly. polars' `json_encode` writes a `Decimal` as a string.

A three-row frame was written with `executemany` through basis's statement shape, read back with `arrow_reader`, and
decoded with `str.json_decode(dtype=...)` to the original dtypes. Its `doc` was a struct of:

- `Decimal(38,10)` and `Decimal(20,2)`;
- `Int64` up to its maximum, and `2^53+1`;
- `Float64`;
- `Date` from 1900-01-01 to 9999-12-31;
- naive and UTC `Datetime("us")`;
- `Boolean`;
- `String` with emoji, CJK, Hebrew, a combining accent, quotes and a backslash;
- a nested struct, a list of structs, an empty list and a null field.

Its `tags` was a `List(String)`, and the third row was null in both. Encoded with polars' `json_encode`, a list column
going through a one-field struct, every value came back equal except two:

- `1.2345678901234567e-30` came back `0.0`, as the table above predicts;
- under `msodbcsql18`'s default Arrow read, `["a", "😀"]` came back `["a", ""]`, the defect in section 2. With the UTF-8
  char fetch, and under `mssql-odbc`, it round-tripped.

Encoded with `json.dumps(default=...)`, `1.2345678901234568e-05` also came back `1.23457e-05`.

Dates and datetimes become ISO 8601 strings, since JSON has no date type, and decode back to `Date` and `Datetime` with
the declared dtype. polars writes a naive datetime as `2026-11-07 13:45:12.123456` and a UTC one as
`2026-11-07T13:45:12.123456+00:00`. Python's `isoformat()`, with a `T`, decodes as well.

Other text:

- Unicode, control characters and NUL (`\u0000`) round-trip as values, though the server re-escapes them.
- A lone surrogate either cannot be bound, as in section 3, or, escaped as `\ud800`, comes back as U+FFFD.

For depth, 60 nested polars lists inside an object round-tripped, within the type's 128 levels.

**Best guess:**

- basis encodes struct and list values itself, in Python, walking each row's values:
  - a mapping and a list as JSON;
  - `str`, `int`, `bool` and null as JSON;
  - a `Decimal` as a JSON string of its digits;
  - a `date` or `datetime` as an ISO 8601 string, a zoned datetime with its offset;
  - a `float` in plain notation from its shortest repr, `format(Decimal(repr(x)), "f")`, which the type stores exactly
    when it has at most 38 digits.
- It refuses NaN, infinity, and any number whose plain form has more than 38 digits, which for a float of full precision
  means roughly under 1e-21 or from 1e38. The refusal is one ERROR line naming the table, column and row. polars'
  `json_encode` stays a faster path only for structs that hold no float.
- basis decodes with polars' `str.json_decode(dtype=...)`, which is exact for everything basis writes. A document
  someone else wrote, with decimals as JSON numbers of more than about 15 significant digits, loses digits on the read.
  The checklist asks how the firm's migrated documents write decimals.

## Fallbacks

| No | Fallback |
|---|---|
| A server without the type (SQL Server 2022 or earlier, or a Managed Instance on the SQL Server 2022 update policy) | `NVARCHAR(MAX)` with `CHECK (ISJSON(col) = 1)`, which `basis ddl` writes instead of `JSON`. The JSON functions work on text since SQL Server 2016, and basis's encoding and decoding stay the same (from docs; not run here). |
| `cursor.description` does not name `json` | Types come from `sys.columns` through `describe` for destinations, and from the job's declared dtype for reads (verified here). |
| The default Arrow read drops 4-byte characters | The UTF-8 char fetch on Linux, `CAST(col AS nvarchar(max))` anywhere, or the `mssql-odbc` provider, a preview (all three verified here). |
| A `dict` or `list` does not bind | Encode to text first (verified here). |
| A `json` column cannot be a key, matched on, or compared | Key and match on scalar columns. To match on a value inside a document, use a `PERSISTED` computed column of `JSON_VALUE`, which compares and indexes. To compare whole documents in SQL, cast both to `nvarchar(max)`, where key order counts (verified here). |
| Floats in exponent form, or out of range, lose digits or are refused | Plain notation, exact up to 38 digits (verified here). A value outside that range is refused, or else written as a JSON string. |
| Decimals as JSON numbers read back lossy through polars | Write them as JSON strings (verified here). Reading others' numbers exactly needs `json.loads(..., parse_float=Decimal)`. |
| `mssql-odbc` cannot bind an all-`None` `json` parameter | Stay on `msodbcsql18` (verified here). |
| `executemany` binds a column whose longest value is ASCII as `varchar`, losing what the code page cannot hold, and raises on a value with more UTF-16 units than that one | Give every column's type with `setinputsizes`, each string as `nvarchar` sized in UTF-16 units, as basis-core does (verified after the spike). |
| `executemany` counts one row for a batch holding a string past 4,000 UTF-16 units | Send each such row as a statement of its own, and count each stage on the server, as basis-core does (verified after the spike). |
| `executemany` refuses a decimal whose text is longer than 38 characters | Bind decimals as text, as basis-core does (verified after the spike). |
| A non-UTF-8 `varchar`, or an Oracle column in a non-Unicode character set, replaces a character it cannot hold, without an error | Refuse the value before writing, by the column's code page or character set, as basis-core's `check_values` does (verified after the spike on SQL Server; unit-tested for Oracle's sets). |
| Oracle refuses a `CLOB` value over 16,000 UTF-8 bytes bound through Arrow | None yet: temporary LOBs, untried. The publish fails with ORA-01461 and nothing commits (verified after the spike). |

## Upstream

To report to microsoft/mssql-python, as verified here with 1.17.0 and `msodbcsql18` 18.6.2.2:

```python
import mssql_python, pyarrow as pa

connection = mssql_python.connect(CONNECTION_STRING, autocommit=True)
connection.cursor().execute("CREATE TABLE t (id int PRIMARY KEY, j json, v varchar(max) COLLATE Latin1_General_100_BIN2_UTF8)")
connection.cursor().execute("INSERT INTO t VALUES (1, ?, ?)", ('["\U0001f600"]', '["\U0001f600"]'))
cursor = connection.cursor()
cursor.execute("SELECT j, v FROM t")
reader = cursor.arrow_reader()
print(pa.Table.from_batches(list(reader), schema=reader.schema).to_pylist())  # [{'j': '[""]', 'v': '[""]'}]
cursor = connection.cursor()
cursor.execute("SELECT j, v FROM t")
print(tuple(cursor.fetchone()))  # ('["😀"]', '["😀"]')
```

Found by basis-core's fix, after the spike, with the same versions against SQL Server 2022: `fetchone` loses a 4-byte
character too, where it straddles the driver's chunks of 4,095 UTF-16 units.

```python
connection.cursor().execute("CREATE TABLE t2 (id int PRIMARY KEY, v varchar(max) COLLATE Latin1_General_100_BIN2_UTF8)")
connection.cursor().execute("INSERT INTO t2 VALUES (1, ?)", ("a" * 4094 + "\U0001f600b",))
cursor = connection.cursor()
cursor.execute("SELECT v FROM t2")
print(repr(cursor.fetchone()[0][4090:]))  # 'aaaab'
```

Found by basis-core's write fix, with the same versions against SQL Server 2022, in a database collated
`SQL_Latin1_General_CP1_CI_AS`. `executemany` types each column from its longest value by characters. When that value
is ASCII, it binds the column as `varchar`, and the server converts every value through the code page, without an
error. A value with more UTF-16 units than that value raises. When that value is past 4,000 UTF-16 units, the driver
runs each row by itself, and `rowcount` is the last row's. A decimal whose text passes 38 characters is refused:

```python
from decimal import Decimal

cursor = connection.cursor()
cursor.execute("CREATE TABLE t3 (id int PRIMARY KEY, v nvarchar(max), d decimal(38,10))")
cursor.executemany("INSERT INTO t3 (id, v) VALUES (?, ?)", [(1, "hello world"), (2, "日本"), (3, "Ω"), (4, "\U0001f600")])
cursor.execute("SELECT v FROM t3 ORDER BY id")
print([row[0] for row in cursor.fetchall()])  # ['hello world', '??', '?', ':-D']
cursor.executemany("INSERT INTO t3 (id, v) VALUES (?, ?)", [(5, "hello world"), (6, "\U0001f600" * 6)])
# RuntimeError: Input string exceeds allowed column size at parameter index 1
cursor.executemany("INSERT INTO t3 (id, v) VALUES (?, ?)", [(7, "x"), (8, "a" * 4001), (9, "x")])
print(cursor.rowcount)  # 1, and all 3 rows are written
cursor.executemany("INSERT INTO t3 (id, d) VALUES (?, ?)", [(10, Decimal("1234567890123456789012345678.0123456789"))])
# ProgrammingError: Driver Error: Invalid precision or scale value; DDBC Error: [Microsoft]Invalid precision value
```

## The probe

The probe reruns every check above, and needs only a connection string. `--local` changes the database's compatibility
level, so it ran here only against the throwaway container. These were the runs:

```sh
docker run -d --name spike06-mssql --memory 2560m -e ACCEPT_EULA=Y -e "MSSQL_SA_PASSWORD=$PW" -p 127.0.0.1:14336:1433 \
  mcr.microsoft.com/mssql/server:2025-latest
# then CREATE DATABASE spike06
export SPIKE06_CONNECTION_STRING="Server=tcp:127.0.0.1,14336;Database=spike06;UID=sa;PWD=$PW;Encrypt=yes;TrustServerCertificate=yes"
MSSQL_PYTHON_NATIVE_PROVIDER=msodbcsql18 uv run --script probe.py --local
MSSQL_PYTHON_NATIVE_PROVIDER=mssql-odbc uv run --script probe.py --local
```

Each run printed 166 lines, one per check, as `[provider] CHECK: ok | detail` or `[provider] CHECK: raised | error`. The
error numbers in section 3 came from the same statements, run inside `BEGIN TRY ... EXEC sp_executesql ...` to read
`ERROR_NUMBER()`.

```python
# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = ["mssql-python==1.17.0", "mssql-python-odbc==18.6.2.2", "mssql-python-rs==0.4.0", "polars==2.1.0", "pyarrow==26.0.1"]
# ///
"""Spike 0.6: does mssql-python handle SQL Server's native json type?

Run it once per native provider, since a process fixes its provider at its first connection:

    export SPIKE06_CONNECTION_STRING='Server=tcp:HOST,1433;Database=DB;UID=...;PWD=...;Encrypt=yes'
    MSSQL_PYTHON_NATIVE_PROVIDER=msodbcsql18 uv run --script probe.py
    MSSQL_PYTHON_NATIVE_PROVIDER=mssql-odbc uv run --script probe.py

It makes tables named spike06_* in the login's default schema and drops them at the end. `--local` also changes the
database's compatibility level and restores it, so pass it only against a throwaway server.
Each line is `[provider] CHECK: ok | detail` or `[provider] CHECK: raised | error`.
"""

import contextlib
import datetime as dt
import decimal
import json
import os
import sys
from collections.abc import Callable, Iterator

import mssql_python
import polars as pl
import pyarrow as pa

CONN = os.environ["SPIKE06_CONNECTION_STRING"]
LOCAL = "--local" in sys.argv
PROVIDER = os.environ.get("MSSQL_PYTHON_NATIVE_PROVIDER", "msodbcsql18")
T = "spike06_docs"
TABLES = ("spike06_docs", "spike06_bulk", "spike06_idx", "spike06_alter", "spike06_rt", "spike06_compat")


@contextlib.contextmanager
def cursor(*, autocommit: bool = True) -> Iterator[mssql_python.Cursor]:
    """A cursor on a fresh connection, so a check that breaks its connection breaks no other."""
    connection = mssql_python.connect(CONN, autocommit=autocommit)
    try:
        with contextlib.closing(connection.cursor()) as cur:
            yield cur
    finally:
        with contextlib.suppress(Exception):
            connection.close()


def short(value: object, limit: int = 300) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[:limit]}... ({len(text)} chars)"


def check(name: str, fn: Callable[[], object]) -> None:
    try:
        detail = fn()
    except Exception as error:  # noqa: BLE001 - a probe reports every failure
        print(f"[{PROVIDER}] {name}: raised | {type(error).__name__}: {short(error, 500)}", flush=True)
        return
    print(f"[{PROVIDER}] {name}: ok{' | ' + short(detail, 700) if detail is not None else ''}", flush=True)


def run(sql: str, params: object = None) -> list[tuple[object, ...]] | int:
    with cursor() as cur:
        cur.execute(sql, params) if params is not None else cur.execute(sql)
        if cur.description is None:
            return cur.rowcount
        return [tuple(row) for row in cur.fetchall()]


def one(sql: str, params: object = None) -> object:
    rows = run(sql, params)
    assert isinstance(rows, list)
    return rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else rows


def reset(table: str = T) -> None:
    run(f"DROP TABLE IF EXISTS {table}")
    run(f"CREATE TABLE {table} (id int NOT NULL PRIMARY KEY, doc json NULL)")


def put(id_: int, text: str | None, table: str = T) -> None:
    run(f"INSERT INTO {table} (id, doc) VALUES (?, ?)", (id_, text))


def mssql_many(rows: list[tuple[int, str | None]], table: str = T) -> None:
    with cursor() as cur:
        cur.executemany(f"INSERT INTO {table} (id, doc) VALUES (?, ?)", rows)


def describe_first(sql: str) -> list[tuple[object, ...]]:
    rows = run("SELECT name, system_type_name, max_length, collation_name FROM sys.dm_exec_describe_first_result_set(?, NULL, 0)", (sql,))
    assert isinstance(rows, list)
    return rows


def arrow_read(sql: str, *, utf8_char: bool = False) -> pa.Table:
    """Arrow, as basis reads. `utf8_char` fetches char data as UTF-8 bytes rather than UTF-16 (the default)."""
    with cursor() as cur:
        if utf8_char:
            cur.connection.setdecoding(mssql_python.SQL_CHAR, encoding="utf-8", ctype=mssql_python.SQL_CHAR)
        cur.execute(sql)
        reader = cur.arrow_reader(batch_size=10_000)
        return pa.Table.from_batches(list(reader), schema=reader.schema)


def versions() -> None:
    print(f"[{PROVIDER}] python {sys.version.split()[0]}, mssql-python {mssql_python.__version__}, polars {pl.__version__}, pyarrow {pa.__version__}")
    with cursor() as cur:
        cur.execute(
            "SELECT @@VERSION, SERVERPROPERTY('ProductVersion'), SERVERPROPERTY('ProductUpdateLevel'), SERVERPROPERTY('Edition'),"
            " SERVERPROPERTY('EngineEdition'), DB_NAME(), d.compatibility_level, d.collation_name"
            " FROM sys.databases AS d WHERE d.name = DB_NAME()"
        )
        print(f"[{PROVIDER}] server {short(tuple(cur.fetchone()), 600)}")
        print(f"[{PROVIDER}] provider {mssql_python.get_native_provider_info()}")
        print(f"[{PROVIDER}] driver {cur.connection.getinfo(mssql_python.SQL_DRIVER_NAME)} {cur.connection.getinfo(mssql_python.SQL_DRIVER_VER)}")


# Q1: the type exists, at which compatibility levels


def q1() -> None:
    check(
        "q1 CREATE TABLE with a json column",
        lambda: (reset(), one(f"SELECT t.name, c.max_length FROM sys.columns AS c JOIN sys.types AS t ON c.user_type_id = t.user_type_id WHERE c.object_id = OBJECT_ID('{T}') AND c.name = 'doc'"))[1],
    )
    if not LOCAL:
        return
    original = one("SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()")
    for level in (100, 130, 150, 160, 170):

        def at_level(level: int = level) -> object:
            run(f"ALTER DATABASE CURRENT SET COMPATIBILITY_LEVEL = {level}")
            run("DROP TABLE IF EXISTS spike06_compat")
            run("CREATE TABLE spike06_compat (id int PRIMARY KEY, doc json)")
            put(1, '{"a":[1,2]}', "spike06_compat")
            return one("SELECT JSON_VALUE(doc, '$.a[1]') FROM spike06_compat")

        check(f"q1 json column at compatibility level {level}", at_level)
    run(f"ALTER DATABASE CURRENT SET COMPATIBILITY_LEVEL = {original}")
    run("DROP TABLE IF EXISTS spike06_compat")


# Q2: reading


def q2() -> None:
    reset()
    put(1, '{"a":1,"b":[1,2,{"c":"x"}]}')
    check("q2 execute binding None alone into a json column", lambda: put(2, None))
    check("q2 executemany binding only None into a json column", lambda: mssql_many([(4, None)]))
    check("q2 executemany binding a str and a None into a json column", lambda: mssql_many([(5, "[]"), (6, None)]))
    run(f"DELETE FROM {T} WHERE id <> 1")
    run(f"INSERT INTO {T} (id, doc) VALUES (2, NULL)")

    def fetch() -> object:
        with cursor() as cur:
            cur.execute(f"SELECT doc FROM {T} ORDER BY id")
            rows = cur.fetchall()
            return {"values": [(type(r[0]).__name__, r[0]) for r in rows], "description": cur.description[0], "odbc_sql_type": getattr(cur, "_column_sql_types", None)}

    check("q2 fetchall of a json column", fetch)

    def arrow() -> object:
        table = arrow_read(f"SELECT doc FROM {T} ORDER BY id")
        frame = pl.from_arrow(table)
        assert isinstance(frame, pl.DataFrame)
        return {"arrow_type": str(table.schema.field("doc").type), "polars_dtype": str(frame.schema["doc"]), "values": frame["doc"].to_list()}

    check("q2 arrow_reader of a json column", arrow)
    check("q2 sp_describe_first_result_set on this connection", lambda: describe_first(f"SELECT doc FROM {T}"))
    check(
        "q2 basis describe (sys.columns)",
        lambda: run(
            "SELECT c.name, t.name, c.max_length, c.precision, c.scale, c.is_nullable, c.collation_name FROM sys.columns AS c"
            " JOIN sys.types AS t ON c.user_type_id = t.user_type_id WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id",
            (T,),
        ),
    )
    put(3, '{ "a" : 1 , "a" : 2 , "z" : [ ] , "b" : "\\/\\u00e9\\ud83d\\ude00" , "y" : 1.0 }')
    check("q2 text the server returns for a document with whitespace, a duplicate key and escapes", lambda: one(f"SELECT doc FROM {T} WHERE id = 3"))

    # Sizes around issue microsoft/mssql-python#261 (a read past 3136 characters broke the connection), and 4000/8000.
    sizes = [("ascii", n, "x") for n in (100, 3_000, 3_136, 3_137, 4_000, 4_001, 8_000, 8_001, 65_536, 1_000_000, 4_000_000)]
    sizes += [("2-byte", 3_000, "é"), ("2-byte", 5_000, "é"), ("4-byte", 2_000, "\U0001f600"), ("4-byte", 300_000, "\U0001f600")]
    for kind, n, char in sizes:
        text = '{"s":"' + char * (n - 8 if kind == "ascii" else n) + '"}'

        def sized(text: str = text) -> object:
            reset()
            put(1, text)
            with cursor() as cur:
                cur.execute(f"SELECT doc FROM {T}")
                fetched = cur.fetchone()[0]
            column = arrow_read(f"SELECT doc FROM {T}").column("doc")
            arrowed = column[0].as_py()
            utf8 = arrow_read(f"SELECT doc FROM {T}", utf8_char=True).column("doc")[0].as_py()
            cast = arrow_read(f"SELECT CAST(doc AS nvarchar(max)) AS doc FROM {T}").column("doc")[0].as_py()
            return (
                f"chars {len(text)}, utf8 bytes {len(text.encode())}; fetchone equal {fetched == text}; arrow equal {arrowed == text} ({column.type},"
                f" {len(arrowed)} chars); arrow with UTF-8 char fetch equal {utf8 == text}; arrow of CAST AS nvarchar(max) equal {cast == text}"
            )

        check(f"q2 read a {kind} document of {len(text):,} chars", sized)

    # Characters outside the Basic Multilingual Plane (4 bytes in UTF-8, a surrogate pair in UTF-16), by column type.
    def non_bmp(column_type: str, body: str) -> Callable[[], object]:
        def go() -> object:
            run("DROP TABLE IF EXISTS spike06_compat")
            run(f"CREATE TABLE spike06_compat (id int PRIMARY KEY, doc {column_type})")
            text = '["' + body + '"]'
            run("INSERT INTO spike06_compat (id, doc) VALUES (1, ?)", (text,))
            arrowed = arrow_read("SELECT doc FROM spike06_compat").column("doc")[0].as_py()
            utf8 = arrow_read("SELECT doc FROM spike06_compat", utf8_char=True).column("doc")[0].as_py()
            return f"arrow {arrowed!r} (equal {arrowed == text}); arrow with UTF-8 char fetch equal {utf8 == text}; fetchone equal {one('SELECT doc FROM spike06_compat') == text}"

        return go

    for column_type in ("json", "varchar(max) COLLATE Latin1_General_100_BIN2_UTF8", "varchar(100) COLLATE Latin1_General_100_BIN2_UTF8", "nvarchar(max)"):
        check(f"q2 arrow of one emoji in a {column_type} column", non_bmp(column_type, "\U0001f600"))
    check("q2 arrow of 'a😀é日' x 3 in a json column", non_bmp("json", "a\U0001f600é日" * 3))
    check("q2 arrow of a CJK Extension B character in a json column", non_bmp("json", "\U00020000"))


# Q3: writing


def q3() -> None:
    reset()
    check("q3 execute, qmark str", lambda: (put(1, '{"a":1}'), one(f"SELECT doc FROM {T} WHERE id = 1"))[1])
    check("q3 execute, pyformat str", lambda: (run(f"INSERT INTO {T} (id, doc) VALUES (%(id)s, %(doc)s)", {"id": 2, "doc": '{"b":2}'}), one(f"SELECT doc FROM {T} WHERE id = 2"))[1])

    def many(rows: list[tuple[int, str | None]]) -> Callable[[], object]:
        def go() -> object:
            reset()
            with cursor() as cur:
                cur.executemany(f"INSERT INTO {T} (id, doc) VALUES (?, ?)", rows)
            back = run(f"SELECT id, doc FROM {T} ORDER BY id")
            assert isinstance(back, list)
            return f"{len(back)} rows; all equal {[r[1] for r in back] == [r[1] for r in rows]}"

        return go

    check("q3 executemany, small strs and a NULL", many([(1, '{"a":1}'), (2, None), (3, "[1,2,3]")]))
    big = '{"s":"' + "x" * 100_000 + '"}'
    check("q3 executemany, mixed sizes (7 B, 5 KB, 100 KB, 4-byte chars)", many([(1, '{"a":1}'), (2, '{"s":"' + "y" * 5_000 + '"}'), (3, big), (4, '{"e":"' + "\U0001f600" * 3_000 + '"}')]))

    reset()
    check("q3 bind a dict directly", lambda: (put(10, {"a": 1}), one(f"SELECT doc FROM {T} WHERE id = 10"))[1])  # type: ignore[arg-type]
    check("q3 bind a list directly", lambda: (put(11, [1, 2]), one(f"SELECT doc FROM {T} WHERE id = 11"))[1])  # type: ignore[arg-type]

    for label, text in [
        ("truncated JSON", '{"a":'),
        ("a bare scalar", "1"),
        ("a bare string", '"x"'),
        ("the literal null", "null"),
        ("an empty string", ""),
        ("NaN, as json.dumps writes it", json.dumps({"x": float("nan")})),
        ("a trailing comma", '{"a":1,}'),
        ("single quotes", "{'a':1}"),
    ]:
        check(f"q3 server validation: {label}", lambda text=text: (put(20, text), run(f"DELETE FROM {T} WHERE id = 20"))[0])

    def bad_row_in_batch() -> object:
        reset()
        connection = mssql_python.connect(CONN, autocommit=False)
        try:
            cur = connection.cursor()
            try:
                cur.executemany(f"INSERT INTO {T} (id, doc) VALUES (?, ?)", [(1, '{"a":1}'), (2, '{"a":'), (3, '{"c":3}')])
                outcome = "no error"
            except mssql_python.Error as error:
                outcome = f"{type(error).__name__}: {short(error)}"
            cur.execute(f"SELECT COUNT(*) FROM {T}")
            landed = cur.fetchone()[0]
            connection.rollback()
            return f"{outcome}; rows in the transaction afterwards: {landed}"
        finally:
            connection.close()

    check("q3 executemany with one invalid row among three, in a transaction", bad_row_in_batch)

    # basis's write path today (mssql.py on main): stage with SELECT TOP 0 ... INTO, executemany, UPDATE then INSERT.
    def basis_upsert() -> object:
        reset()
        put(1, '{"v":"old"}')
        connection = mssql_python.connect(CONN, autocommit=False)
        try:
            cur = connection.cursor()
            cur.execute(f"SELECT TOP 0 id, doc INTO #etl_stage FROM {T}")
            cur.execute(
                "SELECT t.name FROM tempdb.sys.columns AS c JOIN tempdb.sys.types AS t ON c.user_type_id = t.user_type_id WHERE c.object_id = OBJECT_ID('tempdb..#etl_stage') AND c.name = 'doc'"
            )
            stage_type = cur.fetchone()[0]
            cur.executemany("INSERT INTO #etl_stage (id, doc) VALUES (?, ?)", [(1, '{"v":"new"}'), (2, '{"v":"added","n":[1,2]}')])
            cur.execute(f"UPDATE t SET t.doc = s.doc FROM {T} AS t INNER JOIN #etl_stage AS s ON t.id = s.id")
            updated = cur.rowcount
            cur.execute(f"INSERT INTO {T} (id, doc) SELECT s.id, s.doc FROM #etl_stage AS s WHERE NOT EXISTS (SELECT 1 FROM {T} AS t WITH (UPDLOCK, HOLDLOCK) WHERE t.id = s.id)")
            inserted = cur.rowcount
            cur.execute("DROP TABLE #etl_stage")
            connection.commit()
        finally:
            connection.close()
        return f"stage column type {stage_type}; updated {updated}, inserted {inserted}; table now {run(f'SELECT id, doc FROM {T} ORDER BY id')}"

    check("q3 basis's upsert path (SELECT TOP 0 INTO #stage, executemany, UPDATE, INSERT WHERE NOT EXISTS)", basis_upsert)

    def match_on_json() -> object:
        with cursor() as cur:
            cur.execute(f"SELECT TOP 0 doc INTO #etl_match FROM {T}")
            cur.execute("INSERT INTO #etl_match (doc) VALUES (?)", ('{"v":"new"}',))
            cur.execute(f"DELETE t FROM {T} AS t WHERE EXISTS (SELECT 1 FROM #etl_match AS m WHERE t.doc = m.doc)")
            return cur.rowcount

    check("q3 basis's delete by values, matching on a json column", match_on_json)
    check("q3 a json column as a key (CREATE TABLE ... PRIMARY KEY on json)", lambda: run("CREATE TABLE spike06_compat (doc json NOT NULL PRIMARY KEY)"))

    def bulk(rows: list[tuple[int, object]]) -> Callable[[], object]:
        def go() -> object:
            reset("spike06_bulk")
            with cursor() as cur:
                result = cur.bulkcopy("spike06_bulk", rows, timeout=60)
            return f"{result.get('rows_copied')} copied; back {run('SELECT id, doc FROM spike06_bulk ORDER BY id')}"

        return go

    check("q3 bulkcopy, str values (basis does not use bulk copy)", bulk([(1, '{"a":1}'), (2, None), (3, '{"s":"' + "x" * 10_000 + '"}')]))
    check("q3 bulkcopy, dict and list values", bulk([(1, {"a": 1, "d": "é"}), (2, [1, 2])]))
    check("q3 bulkcopy, invalid JSON", bulk([(1, '{"a":')]))

    def bulk_arrow() -> object:
        reset("spike06_bulk")
        with cursor() as cur:
            result = cur.bulkcopy_arrow("spike06_bulk", pa.table({"id": pa.array([1, 2], pa.int32()), "doc": pa.array(['{"a":1}', None], pa.string())}))
        return f"{result.get('rows_copied')} copied; back {run('SELECT id, doc FROM spike06_bulk ORDER BY id')}"

    check("q3 bulkcopy_arrow, a string column", bulk_arrow)


# Q4: functions, indexes, limits


def q4() -> None:
    reset()
    put(1, '{"a":1,"b":{"c":[1,2,3]},"s":"x","n":12.50}')
    put(2, '{"a":2,"b":{"c":[]},"s":"y"}')
    queries = {
        "JSON_VALUE": f"SELECT JSON_VALUE(doc, '$.s') FROM {T} WHERE id = 1",
        "JSON_VALUE RETURNING int": f"SELECT JSON_VALUE(doc, '$.a' RETURNING int) FROM {T} WHERE id = 1",
        "JSON_QUERY": f"SELECT JSON_QUERY(doc, '$.b') FROM {T} WHERE id = 1",
        "OPENJSON": f"SELECT [key], [value], [type] FROM {T} CROSS APPLY OPENJSON(doc) WHERE id = 1",
        "OPENJSON WITH, AS JSON into nvarchar(max)": f"SELECT j.a, j.b FROM {T} CROSS APPLY OPENJSON(doc) WITH (a int '$.a', b nvarchar(max) '$.b' AS JSON) AS j WHERE id = 1",
        "OPENJSON WITH, AS JSON into json": f"SELECT j.a, j.b FROM {T} CROSS APPLY OPENJSON(doc) WITH (a int '$.a', b json '$.b' AS JSON) AS j WHERE id = 1",
        "JSON_OBJECTAGG": f"SELECT JSON_OBJECTAGG(CAST(id AS varchar(10)) : doc) FROM {T}",
        "JSON_ARRAYAGG": f"SELECT JSON_ARRAYAGG(doc ORDER BY id) FROM {T}",
        "JSON_OBJECT with a json value": f"SELECT JSON_OBJECT('k': doc) FROM {T} WHERE id = 1",
        "JSON_MODIFY": f"SELECT JSON_MODIFY(doc, '$.a', 5) FROM {T} WHERE id = 1",
        "ISJSON": f"SELECT ISJSON(doc) FROM {T} WHERE id = 1",
        "JSON_PATH_EXISTS": f"SELECT JSON_PATH_EXISTS(doc, '$.b.c') FROM {T} WHERE id = 1",
        "JSON_CONTAINS": f"SELECT JSON_CONTAINS(doc, 1, '$.a') FROM {T} WHERE id = 1",
        "CAST AS nvarchar(max)": f"SELECT CAST(doc AS nvarchar(max)) FROM {T} WHERE id = 1",
        "CAST AS varchar(10), too short": f"SELECT CAST(doc AS varchar(10)) FROM {T} WHERE id = 1",
        "DATALENGTH": f"SELECT DATALENGTH(doc), LEN(CAST(doc AS nvarchar(max))) FROM {T} WHERE id = 1",
        "equality, doc = doc": f"SELECT COUNT(*) FROM {T} WHERE doc = doc",
        "GROUP BY doc": f"SELECT COUNT(*) FROM {T} GROUP BY doc",
        "ORDER BY doc": f"SELECT id FROM {T} ORDER BY doc",
        "SELECT DISTINCT doc": f"SELECT DISTINCT doc FROM {T}",
        "UNION": f"SELECT doc FROM {T} UNION SELECT doc FROM {T}",
        "UNION ALL": f"SELECT doc FROM {T} UNION ALL SELECT doc FROM {T}",
        "equality through CAST to nvarchar(max)": f"SELECT COUNT(*) FROM {T} AS a JOIN {T} AS b ON CAST(a.doc AS nvarchar(max)) = CAST(b.doc AS nvarchar(max))",
    }
    for label, sql in queries.items():
        check(f"q4 {label}", lambda sql=sql: run(sql))
    for label in ("JSON_QUERY", "JSON_OBJECTAGG", "JSON_ARRAYAGG", "JSON_MODIFY", "JSON_OBJECT with a json value", "CAST AS nvarchar(max)"):
        check(f"q4 result type of {label}", lambda label=label: describe_first(queries[label])[0][1:])
    check("q4 UPDATE ... SET doc.modify", lambda: (run(f"UPDATE {T} SET doc.modify('$.a', 14859) WHERE id = 1"), one(f"SELECT doc FROM {T} WHERE id = 1"))[1])
    check("q4 UPDATE ... SET doc = JSON_MODIFY(doc, ...)", lambda: (run(f"UPDATE {T} SET doc = JSON_MODIFY(doc, '$.s', 'z') WHERE id = 1"), one(f"SELECT doc FROM {T} WHERE id = 1"))[1])

    def idx(ddl: str) -> Callable[[], object]:
        def go() -> object:
            reset("spike06_idx")
            put(1, '{"a":1,"b":{"c":2}}', "spike06_idx")
            run(ddl)
            put(2, '{"a":2}', "spike06_idx")
            return one("SELECT COUNT(*) FROM spike06_idx WHERE JSON_VALUE(doc, '$.a') = '1'")

        return go

    check("q4 index with doc as a key column", idx("CREATE INDEX ix_doc ON spike06_idx (doc)"))
    check("q4 index with doc INCLUDEd", idx("CREATE INDEX ix_inc ON spike06_idx (id) INCLUDE (doc)"))
    check("q4 filtered index WHERE doc IS NOT NULL", idx("CREATE INDEX ix_f ON spike06_idx (id) WHERE doc IS NOT NULL"))
    check("q4 CREATE JSON INDEX ... FOR ('$.a')", idx("CREATE JSON INDEX jx ON spike06_idx (doc) FOR ('$.a')"))
    check("q4 CREATE JSON INDEX on the whole document", idx("CREATE JSON INDEX jx ON spike06_idx (doc)"))
    check("q4 persisted computed column on JSON_VALUE, indexed", idx("ALTER TABLE spike06_idx ADD a AS CAST(JSON_VALUE(doc, '$.a') AS int) PERSISTED; CREATE INDEX ix_a ON spike06_idx (a)"))
    check(
        "q4 CHECK (JSON_PATH_EXISTS(...)) and DEFAULT on a json column",
        lambda: (
            run("DROP TABLE IF EXISTS spike06_idx"),
            run("CREATE TABLE spike06_idx (id int PRIMARY KEY, doc json NOT NULL DEFAULT ('{}') CHECK (JSON_PATH_EXISTS(doc, '$.a') = 1))"),
            run("INSERT INTO spike06_idx (id, doc) VALUES (1, '{\"a\":1}')"),
        )[2],
    )
    check("q4 the CHECK constraint refuses a document without $.a", lambda: run("INSERT INTO spike06_idx (id, doc) VALUES (2, '{\"b\":1}')"))

    # Size limits from the json type's page: 128 nesting levels, 7,998-byte keys, 65,535 array elements and object
    # properties, 32,000 unique keys.
    def write(text: str) -> Callable[[], object]:
        def go() -> object:
            reset()
            put(1, text)
            return f"stored {len(text):,} chars; read back equal {one(f'SELECT doc FROM {T}') == text}"

        return go

    for depth in (127, 128, 129):
        check(f"q4 arrays nested {depth} deep", write("[" * depth + "]" * depth))
    for depth in (128, 129):
        check(f"q4 objects nested {depth} deep", write('{"k":' * (depth - 1) + "{}" + "}" * (depth - 1)))
    for length in (7_998, 7_999):
        check(f"q4 a key of {length:,} bytes", write('{"' + "k" * length + '":1}'))
    for count in (65_535, 65_536):
        check(f"q4 an array of {count:,} elements", write("[" + ",".join("1" for _ in range(count)) + "]"))
    for count in (32_000, 32_001):
        check(f"q4 an object of {count:,} unique keys", write("{" + ",".join(f'"k{i}":1' for i in range(count)) + "}"))
    check("q4 32,001 unique keys spread across nested objects", write("[" + ",".join("{" + ",".join(f'"k{j}_{i}":1' for i in range(1_000)) + "}" for j in range(33)) + "]"))

    def alter(ddl: str, rows: list[str]) -> Callable[[], object]:
        def go() -> object:
            run("DROP TABLE IF EXISTS spike06_alter")
            run("CREATE TABLE spike06_alter (id int IDENTITY PRIMARY KEY, doc nvarchar(max) NULL)")
            for row in rows:
                run("INSERT INTO spike06_alter (doc) VALUES (?)", (row,))
            run(ddl)
            return one("SELECT t.name FROM sys.columns AS c JOIN sys.types AS t ON c.user_type_id = t.user_type_id WHERE c.object_id = OBJECT_ID('spike06_alter') AND c.name = 'doc'")

        return go

    check("q4 ALTER COLUMN nvarchar(max) -> json, valid rows", alter("ALTER TABLE spike06_alter ALTER COLUMN doc json NULL", ['{"a":1}', "[]"]))
    check("q4 ALTER COLUMN nvarchar(max) -> json, one invalid row", alter("ALTER TABLE spike06_alter ALTER COLUMN doc json NULL", ['{"a":1}', "not json"]))
    check("q4 ALTER COLUMN json -> nvarchar(max)", alter("ALTER TABLE spike06_alter ALTER COLUMN doc json NULL; ALTER TABLE spike06_alter ALTER COLUMN doc nvarchar(max) NULL", ['{"a":1}']))
    check(
        "q4 ALTER TABLE ADD a json column",
        lambda: (
            alter("ALTER TABLE spike06_alter ADD extra json NULL", [])(),
            one("SELECT t.name FROM sys.columns AS c JOIN sys.types AS t ON c.user_type_id = t.user_type_id WHERE c.object_id = OBJECT_ID('spike06_alter') AND c.name = 'extra'"),
        )[1],
    )


# Q5: polars round trip


def q5() -> None:
    # Each JSON number on its own: how the json type stores it, and what comes back.
    numbers = {
        "decimal, 30 digits": "12345678901234567890.1234567890",
        "decimal, 38 digits": "1234567890123456789012345678.9012345678",
        "decimal, 39 digits": "12345678901234567890123456789.0123456789",
        "trailing zero 100.10": "100.10",
        "integer 2^53+1": "9007199254740993",
        "int64 max": "9223372036854775807",
        "int64 min": "-9223372036854775808",
        "integer, 38 digits": "12345678901234567890123456789012345678",
        "integer, 39 digits": "123456789012345678901234567890123456789",
        "0.1": "0.1",
        "0.30000000000000004 (17 significant digits)": "0.30000000000000004",
        "1.0": "1.0",
        "-0.0": "-0.0",
        "1.5E+3": "1.5E+3",
        "1.2345678901234567e+20 (Python's repr of a float >= 1e16)": "1.2345678901234567e+20",
        "1e27": "1e27",
        "9.99e27": "9.99e27",
        "1e28": "1e28",
        "1e38": "1e38",
        "9.99e37": "9.99e37",
        "1e39": "1e39",
        "1.7976931348623157e308 (float64 max)": "1.7976931348623157e308",
        "1.2345678901234567e-05 (Python's repr of a float < 1e-4)": "1.2345678901234567e-05",
        "0.000012345678901234567 (the same, written plainly)": "0.000012345678901234567",
        "0.0000000000000000000000000000000000001 (37 places, plain)": "0.0000000000000000000000000000000000001",
        "0.00000000000000000000000000000000000001 (38 places, plain)": "0.00000000000000000000000000000000000001",
        "plain, 40 digits, 39 of them after the point": "1.123456789012345678901234567890123456789",
        "plain, 1.2345678901234567e-30 written out (46 places)": "0.0000000000000000000000000000012345678901234567",
        "1e-10": "1e-10",
        "1e-38": "1e-38",
        "1e-39": "1e-39",
        "5e-324 (smallest float64)": "5e-324",
    }
    for label, literal in numbers.items():

        def number(literal: str = literal) -> object:
            reset()
            put(1, '{"n":' + literal + "}")
            back = one(f"SELECT doc FROM {T} WHERE id = 1")
            assert isinstance(back, str)
            value = json.loads(back, parse_float=decimal.Decimal)["n"]
            same = decimal.Decimal(str(value)) == decimal.Decimal(literal)
            return f"back {back}; numerically equal {same}; JSON_VALUE {one(f"SELECT JSON_VALUE(doc, '$.n') FROM {T} WHERE id = 1")!r}"

        check(f"q5 JSON number {label}", number)

    text_cases = {"unicode": 'Zoë 日本語 😀 עברית é "q" \\ / \t \n   \u001f', "NUL": "a\u0000b", "lone surrogate": "\ud800"}
    for label, value in text_cases.items():
        for ascii_only in (True, False):

            def text_case(value: str = value, ascii_only: bool = ascii_only) -> object:
                reset()
                sent = json.dumps({"s": value}, ensure_ascii=ascii_only)
                put(1, sent)
                back = one(f"SELECT doc FROM {T} WHERE id = 1")
                assert isinstance(back, str)
                return f"text equal {back == sent}; value equal {json.loads(back)['s'] == value}; back {back!r}"

            check(f"q5 string value with {label}, json.dumps ensure_ascii={ascii_only}", text_case)

    schema = {
        "id": pl.Int32,
        "doc": pl.Struct(
            {
                "price": pl.Decimal(38, 10),
                "par": pl.Decimal(20, 2),
                "qty": pl.Int64,
                "ratio": pl.Float64,
                "small": pl.Float64,
                "trade_date": pl.Date,
                "ts": pl.Datetime("us"),
                "ts_utc": pl.Datetime("us", "UTC"),
                "flag": pl.Boolean,
                "name": pl.String,
                "nested": pl.Struct({"level": pl.Int32, "inner": pl.Struct({"xs": pl.List(pl.Int64)})}),
                "legs": pl.List(pl.Struct({"cusip": pl.String, "weight": pl.Float64})),
                "missing": pl.String,
            }
        ),
        "tags": pl.List(pl.String),
    }
    frame = pl.DataFrame(
        {
            "id": [1, 2, 3],
            "doc": [
                {
                    "price": decimal.Decimal("12345678901234567890.1234567890"),
                    "par": decimal.Decimal("100.10"),
                    "qty": 9_007_199_254_740_993,
                    "ratio": 0.1,
                    "small": 1.2345678901234567e-05,
                    "trade_date": dt.date(2026, 11, 7),
                    "ts": dt.datetime(2026, 11, 7, 13, 45, 12, 123456),
                    "ts_utc": dt.datetime(2026, 11, 7, 13, 45, 12, 123456, tzinfo=dt.UTC),
                    "flag": True,
                    "name": 'Zoë 日本語 😀 עברית é "q" \\',
                    "nested": {"level": 1, "inner": {"xs": [1, 2, 3]}},
                    "legs": [{"cusip": "00000AA00", "weight": 0.25}, {"cusip": "00000BB00", "weight": 0.75}],
                    "missing": None,
                },
                {
                    "price": decimal.Decimal("-0.0000000001"),
                    "par": decimal.Decimal("0.00"),
                    "qty": 9_223_372_036_854_775_807,
                    "ratio": 12345.678,
                    "small": 1.2345678901234567e-30,
                    "trade_date": dt.date(1900, 1, 1),
                    "ts": dt.datetime(9999, 12, 31, 23, 59, 59, 999999),
                    "ts_utc": dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
                    "flag": False,
                    "name": "",
                    "nested": {"level": 2, "inner": {"xs": []}},
                    "legs": [],
                    "missing": None,
                },
                None,
            ],
            "tags": [["a", "😀"], [], None],
        },
        schema=schema,
    )

    def encoded_polars(frame: pl.DataFrame) -> pl.DataFrame:
        # polars has json_encode on structs only: a list column goes in a struct of one field, which is then cut away.
        # A null struct encodes as the text "null", which the json type refuses, so nulls stay SQL NULL.
        return frame.with_columns(
            pl.when(pl.col("doc").is_not_null()).then(pl.col("doc").struct.json_encode()).alias("doc"),
            pl.when(pl.col("tags").is_not_null()).then(pl.struct("tags").struct.json_encode().str.slice(8).str.head(-1)).alias("tags"),
        )

    def encoded_python(frame: pl.DataFrame) -> pl.DataFrame:
        def default(value: object) -> object:
            if isinstance(value, decimal.Decimal):
                return str(value)
            if isinstance(value, dt.date | dt.datetime):
                return value.isoformat()
            raise TypeError(type(value).__name__)

        def dumps(value: object) -> str | None:
            return None if value is None else json.dumps(value, default=default, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

        return frame.with_columns(pl.Series(name, [dumps(value) for value in frame[name].to_list()], dtype=pl.String) for name in ("doc", "tags"))

    variants = [
        ("polars struct.json_encode", encoded_polars, False),
        ("polars struct.json_encode, read with UTF-8 char fetch", encoded_polars, True),
        ("json.dumps(default=str-ish)", encoded_python, False),
    ]
    for label, encode, utf8_char in variants:

        def round_trip(encode: Callable[[pl.DataFrame], pl.DataFrame] = encode, utf8_char: bool = utf8_char) -> object:
            run("DROP TABLE IF EXISTS spike06_rt")
            run("CREATE TABLE spike06_rt (id int PRIMARY KEY, doc json NULL, tags json NULL)")
            sent = encode(frame)
            with cursor() as cur:
                cur.executemany("INSERT INTO spike06_rt (id, doc, tags) VALUES (?, ?, ?)", sent.rows())
            back = pl.from_arrow(arrow_read("SELECT id, doc, tags FROM spike06_rt ORDER BY id", utf8_char=utf8_char))
            assert isinstance(back, pl.DataFrame)
            texts_equal = back["doc"].to_list() == sent["doc"].to_list() and back["tags"].to_list() == sent["tags"].to_list()
            decoded = back.with_columns(pl.col("id").cast(pl.Int32), pl.col("doc").str.json_decode(dtype=schema["doc"]), pl.col("tags").str.json_decode(dtype=schema["tags"]))
            diffs: list[tuple[object, ...]] = []
            for row, (want, got) in enumerate(zip(frame["doc"].to_list(), decoded["doc"].to_list(), strict=True)):
                if want is None or got is None:
                    if want != got:
                        diffs.append(("doc", row, want, got))
                    continue
                diffs.extend(("doc", row, field, want[field], got.get(field)) for field in want if want[field] != got.get(field))
            diffs.extend(("tags", row, want, got) for row, (want, got) in enumerate(zip(frame["tags"].to_list(), decoded["tags"].to_list(), strict=True)) if want != got)
            return f"text identical {texts_equal}; frame equal {decoded.equals(frame)}; differences {diffs}; sample sent {sent['doc'][0]!r}; back {back['doc'][0]!r}"

        check(f"q5 polars round trip through a json column, {label}", round_trip)

    def deep_lists() -> object:
        depth = 60
        dtype: pl.DataType = pl.Int64()
        value: object = 1
        for _ in range(depth):
            dtype, value = pl.List(dtype), [value]
        nested = pl.DataFrame({"v": [value]}, schema={"v": dtype})
        text = nested.select(pl.struct("v").struct.json_encode()).item()
        reset()
        put(1, text)
        back = pl.DataFrame({"t": [one(f"SELECT doc FROM {T}")]}).select(pl.col("t").str.json_decode(dtype=pl.Struct({"v": dtype}))).unnest("t")
        return f"{depth} nested lists (+1 object): equal {back.equals(nested)}"

    check("q5 nested polars lists round trip", deep_lists)
    check(
        "q5 polars str.json_decode of a decimal written as a JSON number, and as a JSON string",
        lambda: (
            pl.Series(['{"n":12345678901234567890.1234567890,"s":"12345678901234567890.1234567890"}']).str.json_decode(dtype=pl.Struct({"n": pl.Decimal(38, 10), "s": pl.Decimal(38, 10)})).to_list()
        ),
    )
    check(
        "q5 polars struct.json_encode of floats (exponent or plain)",
        lambda: pl.DataFrame({"s": [{"a": 1.2345678901234567e-07, "b": 1e-30, "c": 1.5e20, "d": 0.000012345678901234568}]}).select(pl.col("s").struct.json_encode()).item(),
    )
    check("q5 NaN and inf through polars struct.json_encode", lambda: pl.DataFrame({"s": [{"f": float("nan"), "g": float("inf")}]}).select(pl.col("s").struct.json_encode()).item())


def cleanup() -> None:
    for table in TABLES:
        with contextlib.suppress(Exception):
            run(f"DROP TABLE IF EXISTS {table}")


if __name__ == "__main__":
    versions()
    try:
        for part in (q1, q2, q3, q4, q5):
            check(f"{part.__name__} (setup)", part)
    finally:
        cleanup()
```

## (work): to run at the firm

A short check at the port, which blocks nothing above.

1. Against the database jobs will publish to, in SSMS or `sqlcmd`, in dev or QA:

   ```sql
   SELECT @@VERSION AS version, SERVERPROPERTY('ProductVersion') AS product_version,
          SERVERPROPERTY('EngineEdition') AS engine_edition, DB_NAME() AS db, compatibility_level
   FROM sys.databases WHERE name = DB_NAME();

   SELECT s.name AS schema_name, o.name AS table_name, c.name AS column_name
   FROM sys.columns AS c
   JOIN sys.types AS t ON c.user_type_id = t.user_type_id
   JOIN sys.objects AS o ON c.object_id = o.object_id
   JOIN sys.schemas AS s ON o.schema_id = s.schema_id
   WHERE t.name = 'json';

   CREATE TABLE dbo.spike06_check (id int NOT NULL PRIMARY KEY, doc json NULL);
   INSERT INTO dbo.spike06_check (id, doc) VALUES (1, N'{"price":"100.10","qty":5,"tags":["a","😀"]}');
   SELECT doc, JSON_VALUE(doc, '$.qty') AS qty FROM dbo.spike06_check;
   DROP TABLE dbo.spike06_check;
   ```

   For a Managed Instance (`engine_edition` 8), also note its update policy in the Azure portal.
2. For one table the Mongo migration has filled, run `SELECT TOP 2 CAST(col AS nvarchar(max)) FROM schema.table;`, with
   values redacted, to see how decimals, dates and ids are written: as JSON numbers or strings, or in wrappers such as
   `{"$date": ...}` and `{"$numberDecimal": ...}`.
3. Run the probe in WSL and on a Windows desktop, default provider, without `--local`, as a login that may create and
   drop tables in its default schema:

   ```sh
   export SPIKE06_CONNECTION_STRING='Server=...;Database=...;Trusted_Connection=yes;Encrypt=yes'
   uv run --script probe.py > probe-wsl.txt 2>&1
   uv run --no-project python -c "import locale; print(locale.getencoding())" >> probe-wsl.txt
   ```

   ```powershell
   $env:SPIKE06_CONNECTION_STRING = 'Server=...;Database=...;Trusted_Connection=yes;Encrypt=yes'
   uv run --script probe.py > probe-windows.txt 2>&1
   uv run --no-project python -c "import locale; print(locale.getencoding())" >> probe-windows.txt
   ```

Send back:

- the version row, and the `CREATE TABLE`'s error if it failed;
- the list of `json` columns;
- the redacted shape of two migrated documents;
- both probe outputs, each ending with its locale's encoding. The lines that matter most are each `q2 ... arrow` line,
  whose "arrow with UTF-8 char fetch equal" says whether that fix holds on Windows, and any `raised` line that is not
  in this result. basis-core sets the UTF-8 char fetch in WSL, where the encoding is UTF-8, and keeps the driver's
  default on Windows, unverified, until these lines show it exact there (`fetches_char_as_utf8`).

A note, not a step: macOS keeps the driver's default too, unverified, as developers there are rare. The probe, run on a
Mac with its `locale.getencoding()`, would say whether `fetches_char_as_utf8` can take macOS as it takes Linux.
