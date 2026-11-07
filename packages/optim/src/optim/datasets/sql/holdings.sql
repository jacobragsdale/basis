-- SQL Server, which calls a portfolio an account. Working positions, cash included, for a batch of portfolios: the
-- start of the business day plus what has traded and settled into the books since. %(portfolios)s arrives as a JSON
-- array of portfolio ids; typed, since an untyped OPENJSON value is NVARCHAR, which turns an index seek on a VARCHAR
-- key into a table scan.
SELECT p.account_id                   AS portfolio_id,
       p.security_id,
       SUM(p.quantity)                AS quantity
FROM   positions p
WHERE  p.account_id IN (SELECT id FROM OPENJSON(%(portfolios)s) WITH (id VARCHAR(64) '$'))
  AND  p.as_of_date = %(as_of_date)s
GROUP  BY p.account_id, p.security_id
HAVING SUM(p.quantity) <> 0  -- cash can be overdrawn: a negative balance is cash a decision must raise
