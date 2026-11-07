-- Oracle. Reference data and prices for every security a portfolio can hold or buy, cash included.
SELECT s.security_id,
       s.cusip,
       s.isin,
       s.asset_class,
       s.currency,
       s.issuer,
       s.sector,
       s.rating,
       s.maturity_date                    AS maturity,
       a.clean_price                      AS price,
       a.accrued,
       s.price_per,
       -- INCREMENT is reserved in Oracle, so the alias is quoted
       s.min_increment                    AS "INCREMENT",
       s.min_piece,
       s.min_remaining
FROM   securities s
JOIN   analytics a ON a.security_id = s.security_id AND a.as_of = :as_of_date
