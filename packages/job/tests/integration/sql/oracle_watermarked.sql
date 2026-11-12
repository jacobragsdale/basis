SELECT day, security_id, price, note, booked_at
FROM etl_it_source
WHERE booked_at > :watermark
