# Integration tests

These run against real databases, and only on request: `uv run pytest -m integration`, from the workspace's root.
They need a scratch SQL Server database and a scratch Oracle schema, which they create and drop tables in; never point
them at a shared one.

```sh
docker run -d --name etlf-test-mssql -p 127.0.0.1:14433:1433 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=Etl-test-1234 mcr.microsoft.com/mssql/server:2022-latest
docker run -d --name etlf-test-oracle -p 127.0.0.1:15221:1521 -e ORACLE_PASSWORD=etltest -e APP_USER=etl -e APP_USER_PASSWORD=etltest gvenzl/oracle-free:23-slim-faststart
# once SQL Server is up: a scratch database that allows snapshot reads
docker exec etlf-test-mssql /opt/mssql-tools18/bin/sqlcmd -C -U sa -P Etl-test-1234 -Q "CREATE DATABASE etltest; ALTER DATABASE etltest SET ALLOW_SNAPSHOT_ISOLATION ON"

export ETL_TEST_MSSQL='Server=127.0.0.1,14433;Database=etltest;UID=sa;PWD=Etl-test-1234;Encrypt=yes;TrustServerCertificate=yes'
export ETL_TEST_ORACLE_DSN=127.0.0.1:15221/FREEPDB1 ETL_TEST_ORACLE_USER=etl ETL_TEST_ORACLE_PASSWORD=etltest
uv run pytest -m integration
```
