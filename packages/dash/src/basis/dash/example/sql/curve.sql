-- The Treasury curve at the close asked for, and at the prior day's, a week's and a month's closes before it.
select c.close_name as [close], c.tenor, c.ytw  -- close is a word of T-SQL's own, so quoted
from treasury_curve as c
where c.as_of = %(as_of)s
