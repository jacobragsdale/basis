"""The security master: the source's active securities, copied whole into the warehouse on every run, which replaces
the table's contents. It has no interval. It refuses to write when two active securities share an ISIN."""

import basis.job as etl

from jobs.security_master.rows import MasterSecurity, Security


class Params(etl.Params):
    pass


class Inputs(etl.Inputs):
    securities = etl.Query(Security, database="source", sql="sql/securities.sql", empty="fail")


class Outputs(etl.Outputs):
    master = etl.Table(MasterSecurity, database="warehouse", table="security_master", key=("security_id",), write=etl.Replace())


def transform(inputs: Inputs, params: Params) -> Outputs:
    active = inputs.securities[inputs.securities["active"]]
    return Outputs(master=active.drop(columns="active").sort_values("security_id", ignore_index=True))


def check(inputs: Inputs, outputs: Outputs, params: Params) -> list[str]:
    """Reasons not to write, beyond the framework's own checks."""
    shared = outputs.master["isin"][outputs.master["isin"].duplicated()]
    return [f"active securities share the ISINs {sorted(set(shared))}"] if len(shared) else []
