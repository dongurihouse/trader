"""Small shared helpers for loopback service APIs."""

from __future__ import annotations

import json
from typing import Any, Collection, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_operation(
    port: int,
    operation: str,
    allowed: Collection[str],
    service: str,
    install_command: str,
    error: type[Exception],
) -> dict[str, Any]:
    if operation not in allowed:
        raise error("unknown %s operation: %s" % (service, operation))
    request = Request(
        "http://127.0.0.1:%d/%s" % (port, operation),
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except HTTPError as exc:
        try:
            detail = json.load(exc)
        except (json.JSONDecodeError, OSError):
            detail = {"error": exc.reason}
        raise error(
            "%s %s request failed: %s"
            % (service, operation, detail.get("error", exc.reason))
        ) from exc
    except (URLError, OSError) as exc:
        raise error(
            "%s service is unavailable; start it with: %s"
            % (service, install_command)
        ) from exc
    if not isinstance(payload, dict):
        raise error("%s service returned an invalid response" % service)
    return payload


def write_json(handler: Any, payload: Mapping[str, Any], status: int = 200) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
