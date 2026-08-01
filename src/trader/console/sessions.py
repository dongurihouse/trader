"""Session directory discovery and resolution."""

from __future__ import annotations

from pathlib import Path


def list_sessions(data_root: Path) -> list[str]:
    """Return session-directory names in chronological string order."""
    sessions_dir = data_root / "sessions"
    if not sessions_dir.exists():
        return []

    return sorted(path.name for path in sessions_dir.iterdir() if path.is_dir())


def resolve_session_dir(data_root: Path, session_id: str | None) -> Path:
    """Resolve a named session, or the newest session when no name is given."""
    sessions_dir = data_root / "sessions"

    if session_id is not None:
        session_dir = sessions_dir / session_id
        if session_dir.is_dir():
            return session_dir
        raise FileNotFoundError(f"session {session_id!r} does not exist")

    session_ids = list_sessions(data_root)
    if not session_ids:
        raise FileNotFoundError(f"no sessions found under {sessions_dir}")
    return sessions_dir / session_ids[-1]
