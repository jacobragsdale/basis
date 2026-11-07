-- SQL Server. Sector caps in force on the business date for a batch of portfolios.
SELECT m.account_id AS portfolio_id,
       m.mandate_id,
       m.sector,
       m.max_weight
FROM   account_mandates m
WHERE  m.account_id IN (SELECT id FROM OPENJSON(%(portfolios)s) WITH (id VARCHAR(64) '$'))
  AND  m.effective_date <= %(as_of_date)s
  AND  (m.expiry_date IS NULL OR m.expiry_date > %(as_of_date)s)
