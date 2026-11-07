from builders import Price

import etl_framework as etl
from etl_framework.ddl import create_table
from etl_framework.outputs import Target

TARGET = Target(Price, "warehouse", "dbo.prices", etl.Append(), ("day", "security_id"))


def test_sql_server_ddl_names_every_column_and_the_key() -> None:
    assert create_table(TARGET, "mssql") == (
        "CREATE TABLE dbo.prices (\n"
        "    day DATE NOT NULL,\n"
        "    security_id NVARCHAR(8) NOT NULL,\n"
        "    price DECIMAL(18,4) NOT NULL,\n"
        "    note NVARCHAR(MAX),  -- no MaxLength in the row: unbounded\n"
        "    booked_at DATETIME2(6),  -- naive, in America/New_York\n"
        "    CONSTRAINT pk_prices PRIMARY KEY (day, security_id)\n"
        ");\n"
    )


def test_oracle_ddl_uses_oracle_types() -> None:
    lines = create_table(TARGET, "oracle").splitlines()
    assert lines[1:6] == [
        "    day DATE NOT NULL,",
        "    security_id VARCHAR2(8 CHAR) NOT NULL,",
        "    price NUMBER(18,4) NOT NULL,",
        "    note CLOB,  -- no MaxLength in the row: unbounded",
        "    booked_at TIMESTAMP(6),  -- naive, in America/New_York",
    ]
