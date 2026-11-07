-- The interval's trades. SQL Server binds the run's interval as %(interval_start)s and %(interval_end)s, naive UTC.
SELECT trade_id, account_id, security_id, trade_date, side, quantity, price, booked_at
FROM dbo.trades
WHERE trade_date >= %(interval_start)s AND trade_date < %(interval_end)s
