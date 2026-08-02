"""Session directory discovery and resolution."""

from __future__ import annotations

from pathlib import Path


def list_sessions(data_root: Path) -> list[str]:
    """Return session-directory names from oldest to newest timestamp suffix."""
    sessions_dir = data_root / "sessions"
    if not sessions_dir.exists():
        return []

    session_ids = [path.name for path in sessions_dir.iterdir() if path.is_dir()]
    return sorted(session_ids, key=_session_sort_key)


def default_results_session_id(data_root: Path) -> str | None:
    """Return the default session id for post-session results, if one exists."""
    session_ids = list_sessions(data_root)
    if not session_ids:
        return None

    for session_id in reversed(session_ids):
        if session_id.startswith("backtest-"):
            return session_id
    return session_ids[-1]


def _session_sort_key(session_id: str) -> tuple[int, str, str, str]:
    parts = session_id.rsplit("-", 2)
    if len(parts) == 3:
        _, date, time = parts
        if len(date) == 8 and date.isdigit() and len(time) == 6 and time.isdigit():
            return (1, date, time, session_id)
    return (0, "", "", session_id)


def _resolve_existing_session_dir(sessions_dir: Path, session_id: str) -> Path:
    resolved_sessions_dir = sessions_dir.resolve()
    session_dir = (sessions_dir / session_id).resolve()
    if session_dir.is_relative_to(resolved_sessions_dir) and session_dir.is_dir():
        return session_dir
    raise FileNotFoundError(f"session {session_id!r} does not exist")


def resolve_session_dir(data_root: Path, session_id: str | None) -> Path:
    """Resolve a named session, or the newest session when no name is given."""
    sessions_dir = data_root / "sessions"

    if session_id is not None:
        if (
            not session_id
            or session_id in {".", ".."}
            or Path(session_id).is_absolute()
            or "/" in session_id
            or "\\" in session_id
        ):
            raise FileNotFoundError(f"session {session_id!r} does not exist")

        return _resolve_existing_session_dir(sessions_dir, session_id)

    session_ids = list_sessions(data_root)
    if not session_ids:
        raise FileNotFoundError(f"no sessions found under {sessions_dir}")
    return _resolve_existing_session_dir(sessions_dir, session_ids[-1])
