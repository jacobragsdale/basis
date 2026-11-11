I added a `max_price_move` param to the `positions` job, so its check refuses a day when a security's price moves too far, and now `uv run basis check` fails on coverage. Get the checks passing.
