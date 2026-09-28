"""Exchange rates into won, looked up when asked for.

Frankfurter serves the European Central Bank's daily reference rates, free
and without a key. The rate is a day's, not a moment's -- plenty for a figure
decades away, and the date it is from is shown beside it.
"""

from __future__ import annotations

from datetime import date

import httpx

API = "https://api.frankfurter.dev/v1"
HOME = "KRW"


class FxError(RuntimeError):
    pass


class Rates:
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        # Asked for a few times a month at most, so a client per lookup,
        # closed after it, rather than one held open for the bot's lifetime.
        self._client = client

    async def to_won(self, currencies: set[str]) -> tuple[dict[str, float], date | None]:
        """Won per one unit of each currency, and the day the rates are from.

        One request per currency: the API quotes from a single base, and a
        base of KRW would give the inverse, which rounds worse at small rates.
        """
        wanted = sorted(currencies - {HOME})
        if not wanted:
            return {HOME: 1.0}, None
        if self._client is not None:
            return await self._fetch(self._client, wanted)
        async with httpx.AsyncClient(base_url=API, timeout=20) as client:
            return await self._fetch(client, wanted)

    @staticmethod
    async def _fetch(
        client: httpx.AsyncClient, wanted: list[str]
    ) -> tuple[dict[str, float], date | None]:
        rates = {HOME: 1.0}
        day: date | None = None
        for currency in wanted:
            try:
                resp = await client.get(
                    "/latest", params={"base": currency, "symbols": HOME}
                )
                resp.raise_for_status()
                body = resp.json()
                rates[currency] = float(body["rates"][HOME])
                day = date.fromisoformat(body["date"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                raise FxError(f"No {currency}/{HOME} rate: {exc}") from exc
        return rates, day
