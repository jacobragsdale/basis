-- SQL Server. Trading controls in force on the business date for a batch of portfolios. A null security_id controls
-- every security in the portfolio.
SELECT r.account_id                   AS portfolio_id,
       r.restriction_id               AS control_id,
       r.security_id,
       r.action,
       r.reason
FROM   account_restrictions r
WHERE  r.account_id IN (SELECT id FROM OPENJSON(%(portfolios)s) WITH (id VARCHAR(64) '$'))
  AND  r.effective_date <= %(as_of_date)s
  AND  (r.expiry_date IS NULL OR r.expiry_date > %(as_of_date)s)
