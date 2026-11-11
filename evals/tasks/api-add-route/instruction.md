Add a route to the quotes service, `GET /bonds/{security}/next-coupon?settle=DATE`: the next coupon a bond in the warehouse pays after the settlement date, with the day its period starts accruing, the day it pays, and its interest per 100 of face. It answers the same problems the other routes of a bond do when the bond is unknown, or the date is outside the bond's life.

When you're done, the repo's checks should pass.
