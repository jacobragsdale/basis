# Daily positions

Each account's net quantity and gross notional in each security, per trade date, from the day's trades; and each
account's latest trade date and booking time.

| | |
|---|---|
| Reads | `trades` (the interval's trades) and `accounts`, from `source` |
| Writes | `daily_positions`, replacing the interval's trade dates; `account_activity`, upserted by account |
| Params | `include_closed_accounts`: whether trades in closed accounts count |
| Checks | every trade's account is listed; sides are BUY or SELL; no position falls after the interval |
| Limits | a trade booked late, for a trade date outside the run's interval, is counted only by a rerun of that date |
