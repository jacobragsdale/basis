# Security master

The source's active securities, copied whole into the warehouse.

| | |
|---|---|
| Reads | `securities`, from `source` |
| Writes | `security_master`, replaced on every run |
| Params | none |
| Checks | no two active securities share an ISIN |
