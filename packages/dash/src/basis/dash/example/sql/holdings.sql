-- Each holding of each book at the close of the day asked for, priced as the warehouse priced it then and at the
-- close before, with the Treasury curve's move at its tenor between the two.
select
    h.book,
    h.cusip,
    s.issuer,
    s.sector,
    s.rating,
    s.coupon,
    s.maturity,
    h.par,
    p.price,
    q.price as prior_price,
    p.ytw,
    q.ytw as prior_ytw,
    p.curve_change,
    p.duration,
    p.oas
from holdings as h
join securities as s on s.cusip = h.cusip
join prices as p on p.cusip = h.cusip and p.as_of = h.as_of
join prices as q on q.cusip = h.cusip and q.as_of = p.prior_as_of
where h.as_of = %(as_of)s
