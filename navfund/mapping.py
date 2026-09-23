"""Resolve a NAV Fund Services account to our Detail-tab category and location.

The mapping itself is data, in ``account_map.yaml`` — this module only loads it,
validates it, and answers lookups.

Account names are matched **case- and whitespace-insensitively**. NAV's own
casing is inconsistent for the same account across endpoints and reports
(``XLBTC HOT WALLET ON ARBITRUM ETH GAS FEE`` vs ``XLBTC Hot Wallet on
Arbitrum``), and at least one of our BitGo labels carries a stray leading space,
so exact string equality would fail for reasons that have nothing to do with the
data being right.
"""
import logging
import os
from functools import lru_cache

import yaml

logger = logging.getLogger(__name__)

MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "account_map.yaml")


class MappingError(ValueError):
    """The mapping file is internally inconsistent."""


def normalize_key(name) -> str:
    """Collapse case and whitespace so lookups survive NAV's inconsistency."""
    return " ".join(str(name or "").split()).casefold()


@lru_cache(maxsize=1)
def load_map(path: str = MAP_FILE) -> dict:
    """Parse and validate ``account_map.yaml``.

    Validation is deliberate: a typo'd category or a mapping pointing at a
    segment that isn't declared would otherwise surface much later as rows
    quietly landing in the wrong place.
    """
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    categories = set(raw.get("categories") or [])
    if not categories:
        raise MappingError(f"{path}: no `categories` declared")

    segments = raw.get("segments") or {}
    if not segments:
        raise MappingError(f"{path}: no `segments` declared")

    index = {}
    for segment, cfg in segments.items():
        for account, entry in (cfg.get("accounts") or {}).items():
            category = entry.get("category")
            location = entry.get("location")
            if category not in categories:
                raise MappingError(
                    f"{path}: {segment}/{account!r} has category {category!r}, "
                    f"which is not one of {sorted(categories)}")
            if not location:
                raise MappingError(f"{path}: {segment}/{account!r} has no location")

            overrides = {}
            for tick, over in (entry.get("ticker_overrides") or {}).items():
                over_category = over.get("category", category)
                if over_category not in categories:
                    raise MappingError(
                        f"{path}: {segment}/{account!r} ticker override {tick!r} has "
                        f"category {over_category!r}, which is not one of {sorted(categories)}")
                overrides[normalize_key(tick)] = {
                    "category": over_category,
                    "location": over.get("location", location),
                    "our_asset": over.get("our_asset"),
                }

            key = normalize_key(account)
            if key in index:
                raise MappingError(
                    f"{path}: account {account!r} is mapped twice "
                    f"({index[key]['segment']} and {segment})")
            index[key] = {
                "segment": segment,
                "nav_account": account,
                "category": category,
                "location": location,
                "tickers": {normalize_key(k): v for k, v in (entry.get("tickers") or {}).items()},
                "ticker_overrides": overrides,
            }

    return {
        "categories": categories,
        "segments": segments,
        "index": index,
        "unmapped_notes": raw.get("unmapped_notes") or {},
    }


def segment_for_fund(fund_name: str, path: str = MAP_FILE) -> str | None:
    """Our segment key (``xlBTC``) for a NAV fund name (``XLBTC SA``)."""
    wanted = normalize_key(fund_name)
    for segment, cfg in load_map(path)["segments"].items():
        if normalize_key(cfg.get("fund_name")) == wanted:
            return segment
    return None


def fund_ids(path: str = MAP_FILE) -> dict:
    """``{segment: global_fund_id}`` for the segments we reconcile."""
    return {seg: cfg.get("global_fund_id")
            for seg, cfg in load_map(path)["segments"].items()}


def resolve(nav_account: str, ticker: str = "", path: str = MAP_FILE) -> dict | None:
    """Map one NAV (account, ticker) onto our taxonomy.

    Returns ``{segment, category, location, nav_account, our_asset}``, or ``None``
    when the account is not mapped — callers must surface those rather than drop
    them (see ``unmapped_notes`` in the YAML for the ones we know about).
    """
    entry = load_map(path)["index"].get(normalize_key(nav_account))
    if entry is None:
        return None

    # NAV books a wallet's native gas coin as ETH_GAS/BTC_GAS; the per-account
    # rename says what we call it (ARBETH on Arbitrum, ETH on mainnet).
    key = normalize_key(ticker)
    resolved = {
        "segment": entry["segment"],
        "category": entry["category"],
        "location": entry["location"],
        "nav_account": entry["nav_account"],
        "our_asset": entry["tickers"].get(key, ticker),
    }

    # A single NAV account can hold tickers we file under different categories
    # (Spark's sUSDS lives inside their Arbitrum wallet account).
    override = entry["ticker_overrides"].get(key)
    if override:
        resolved["category"] = override["category"]
        resolved["location"] = override["location"]
        if override["our_asset"]:
            resolved["our_asset"] = override["our_asset"]
    return resolved


def unmapped_note(nav_account: str, ticker: str = "", path: str = MAP_FILE) -> str:
    """Our recorded reason for leaving an account unmapped, if we have one."""
    notes = load_map(path)["unmapped_notes"]
    for candidate in (f"{nav_account} / {ticker}", nav_account):
        for key, note in notes.items():
            if normalize_key(key) == normalize_key(candidate):
                return " ".join(str(note).split())
    return ""