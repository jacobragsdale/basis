CREATE TABLE etl.publishes (
    run_id CHAR(36) NOT NULL,
    job NVARCHAR(200) NOT NULL,
    output NVARCHAR(128) NOT NULL,
    table_name NVARCHAR(261) NOT NULL,
    interval_start DATETIME2 NULL,
    interval_end DATETIME2 NULL,
    rerun_safe BIT NOT NULL,
    deleted BIGINT NOT NULL,
    upserted BIGINT NOT NULL,
    inserted_new BIGINT NOT NULL,
    inserted BIGINT NOT NULL,
    published_at DATETIME2 NOT NULL,
    etl_version NVARCHAR(50) NOT NULL,
    detail NVARCHAR(MAX) NULL,
    CONSTRAINT pk_publishes PRIMARY KEY (run_id, output)
);
CREATE INDEX ix_publishes_interval ON etl.publishes (job, output, interval_start, interval_end);
