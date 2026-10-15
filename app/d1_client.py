from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Sequence
from urllib import error, request


class D1Error(RuntimeError):
    """Raised when Cloudflare D1 rejects a request."""


@dataclass(frozen=True)
class D1Statement:
    sql: str
    params: tuple[str, ...] = ()


class D1Client:
    """Cloudflare D1 REST query API; batch requests use one statement per item."""

    def __init__(
        self,
        *,
        account_id: str,
        database_id: str,
        api_token: str,
        timeout_seconds: float = 30.0,
        api_base: str = "https://api.cloudflare.com/client/v4",
    ) -> None:
        self.endpoint = (
            f"{api_base.rstrip('/')}/accounts/{account_id}/d1/database/{database_id}/query"
        )
        self._token = api_token
        self._timeout = timeout_seconds

    def query(self, sql: str, params: Sequence[str] = ()) -> list[dict[str, Any]]:
        result = self._request({"sql": sql, "params": list(params)}, expected=1)
        return self._rows(result[0])

    def batch(self, statements: Sequence[D1Statement]) -> None:
        if not statements:
            return
        body = {
            "batch": [
                {"sql": item.sql, "params": list(item.params)} for item in statements
            ]
        }
        self._request(body, expected=len(statements))

    @staticmethod
    def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        rows = result.get("results", [])
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise D1Error("D1 returned an invalid query result")
        return rows

    def _request(self, body: dict[str, Any], *, expected: int) -> list[dict[str, Any]]:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "pg-d1-sync/0.1.0",
            },
        )
        try:
            with request.urlopen(req, timeout=self._timeout) as response:
                data = json.load(response)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise D1Error(f"D1 HTTP {exc.code}: {detail[:1000]}") from exc
        except (error.URLError, TimeoutError) as exc:
            raise D1Error(f"D1 request failed: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise D1Error("D1 returned invalid JSON") from exc

        if not isinstance(data, dict) or not data.get("success"):
            errors = data.get("errors", []) if isinstance(data, dict) else []
            message = "; ".join(str(x.get("message", x)) for x in errors) or "unknown API error"
            raise D1Error(f"D1 query failed: {message}")
        result = data.get("result")
        if isinstance(result, dict) and expected == 1:
            result = [result]
        if not isinstance(result, list) or len(result) != expected:
            raise D1Error(f"D1 returned {len(result) if isinstance(result, list) else 0} results, expected {expected}")
        if not all(isinstance(item, dict) for item in result):
            raise D1Error("D1 returned invalid batch results")
        for item in result:
            if item.get("success") is False:
                raise D1Error(f"D1 statement failed: {item.get('error', 'unknown error')}")
        return result
