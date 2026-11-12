SELECT day, security_id, price, note, booked_at FROM dbo.prices WHERE day >= %(interval_start)s AND day < %(interval_end)s
