"""What the Anthropic API cost, read from the organisation's cost report.

This is the one tool whose bill moves with use, so typing a figure into Notion
would be stale by the next run. It needs an *admin* key (`sk-ant-admin...`),
issued by an organisation admin in the Console -- a different key from the one
the bot calls the model with, and one that can read usage and nothing else
here. Without it the Build Tools database simply holds a hand-typed estimate.

The report quotes USD in cents, as a decimal string.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx

API = "https://api.anthropic.com"
VERSION = "2023-06-01"
# A month never needs more than 31 daily buckets, the API's own ceiling.
LIMIT = 31


class UsageError(RuntimeError):
    pass


def _utc(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AnthropicUsage:
    def __init__(self, admin_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._key = admin_key

    async def cost(self, first: date, stop: date) -> Decimal:
        """Dollars spent from `first` up to, not including, `stop` (UTC days)."""
        if self._client is not None:
            return await self._cost(self._client, first, stop)
        async with httpx.AsyncClient(base_url=API, timeout=30) as client:
            return await self._cost(client, first, stop)

    async def _cost(self, client: httpx.AsyncClient, first: date, stop: date) -> Decimal:
        cents = Decimal(0)
        page: str | None = None
        while True:
            params = {
                "starting_at": _utc(first),
                "ending_at": _utc(stop),
                "bucket_width": "1d",
                "limit": LIMIT,
            }
            if page:
                params["page"] = page
            try:
                resp = await client.get(
                    "/v1/organizations/cost_report",
                    params=params,
                    headers={"x-api-key": self._key, "anthropic-version": VERSION},
                )
                resp.raise_for_status()
                body = resp.json()
                for bucket in body["data"]:
                    for result in bucket["results"]:
                        if result.get("currency", "USD") != "USD":
                            raise UsageError(f"unexpected currency {result['currency']}")
                        cents += Decimal(str(result["amount"]))
            except httpx.HTTPStatusError as exc:
                # The body names the cause (a non-admin key, mostly), and the
                # status alone would send someone hunting in the wrong place.
                raise UsageError(f"{exc.response.status_code} from the cost report") from exc
            except (httpx.HTTPError, KeyError, TypeError, ArithmeticError, ValueError) as exc:
                raise UsageError(f"cost report unreadable: {exc}") from exc
            if not body.get("has_more") or not body.get("next_page"):
                return cents / 100
            page = body["next_page"]


def month_bounds(today: date) -> tuple[date, date, date]:
    """First of this month, first of next, and first of last."""
    first = today.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    return first, nxt, prev
