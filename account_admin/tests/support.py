from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

AccountAdminASGI = object  # The public HTTP gateway is tested separately.
from account_admin.config import LocalDevConfig


IDENTITY_TEST_KEY = b"identity-local-fixture-test-key-0001"
SERVICE_TEST_KEY = b"service-local-fixture-test-key-00001"


def config(data_dir: Path) -> LocalDevConfig:
    return LocalDevConfig(
        data_dir=data_dir,
        identity_signing_keys={"local-identity-v1": IDENTITY_TEST_KEY},
        service_signing_keys={"local-service-v1": SERVICE_TEST_KEY},
        allow_fixture_login=True,
    )


@dataclass
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)

    def header(self, name: str) -> str | None:
        normalized = name.casefold()
        return next((value for key, value in self.headers if key.casefold() == normalized), None)

    def headers_all(self, name: str) -> list[str]:
        normalized = name.casefold()
        return [value for key, value in self.headers if key.casefold() == normalized]


async def _request_async(
    app: AccountAdminASGI,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    client_ip: str = "127.0.0.1",
) -> Response:
    sent: list[dict[str, Any]] = []
    payload = json.dumps(body or {}).encode("utf-8") if body is not None else b""
    incoming = [
        {
            "type": "http.request",
            "body": payload,
            "more_body": False,
        }
    ]

    async def receive() -> dict[str, Any]:
        return incoming.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    raw_headers = {"user-agent": "account-admin-test/1.0", **(headers or {})}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [(key.casefold().encode("latin-1"), value.encode("latin-1")) for key, value in raw_headers.items()],
        "client": (client_ip, 41000),
        "server": ("127.0.0.1", 18081),
    }
    await app(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(item.get("body") or b"" for item in sent if item["type"] == "http.response.body")
    response_headers = [
        (name.decode("latin-1"), value.decode("latin-1")) for name, value in start.get("headers") or []
    ]
    return Response(status=start["status"], headers=response_headers, body=response_body)


def request(
    app: AccountAdminASGI,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    client_ip: str = "127.0.0.1",
) -> Response:
    return asyncio.run(_request_async(app, method, path, body=body, headers=headers, client_ip=client_ip))


def session_cookie(response: Response) -> str:
    values = response.headers_all("set-cookie")
    pairs = [item.split(";", 1)[0] for item in values]
    return "; ".join(pairs)


def login(app: AccountAdminASGI, fixture_id: str) -> tuple[Response, str, str]:
    response = request(
        app,
        "POST",
        "/account-admin/api/v1/local-fixture/login",
        body={"fixture_id": fixture_id},
        headers={"origin": "http://127.0.0.1:18081"},
    )
    payload = response.json()
    return response, session_cookie(response), str(payload.get("csrf_token") or "")


def mutation_headers(cookie: str, csrf_token: str) -> dict[str, str]:
    return {
        "cookie": cookie,
        "origin": "http://127.0.0.1:18081",
        "x-vq-csrf": csrf_token,
    }
