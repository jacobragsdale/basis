The `positions` job should also write each account's daily totals: for each trade date and account, the number of trades and their gross notional (quantity times price, summed and rounded to 2 decimal places), counting the same trades the positions count. Write them to the `account_daily_totals` table of the `warehouse` database, upserted by trade date and account. Call the output `account_totals`, with the columns `trade_date`, `account_id`, `trades` and `gross_notional`.

When you're done, the repo's checks should pass.
