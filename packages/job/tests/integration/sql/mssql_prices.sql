SELECT day, security_id, price, note, booked_at
FROM etl_it_source
WHERE day >= %(interval_start)s AND day < %(interval_end)s
