"""Headless MCP relay for fetching verbatim Robinhood bar payloads."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any, NoReturn

from trader.provider.calendar import MarketCalendarImpl


DEFAULT_TOOL = "mcp__robinhood-trading__get_equity_historicals"
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TIMEOUT_S = 300

_SAVED_TO = re.compile(r"saved to (\S+)")
_AUTH_MARKERS = (
    "auth",
    "unavailable",
    "not available",
    "no such tool",
    "not connected",
)


class RelayError(RuntimeError):
    """The relay run produced no usable, validated payload."""


class RelayAuthRequired(RelayError):
    """The stored MCP server authentication has lapsed or is unavailable."""


_AUTH_FIX = (
    "Robinhood MCP auth required: re-authenticate with `/mcp` in an "
    "interactive `claude` session (server `robinhood-trading`) — a "
    "~30-second action; no credentials pass through this codebase."
)


def build_command(
    symbols: Iterable[str],
    start_iso: str,
    end_iso: str,
    interval: str,
    bounds: str,
    model: str = DEFAULT_MODEL,
    *,
    tool: str = DEFAULT_TOOL,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> list[str]:
    """Build argv for a single tightly constrained headless MCP tool call."""
    args_json = json.dumps(
        {
            "symbols": [symbol.strip().upper() for symbol in symbols],
            "start_time": start_iso,
            "end_time": end_iso,
            "interval": interval,
            "bounds": bounds,
        }
    )
    prompt = (
        f"Call the tool {tool} exactly once, with exactly these arguments:\n"
        f"{args_json}\n"
        "Do not change any argument. Make no other tool calls. After the "
        "tool returns, reply with exactly DONE and output no other text."
    )
    return [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        tool,
        "--max-turns",
        "2",
        "--model",
        model,
    ]


def session_range_iso(
    calendar: MarketCalendarImpl,
    day: date,
) -> tuple[str, str]:
    """Return a provider calendar session's inclusive UTC relay range."""
    if not calendar.is_session(day):
        raise RelayError(f"{day.isoformat()} is not a trading day")

    open_utc = datetime.combine(
        day,
        calendar.open_time,
        tzinfo=calendar.timezone,
    ).astimezone(timezone.utc)
    close_utc = calendar.session_close(day)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return open_utc.strftime(fmt), close_utc.strftime(fmt)


def _events(stdout: str) -> list[dict]:
    events: list[dict] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _blocks(events: Iterable[dict], event_type: str) -> Iterator[dict]:
    for event in events:
        if event.get("type") != event_type:
            continue
        content = (event.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield block


def _content_text(content: object) -> str:
    """Flatten tool-result content from either supported stream-json shape."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
        )
    return ""


def _raise_failure(message: str, hint_text: str) -> NoReturn:
    low = (hint_text or "").lower()
    if any(marker in low for marker in _AUTH_MARKERS):
        raise RelayAuthRequired(
            f"{_AUTH_FIX} (underlying error: {hint_text.strip()})"
        )
    raise RelayError(message)


def _parse_result_text(text: str) -> dict:
    """Parse an inline JSON payload or a Claude auto-save file notice."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return payload

    match = _SAVED_TO.search(text)
    if match:
        path = Path(match.group(1).rstrip(".,;:)]\"'"))
        if not path.is_file():
            raise RelayError(
                "tool result points at a saved-output file that does not "
                f"exist: {path}"
            )
        try:
            saved = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise RelayError(
                f"saved tool output at {path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(saved, dict):
            raise RelayError(
                f"saved tool output at {path} is not a JSON object"
            )
        return saved

    raise RelayError(
        "tool result is neither inline JSON nor a saved-output notice: "
        f"{text[:200]!r}"
    )


def run_relay(
    symbols: Iterable[str],
    *,
    start_iso: str,
    end_iso: str,
    interval: str = "minute",
    bounds: str = "regular",
    tool: str = DEFAULT_TOOL,
    claude_bin: str = DEFAULT_CLAUDE_BIN,
    model: str = DEFAULT_MODEL,
    timeout_s: int | float = DEFAULT_TIMEOUT_S,
    runner: Callable[..., Any] = subprocess.run,
) -> dict:
    """Run the relay and return its verbatim tool-result payload."""
    command = build_command(
        symbols,
        start_iso,
        end_iso,
        interval,
        bounds,
        model,
        tool=tool,
        claude_bin=claude_bin,
    )
    try:
        process = runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise RelayError(f"relay run timed out after {timeout_s}s") from exc

    events = _events(getattr(process, "stdout", "") or "")
    tool_ids = {
        block.get("id")
        for block in _blocks(events, "assistant")
        if block.get("type") == "tool_use" and block.get("name") == tool
    }
    result_block = None
    for block in _blocks(events, "user"):
        if (
            block.get("type") == "tool_result"
            and block.get("tool_use_id") in tool_ids
        ):
            result_block = block
            break

    if result_block is None:
        hints = [
            block.get("text", "")
            for block in _blocks(events, "assistant")
            if block.get("type") == "text"
        ]
        hints += [
            str(event.get("result", ""))
            for event in events
            if event.get("type") == "result"
        ]
        hints.append(getattr(process, "stderr", "") or "")
        hint_text = "\n".join(hint for hint in hints if hint)
        _raise_failure(
            f"relay run never produced a result from {tool} "
            f"(exit {getattr(process, 'returncode', '?')}): "
            f"{hint_text.strip()[:300]!r}",
            hint_text,
        )

    text = _content_text(result_block.get("content"))
    if result_block.get("is_error"):
        _raise_failure(f"tool call failed: {text.strip()[:300]}", text)
    return _parse_result_text(text)


def _parse_ts(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def _session_seconds(
    calendar: MarketCalendarImpl,
    day: date,
    close_time: time,
) -> int:
    return int(
        (
            datetime.combine(day, close_time)
            - datetime.combine(day, calendar.open_time)
        ).total_seconds()
    )


def _minimum_intraday_bars(
    calendar: MarketCalendarImpl,
    day: date,
    min_intraday_bars: int,
) -> int:
    if min_intraday_bars < 1:
        raise RelayError("min_intraday_bars must be at least 1")
    if day not in calendar.early_closes:
        return min_intraday_bars

    regular_seconds = _session_seconds(calendar, day, calendar.close_time)
    actual_seconds = _session_seconds(calendar, day, calendar.early_close_time)
    if regular_seconds <= 0 or actual_seconds <= 0:
        raise RelayError(f"{day.isoformat()}: calendar session length is invalid")

    scaled = (
        min_intraday_bars * actual_seconds + regular_seconds - 1
    ) // regular_seconds
    return min(min_intraday_bars, max(1, scaled))


def validate_payload(
    payload: dict,
    symbols: Iterable[str],
    start_iso: str,
    end_iso: str,
    *,
    calendar: MarketCalendarImpl,
    day: date,
    min_intraday_bars: int,
) -> None:
    """Reject relay payloads missing requested or in-range bar coverage."""
    if not isinstance(payload, dict):
        raise RelayError(
            f"payload is not a JSON object: {type(payload).__name__}"
        )
    results = (payload.get("data") or {}).get("results") or []
    if not results:
        raise RelayError("payload has no data.results blocks")

    wanted = {symbol.strip().upper() for symbol in symbols}
    got = {result.get("symbol") for result in results}
    missing = sorted(wanted - got)
    if missing:
        raise RelayError(
            "requested symbols missing from payload: " + ", ".join(missing)
        )

    start = _parse_ts(start_iso)
    end = _parse_ts(end_iso)
    required_bars = _minimum_intraday_bars(calendar, day, min_intraday_bars)
    for result in results:
        symbol = result.get("symbol")
        bars = result.get("bars") or []
        if not bars:
            raise RelayError(f"{symbol}: result block has no bars")
        if len(bars) < required_bars:
            shortfall = required_bars - len(bars)
            raise RelayError(
                f"{symbol}: result block has {len(bars)} bars; "
                f"minimum {required_bars}; short by {shortfall}"
            )
        for bar in (bars[0], bars[-1]):
            timestamp = _parse_ts(bar["begins_at"])
            if not start <= timestamp <= end:
                raise RelayError(
                    f"{symbol}: bar begins_at {bar['begins_at']} is outside "
                    f"[{start_iso}, {end_iso}]"
                )


def save_raw(payload: dict, out_root: Path, day: date, tag: str) -> Path:
    """Save one relay payload beneath its symbol or ``MULTI`` directory."""
    results = payload["data"]["results"]
    symbols = sorted({result["symbol"] for result in results})
    symbol_dir = symbols[0] if len(symbols) == 1 else "MULTI"
    out_dir = Path(out_root) / symbol_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rh-relay-{day}-{tag}.json"
    path.write_text(json.dumps(payload, indent=1))
    return path


__all__ = [
    "DEFAULT_CLAUDE_BIN",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TOOL",
    "RelayAuthRequired",
    "RelayError",
    "build_command",
    "run_relay",
    "save_raw",
    "session_range_iso",
    "validate_payload",
]
