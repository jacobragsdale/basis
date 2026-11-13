-- Each holding of each book at the close of the day asked for, priced as the warehouse priced it.
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
    p.ytw,
    p.duration,
    p.oas
from holdings as h
join securities as s on s.cusip = h.cusip
join prices as p on p.cusip = h.cusip and p.as_of = h.as_of
where h.as_of = %(as_of)s
