"""Quick standalone check of the BitGo wallet-fetch API.

Loads config/.env (same as the app) and uses a SINGLE BITGO_ACCESS_TOKEN to
query one or more enterprise accounts (XLtokens), then prints their wallets.

Enterprise accounts are discovered from the environment by naming convention:

    BITGO_ACCESS_TOKEN=v2x...          # one token, reaches every enterprise
    BITGO_ENTERPRISE_ID_XL1=6965...    # one line per XLtoken / enterprise
    BITGO_ENTERPRISE_ID_XL2=abcd...

The suffix after BITGO_ENTERPRISE_ID_ (XL1, XL2, ...) is used as the account
label in the output. A bare BITGO_ENTERPRISE_ID (no suffix) is also honored and
labeled "default"; if no enterprise id is set at all, every wallet the token can
see is fetched under the label "all".

Usage:
    python scripts/check_bitgo.py                    # per-wallet, all accounts
    python scripts/check_bitgo.py --by-asset         # aggregate by asset per account
    python scripts/check_bitgo.py --account XL1      # limit to one account
    python scripts/check_bitgo.py --raw              # also dump raw wallet JSON
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Load config/.env exactly like navdash/settings.py does, so we don't depend on
# the caller having exported the vars (and it handles CRLF / quoting for us).
from dotenv import load_dotenv

# override=True so the file wins over any stale value already exported into the
# shell (e.g. a prior `set -a; . config/.env` that captured CRLF line endings).
load_dotenv(BASE_DIR / "config" / ".env", override=True)

from tracker.scrapers.bitgo import (
    _to_amount,
    aggregate_holdings,
    fetch_all_wallets,
    normalize_symbol,
)

_ENTERPRISE_PREFIX = "BITGO_ENTERPRISE_ID_"


def discover_accounts() -> list[tuple[str, str]]:
    """Return [(label, enterprise_id), ...] discovered from the environment.

    Every BITGO_ENTERPRISE_ID_<NAME> var becomes one account labeled <NAME>.
    Falls back to a bare BITGO_ENTERPRISE_ID ("default"), or to no enterprise
    filter at all ("all") when nothing is configured.
    """
    accounts: list[tuple[str, str]] = []
    for key, value in os.environ.items():
        if key.startswith(_ENTERPRISE_PREFIX):
            ent = (value or "").strip()
            if ent:
                accounts.append((key[len(_ENTERPRISE_PREFIX):], ent))

    if not accounts:
        bare = os.environ.get("BITGO_ENTERPRISE_ID", "").strip()
        accounts.append(("default" if bare else "all", bare))

    accounts.sort(key=lambda a: a[0])
    return accounts


def _wallet_assets(wallet: dict) -> list[tuple[str, float]]:
    """Return [(symbol, amount), ...] for a wallet's native coin + tokens."""
    assets: list[tuple[str, float]] = []
    symbol = normalize_symbol(wallet.get("coin"))
    amount = _to_amount(wallet.get("balanceString"), symbol)
    if amount:
        assets.append((symbol, amount))
    for token_name, token in (wallet.get("tokens") or {}).items():
        tsym = normalize_symbol(token.get("coin") or token_name)
        tamount = _to_amount(token.get("balanceString"), tsym)
        if tamount:
            assets.append((tsym, tamount))
    return assets


def _print_by_wallet(wallets: list[dict]) -> None:
    """One block per wallet: label/id, coin, and its per-asset balances."""
    for wallet in wallets:
        label = wallet.get("label") or "(no label)"
        wid = wallet.get("id", "")
        coin = wallet.get("coin", "")
        print(f"  {label}  [{coin}]  {wid}")
        assets = _wallet_assets(wallet)
        if not assets:
            print("      (empty)")
        for sym, amt in assets:
            print(f"      {sym:<10}{amt:>22.8f}")


def _print_by_asset(wallets: list[dict]) -> None:
    """Aggregate balances across the account's wallets by asset."""
    result = aggregate_holdings(wallets)
    print(f"  {'SYMBOL':<10}{'AMOUNT':>22}{'USD':>16}")
    print("  " + "-" * 48)
    for h in result["holdings"]:
        usd = "-" if h["usd"] is None else f"{h['usd']:,.2f}"
        print(f"  {h['symbol']:<10}{h['amount']:>22.8f}{usd:>16}")
    print("  " + "-" * 48)
    print(f"  {'TOTAL (priced only)':<32}{result['total_usd']:>16,.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the BitGo wallet fetch API.")
    parser.add_argument("--raw", action="store_true", help="dump raw wallet JSON")
    parser.add_argument(
        "--by-asset", action="store_true",
        help="aggregate balances by asset per account (default: per wallet)",
    )
    parser.add_argument(
        "--account", metavar="NAME", default="",
        help="limit to a single account label (e.g. XL1)",
    )
    args = parser.parse_args()

    token = os.environ.get("BITGO_ACCESS_TOKEN", "").strip()
    if not token:
        print("ERROR: BITGO_ACCESS_TOKEN is not set in the environment.", file=sys.stderr)
        return 1

    base_url = (os.environ.get("BITGO_BASE_URL", "") or "https://app.bitgo.com").strip()

    accounts = discover_accounts()
    if args.account:
        accounts = [a for a in accounts if a[0] == args.account]
        if not accounts:
            print(f"ERROR: no account labeled {args.account!r}. "
                  f"Set {_ENTERPRISE_PREFIX}{args.account} in .env.", file=sys.stderr)
            return 1

    print(f"Base URL: {base_url}")
    print(f"Accounts: {', '.join(label for label, _ in accounts)}\n")

    exit_code = 0
    raw_by_account: dict[str, list[dict]] = {}
    for label, enterprise_id in accounts:
        header = f"=== {label} (enterprise={enterprise_id or 'all'}) ==="
        print(header)
        try:
            wallets = fetch_all_wallets(
                access_token=token,
                base_url=base_url,
                enterprise_id=enterprise_id,
            )
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
            print(f"  FAILED: {exc}\n", file=sys.stderr)
            exit_code = 1
            continue

        raw_by_account[label] = wallets
        print(f"  {len(wallets)} wallet(s)")
        if args.by_asset:
            _print_by_asset(wallets)
        else:
            _print_by_wallet(wallets)
        print()

    if args.raw:
        print(json.dumps(raw_by_account, indent=2, default=str))

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
