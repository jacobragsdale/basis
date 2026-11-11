CREATE TABLE etl.publishes (
    run_id CHAR(36) NOT NULL,
    job VARCHAR2(200 CHAR) NOT NULL,
    output VARCHAR2(128 CHAR) NOT NULL,
    table_name VARCHAR2(261 CHAR) NOT NULL,
    interval_start TIMESTAMP NULL,
    interval_end TIMESTAMP NULL,
    rerun_safe NUMBER(1) NOT NULL,
    deleted NUMBER(19) NOT NULL,
    upserted NUMBER(19) NOT NULL,
    inserted_new NUMBER(19) NOT NULL,
    inserted NUMBER(19) NOT NULL,
    published_at TIMESTAMP NOT NULL,
    etl_version VARCHAR2(50 CHAR) NOT NULL,
    detail CLOB NULL,
    CONSTRAINT pk_publishes PRIMARY KEY (run_id, output)
);
CREATE INDEX ix_publishes_interval ON etl.publishes (job, output, interval_start, interval_end);
CREATE TABLE etl.publishes_locks (
    job VARCHAR2(200 CHAR) NOT NULL,
    output VARCHAR2(128 CHAR) NOT NULL,
    CONSTRAINT pk_publishes_locks PRIMARY KEY (job, output)
);
