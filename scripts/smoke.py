"""Authenticated public-market smoke check; plus paper capability flag."""
from __future__ import annotations

import argparse
import getpass
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="Module 2 public-market runtime integration smoke check")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--markets", default="spot,futures", help="Configured markets")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT", help="Configured symbols")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=15) as client:
        for path in ("/healthz", "/readyz"):
            response = client.get(path)
            print(f"{path}: HTTP {response.status_code}")
            response.raise_for_status()
        username = input("Username: ").strip()
        password = getpass.getpass("Password: ")
        code = getpass.getpass("Current 6-digit authenticator code: ")
        response = client.post("/v1/auth/login", json={"username": username, "password": password, "totp": code})
        response.raise_for_status()
        token = response.json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        result = {}
        for path in ("/v1/auth/me", "/v1/trading/capabilities", "/v1/market/instruments",
                     "/v1/market/streams", "/v1/market/latest", "/v1/market/data-quality"):
            response = client.get(path, headers=headers)
            print(f"{path}: HTTP {response.status_code}")
            response.raise_for_status()
            result[path] = response.json()
        caps = result["/v1/trading/capabilities"]
        assert not caps["live_trading_enabled"] and not caps["spot_orders"] and not caps["futures_orders"] and caps["paper_orders"]
        streams = result["/v1/market/streams"]["streams"]
        events = result["/v1/market/latest"]["events"]
        instruments = result["/v1/market/instruments"]["instruments"]
        gaps = result["/v1/market/data-quality"]["unresolved_gaps"]
        print(f"Summary: {len(streams)} streams; {len(events)} event slots; "
              f"{len(instruments)} instruments; {len(gaps)} unresolved agg-trade gaps")
        for item in streams:
            print(f"  {item['market']}: healthy={item['healthy']}, reconnects={item['reconnects']}")
        if not streams or any(not s["healthy"] for s in streams):
            print("FAIL: at least one data feed is stale/disconnected")
            return 2
        configured_markets = [m.strip() for m in args.markets.split(",") if m.strip()]
        configured_symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        expected = {(m, s, k) for m in configured_markets for s in configured_symbols
                    for k in (("agg_trade", "kline", "book_ticker", "mark_price")
                              if m == "futures" else ("agg_trade", "kline", "book_ticker"))}
        active = {(e["market"], e["symbol"], e["event_type"]) for e in events if not e["stale"]}
        missing = expected - active
        known_instruments = {(i["market"], i["symbol"]) for i in instruments}
        missing_metadata = {(m, s) for m in configured_markets for s in configured_symbols} - known_instruments
        if missing or missing_metadata:
            print(f"FAIL: missing/stale expected streams: {sorted(missing)}; missing metadata: {sorted(missing_metadata)}")
            return 3
        if gaps:
            print("WARN: unresolved aggregate-trade gaps, investigate before treating history as complete")
        print("PASS: public-market feeds, virtual-only capability, authenticated endpoints")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (httpx.RequestError, httpx.HTTPStatusError, AssertionError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
