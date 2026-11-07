"""Security master: the source's active securities, copied whole into the warehouse. A run has no interval, and
replaces the table."""

import etl_framework as etl
import polars as pl

from jobs.security_master.rows import MasterSecurity, Security


class Params(etl.Params):
    """This job has no params."""


class Inputs(etl.Inputs):
    securities = etl.Query(Security, database="source", sql="sql/securities.sql", empty="fail")


class Outputs(etl.Outputs):
    master = etl.Table(MasterSecurity, database="warehouse", table="security_master", key=("security_id",), write=etl.Replace())


def transform(inputs: Inputs, params: Params) -> Outputs:
    return Outputs(master=inputs.securities.filter(pl.col("active")).drop("active").sort("security_id"))


def check(inputs: Inputs, outputs: Outputs, params: Params) -> list[str]:
    """Reasons not to write, beyond the framework's own checks."""
    shared = outputs.master.filter(pl.col("isin").is_duplicated()).get_column("isin").unique().sort().to_list()
    return [f"active securities share the ISINs {shared}"] if shared else []
