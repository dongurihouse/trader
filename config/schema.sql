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
    ticker    TEXT    NOT NULL,
    algo      TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    action    TEXT    NOT NULL,
    direction INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (ticker, algo, ts, action),
    CHECK (action IN ('entry', 'exit_all')),
    CHECK (direction IN (-1, 1))
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
