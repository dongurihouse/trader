PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS bars (
    ticker     TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    ticker       TEXT    NOT NULL,
    event        TEXT    NOT NULL,
    event_ts     INTEGER NOT NULL,
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    direction    REAL    NOT NULL DEFAULT 0,
    strength     REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (ticker, event, event_ts),
    CHECK (window_start <= event_ts AND event_ts <= window_end),
    CHECK (direction >= -1 AND direction <= 1),
    CHECK (strength >= 0)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS trades (
    ticker TEXT    NOT NULL,
    algo   TEXT    NOT NULL,
    ts     INTEGER NOT NULL,
    action TEXT    NOT NULL,
    PRIMARY KEY (ticker, algo, ts, action),
    CHECK (action IN ('entry', 'exit', 'exit_all'))
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS outputs (
    ticker      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    config      TEXT    NOT NULL,
    output      TEXT    NOT NULL,
    computed_at INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts, kind, config)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS configs (
    version    TEXT    NOT NULL PRIMARY KEY,
    first_seen INTEGER NOT NULL,
    content    TEXT    NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS logs (
    ts      INTEGER NOT NULL,
    service TEXT    NOT NULL,
    level   TEXT    NOT NULL,
    message TEXT    NOT NULL,
    CHECK (level IN ('info', 'warn', 'error'))
);

CREATE INDEX IF NOT EXISTS logs_service_ts ON logs (service, ts);
