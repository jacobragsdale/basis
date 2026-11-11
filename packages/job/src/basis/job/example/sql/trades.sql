-- The interval's trades. SQL Server binds the run's interval as %(interval_start)s and %(interval_end)s, naive UTC,
-- which a date compares with as its midnight. To filter booked_at, whose naive times are New York's, instead give the
-- Query zone="America/New_York", so the binds are New York's times too.
SELECT trade_id, account_id, security_id, trade_date, side, quantity, price, booked_at
FROM dbo.trades
WHERE trade_date >= %(interval_start)s AND trade_date < %(interval_end)s
