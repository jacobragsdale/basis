I added a check to the `positions` job that refuses a trade of more than 10 million, with a test, and now `uv run basis check` fails on lint. Fix it, keeping what the check does.
