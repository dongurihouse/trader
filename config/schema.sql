PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS bars (
    ticker     TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     INTEGER NOT NULL,
    interpolated INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts),
    CHECK (interpolated IN (0, 1))
);

CREATE TABLE IF NOT EXISTS bar_jobs (
    kind         TEXT    NOT NULL,
    scope        TEXT    NOT NULL,
    target       TEXT    NOT NULL,
    window_start INTEGER NOT NULL,
    window_end   INTEGER NOT NULL,
    progress_ts  INTEGER,
    started_at   INTEGER NOT NULL,
    completed_at INTEGER,
    PRIMARY KEY (kind, scope, target),
    CHECK (kind IN ('backfill', 'sweep')),
    CHECK (window_start <= window_end),
    CHECK (
        progress_ts IS NULL OR
        (window_start <= progress_ts AND progress_ts <= window_end)
    ),
    CHECK (completed_at IS NULL OR progress_ts IS NOT NULL)
);

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
);

CREATE TABLE IF NOT EXISTS trades (
    ticker          TEXT    NOT NULL,
    algo            TEXT    NOT NULL,
    ts              INTEGER NOT NULL,
    action          TEXT    NOT NULL,
    direction       INTEGER NOT NULL DEFAULT 1,
    real_order      INTEGER NOT NULL DEFAULT 0,
    broker_order_id TEXT,
    PRIMARY KEY (ticker, algo, ts, action),
    CHECK (action IN ('entry', 'exit_all')),
    CHECK (direction IN (-1, 1)),
    CHECK (real_order IN (0, 1))
);

CREATE TABLE IF NOT EXISTS broker_positions (
    ticker          TEXT    NOT NULL,
    algo            TEXT    NOT NULL,
    entry_ts        INTEGER NOT NULL,
    direction       INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    entry_order_id  TEXT    NOT NULL UNIQUE,
    exit_ts         INTEGER,
    exit_order_id   TEXT,
    PRIMARY KEY (ticker, algo, entry_ts),
    CHECK (direction IN (-1, 1)),
    CHECK (
        (exit_ts IS NULL AND exit_order_id IS NULL) OR
        (exit_ts IS NOT NULL AND exit_order_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS option_shadows (
    ticker              TEXT    NOT NULL,
    algo                TEXT    NOT NULL,
    entry_ts            INTEGER NOT NULL,
    direction           INTEGER NOT NULL,
    option_id           TEXT,
    option_type         TEXT,
    expiration_date     TEXT,
    strike_price        REAL,
    underlying_price    REAL,
    entry_ask           REAL,
    entry_quote_ts      TEXT,
    exit_ts             INTEGER,
    exit_bid            REAL,
    exit_quote_ts       TEXT,
    return_pct          REAL,
    pnl_dollars         REAL,
    status              TEXT    NOT NULL,
    error               TEXT,
    updated_at          INTEGER NOT NULL,
    PRIMARY KEY (ticker, algo, entry_ts),
    CHECK (direction IN (-1, 1)),
    CHECK (option_type IS NULL OR option_type IN ('call', 'put')),
    CHECK (status IN ('open', 'closed', 'entry_error', 'exit_error')),
    CHECK (entry_ask IS NULL OR entry_ask >= 0),
    CHECK (exit_bid IS NULL OR exit_bid >= 0)
);

CREATE TABLE IF NOT EXISTS outputs (
    ticker      TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL,
    output      TEXT    NOT NULL,
    computed_at INTEGER NOT NULL,
    PRIMARY KEY (ticker, ts, kind)
);

CREATE TABLE IF NOT EXISTS signals (
    name        TEXT    NOT NULL PRIMARY KEY,
    definition  TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS algos (
    name          TEXT    NOT NULL PRIMARY KEY,
    definition    TEXT    NOT NULL,
    dependencies  TEXT    NOT NULL,
    active_from   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    ts      INTEGER NOT NULL,
    service TEXT    NOT NULL,
    level   TEXT    NOT NULL,
    message TEXT    NOT NULL,
    CHECK (level IN ('info', 'warn', 'error'))
);

CREATE INDEX IF NOT EXISTS logs_service_ts ON logs (service, ts);
