SELECT account, par, as_of
FROM positions
WHERE as_of = %(as_of)s
