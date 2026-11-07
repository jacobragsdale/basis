-- SQL Server. The portfolios the model manages on the business date, with each client's own rating floor.
SELECT a.account_id AS portfolio_id,
       a.min_rating
FROM   accounts a
WHERE  a.strategy = 'taxable_income'
  AND  a.open_date <= %(as_of_date)s
  AND  (a.close_date IS NULL OR a.close_date > %(as_of_date)s)
