Add a job named `security_master` to this repo, which copies the source's securities into the warehouse:

- It reads `dbo.securities` from the `source` database. Its columns are `security_id` VARCHAR(12), the primary key; `isin` CHAR(12) NOT NULL; `name` VARCHAR(100) NOT NULL; `currency` CHAR(3) NOT NULL; `coupon` DECIMAL(9,6) NULL; `maturity_date` DATE NULL; and `active` BIT NOT NULL. Call the input `securities`.
- It writes the active securities, with every column but `active`, to the `security_master` table of the `warehouse` database, keyed by `security_id`, replacing the table's contents on every run. Call the output `master`.
- It has no interval: every run takes the whole table.
- It refuses to write when two active securities share an ISIN.

When you're done, the repo's checks should pass.
