SELECT movement_id, account_id, currency, amount, value_date, status
FROM dbo.cash_movements
WHERE value_date >= %(interval_start)s AND value_date < %(interval_end)s
