The quotes service's dirty-price route should refuse a face amount above a limit set in the service's settings, `max_face`, which is 1,000,000,000 unless configured otherwise. Answer such a request with a 422 problem whose errors name the `face` parameter, as the accrued-interest route's name `end`.

When you're done, the repo's checks should pass.
