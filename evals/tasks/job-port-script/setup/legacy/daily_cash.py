"""Nightly cash: each account's net cash movement per currency on one value date, written to the warehouse.

    python daily_cash.py 2026-11-05

Reads dbo.cash_movements in the ops database:

    movement_id   VARCHAR(32)    NOT NULL  PRIMARY KEY
    account_id    VARCHAR(20)    NOT NULL
    currency      CHAR(3)        NOT NULL
    amount        DECIMAL(19,4)  NOT NULL  -- signed: receipts are positive, payments negative
    value_date    DATE           NOT NULL
    status        VARCHAR(10)    NOT NULL  -- PENDING, SETTLED or CANCELLED

Writes dbo.daily_cash in the warehouse database:

    value_date    DATE           NOT NULL
    account_id    VARCHAR(20)    NOT NULL
    currency      CHAR(3)        NOT NULL
    net_amount    DECIMAL(19,4)  NOT NULL
    movements     INT            NOT NULL
    PRIMARY KEY (value_date, account_id, currency)
"""

import sys
from datetime import date, timedelta
from decimal import Decimal

import pyodbc

OPS = "DSN=ops;Trusted_Connection=yes"
WAREHOUSE = "DSN=warehouse;Trusted_Connection=yes"
SELECT = "SELECT movement_id, account_id, currency, amount, value_date, status FROM dbo.cash_movements WHERE value_date >= ? AND value_date < ?"
INSERT = "INSERT INTO dbo.daily_cash (value_date, account_id, currency, net_amount, movements) VALUES (?, ?, ?, ?, ?)"


def main(day: date) -> None:
    ops = pyodbc.connect(OPS)
    rows = ops.execute(SELECT, day, day + timedelta(days=1)).fetchall()
    totals: dict[tuple[date, str, str], tuple[Decimal, int]] = {}
    for row in rows:
        if row.status == "CANCELLED":
            continue
        if len(row.currency) != 3 or not row.currency.isupper():
            sys.exit(f"movement {row.movement_id} has a bad currency {row.currency!r}: nothing written")
        key = (row.value_date, row.account_id, row.currency)
        net, count = totals.get(key, (Decimal(0), 0))
        totals[key] = (net + row.amount, count + 1)
    warehouse = pyodbc.connect(WAREHOUSE)
    cursor = warehouse.cursor()
    cursor.execute("DELETE FROM dbo.daily_cash WHERE value_date = ?", day)
    for (value_date, account_id, currency), (net, count) in sorted(totals.items()):
        cursor.execute(INSERT, value_date, account_id, currency, net, count)
    warehouse.commit()


if __name__ == "__main__":
    main(date.fromisoformat(sys.argv[1]))
