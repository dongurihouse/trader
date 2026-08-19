"""SQLite storage for bars, metadata, jobs, logs, and operational queries."""

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from bar_provider import (
    UTC,
    BarRow,
    ConfigError,
    MetadataPoint,
    TechnicalSpec,
    _epoch,
    _parse_iso,
)


SCHEMA_PATH = Path(__file__).resolve().parent.parent / "config" / "schema.sql"
EASTERN = ZoneInfo("America/New_York")
BAR_COLUMNS = (
    "ticker",
    "ts",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "interpolated",
)


def _query_epoch(value: str, end: bool = False) -> int:
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            clock = clock_time(23, 59, 59) if end else clock_time(0, 0)
            return _epoch(datetime.combine(day, clock, tzinfo=EASTERN))
        return _epoch(_parse_iso(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError("invalid timestamp: %s" % value) from exc


@dataclass(frozen=True)
class JobRef:
    kind: str
    target: str
    scopes: Tuple[str, ...]


@dataclass(frozen=True)
class JobState:
    scope: str
    window_start: int
    window_end: int
    progress_ts: Optional[int]


class BarStore:
    def __init__(self, path: Path, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            uri = "file:%s?mode=ro" % self.path.resolve()
            self.connection = sqlite3.connect(uri, uri=True, timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA busy_timeout=30000")
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            schema = SCHEMA_PATH.read_text()
        except FileNotFoundError as exc:
            raise ConfigError("schema file does not exist: %s" % SCHEMA_PATH) from exc
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(schema)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _upsert(connection, rows: Sequence[tuple]) -> None:
        connection.executemany(
            """
            INSERT INTO bars (
                ticker, ts, open, high, low, close, volume,
                interpolated, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker, ts) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                interpolated=excluded.interpolated,
                fetched_at=excluded.fetched_at
            """,
            rows,
        )

    def store_bars(
        self,
        bars: Sequence[BarRow],
        job: Optional[JobRef] = None,
        progress_ts: Optional[int] = None,
    ) -> Dict[str, int]:
        if (job is None) != (progress_ts is None):
            raise ValueError("job and progress_ts must be supplied together")
        stats = {
            "received": len(bars),
            "interpolated": sum(int(bar.interpolated) for bar in bars),
        }
        fetched_at = _epoch(datetime.now(UTC))
        rows = [
            (
                bar.ticker,
                bar.ts,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                int(bar.interpolated),
                fetched_at,
            )
            for bar in bars
        ]
        with self.connection as connection:
            self._upsert(connection, rows)
            if job is not None:
                connection.executemany(
                    """
                    UPDATE bar_jobs
                    SET progress_ts = CASE
                        WHEN progress_ts IS NULL OR progress_ts < ? THEN ?
                        ELSE progress_ts
                    END
                    WHERE kind=? AND scope=? AND target=? AND completed_at IS NULL
                    """,
                    [
                        (
                            progress_ts,
                            progress_ts,
                            job.kind,
                            scope,
                            job.target,
                        )
                        for scope in job.scopes
                    ],
                )
        return stats

    def store_metadata(
        self,
        points: Sequence[MetadataPoint],
        ticker: str,
        spec: TechnicalSpec,
    ) -> Dict[str, int]:
        fetched_at = _epoch(datetime.now(UTC))
        rows = [
            (
                ticker,
                point.ts,
                spec.name,
                spec.params,
                point.value,
                fetched_at,
                ticker,
                point.ts,
            )
            for point in points
        ]
        with self.connection as connection:
            cursor = connection.executemany(
                """
                INSERT INTO bar_metadata (
                    ticker, ts, name, params, value, fetched_at
                )
                SELECT ?, ?, ?, ?, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM bars WHERE ticker=? AND ts=?
                )
                ON CONFLICT(ticker, ts, name, params) DO UPDATE SET
                    value=excluded.value,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
        return {"received": len(points), "written": cursor.rowcount}

    def latest_metadata(
        self, ticker: str, spec: TechnicalSpec, refreshed_after: int
    ) -> Optional[int]:
        row = self.connection.execute(
            """
            SELECT MAX(ts) AS ts FROM bar_metadata
            WHERE ticker=? AND name=? AND params=? AND fetched_at>=?
            """,
            (ticker, spec.name, spec.params, refreshed_after),
        ).fetchone()
        return row["ts"]

    def ensure_jobs(
        self,
        kind: str,
        scopes: Sequence[str],
        target: str,
        window_start: int,
        window_end: int,
        force: bool = False,
    ) -> None:
        started_at = _epoch(datetime.now(UTC))
        conflict = (
            """ON CONFLICT(kind, scope, target) DO UPDATE SET
                window_start=excluded.window_start,
                window_end=excluded.window_end,
                progress_ts=NULL,
                started_at=excluded.started_at,
                completed_at=NULL"""
            if force
            else "ON CONFLICT(kind, scope, target) DO NOTHING"
        )
        statement = """
            INSERT INTO bar_jobs (
                kind, scope, target, window_start, window_end, started_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            %s
        """ % conflict
        with self.connection as connection:
            connection.executemany(
                statement,
                [
                    (kind, scope, target, window_start, window_end, started_at)
                    for scope in scopes
                ],
            )

    def incomplete_jobs(
        self, kind: str, scopes: Sequence[str], target: str
    ) -> List[JobState]:
        placeholders = ",".join("?" for _ in scopes)
        rows = self.connection.execute(
            """
            SELECT scope, window_start, window_end, progress_ts
            FROM bar_jobs
            WHERE kind=? AND target=? AND completed_at IS NULL
              AND scope IN (%s)
            ORDER BY scope
            """ % placeholders,
            (kind, target, *scopes),
        ).fetchall()
        return [JobState(**dict(row)) for row in rows]

    def complete_jobs(
        self, kind: str, scopes: Sequence[str], target: str
    ) -> None:
        completed_at = _epoch(datetime.now(UTC))
        with self.connection as connection:
            connection.executemany(
                """
                UPDATE bar_jobs
                SET progress_ts=window_end, completed_at=?
                WHERE kind=? AND scope=? AND target=? AND completed_at IS NULL
                """,
                [(completed_at, kind, scope, target) for scope in scopes],
            )

    def latest_by_ticker(self, tickers: Sequence[str]) -> Dict[str, Optional[int]]:
        latest = {ticker: None for ticker in tickers}
        placeholders = ",".join("?" for _ in tickers)
        rows = self.connection.execute(
            "SELECT ticker, MAX(ts) AS ts FROM bars "
            "WHERE ticker IN (%s) GROUP BY ticker" % placeholders,
            tuple(tickers),
        ).fetchall()
        for row in rows:
            latest[row["ticker"]] = row["ts"]
        return latest

    def append_log(self, level: str, message: str) -> None:
        with self.connection as connection:
            connection.execute(
                "INSERT INTO logs(ts, service, level, message) VALUES (?, ?, ?, ?)",
                (_epoch(datetime.now(UTC)), "bars", level, message),
            )

    def recent_logs(self, limit: int = 50) -> List[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT ts,service,level,message FROM logs
            ORDER BY rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def sweep_complete(self, day: date, tickers: Sequence[str]) -> bool:
        if not tickers:
            return False
        scopes = tuple(dict.fromkeys(tickers))
        placeholders = ",".join("?" for _ in scopes)
        row = self.connection.execute(
            """
            SELECT COUNT(DISTINCT scope) AS completed FROM bar_jobs
            WHERE kind='sweep' AND target=? AND scope IN (%s)
              AND completed_at IS NOT NULL
            """ % placeholders,
            (day.isoformat(), *scopes),
        ).fetchone()
        return int(row["completed"]) == len(scopes)

    def summary(self, tickers: Sequence[str]) -> List[sqlite3.Row]:
        placeholders = ",".join("?" for _ in tickers)
        query = """
            SELECT ticker, COUNT(*) AS bars, MIN(ts) AS earliest, MAX(ts) AS latest
            FROM bars
            WHERE ticker IN (%s)
            GROUP BY ticker
            ORDER BY ticker
        """ % placeholders
        return self.connection.execute(query, tuple(tickers)).fetchall()

    def query(
        self,
        ticker: str,
        start: Optional[str],
        end: Optional[str],
        limit: Optional[int],
    ) -> List[sqlite3.Row]:
        clauses = ["ticker=?"]
        arguments: List[object] = [ticker]
        if start:
            clauses.append("ts>=?")
            arguments.append(_query_epoch(start))
        if end:
            clauses.append("ts<=?")
            arguments.append(_query_epoch(end, end=True))
        query = (
            "SELECT %s FROM bars WHERE %s ORDER BY ts"
            % (", ".join(BAR_COLUMNS), " AND ".join(clauses))
        )
        if limit:
            query += " LIMIT ?"
            arguments.append(limit)
        return self.connection.execute(query, tuple(arguments)).fetchall()
