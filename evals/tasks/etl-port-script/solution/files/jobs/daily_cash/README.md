# Daily cash

Each account's net cash movement per currency and value date, ported from the nightly script `daily_cash.py`.

| | |
|---|---|
| Reads | `movements`, the interval's cash movements, from `ops` |
| Writes | `dbo.daily_cash`, replacing the interval's value dates |
| Params | none |
| Checks | every counted movement's currency is three upper-case letters |
