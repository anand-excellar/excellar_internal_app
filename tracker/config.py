from dataclasses import dataclass, field
from typing import Dict, Any
from django.conf import settings


@dataclass
class Token:
    key: str
    address: str = ""
    debank_url: str = ""
    base_asset: str = ""
    initial_portfolio_value: float = 0.0
    funding_account: str = "btc"
    enabled: bool = False
    price_symbol: str = ""
    cross_mtm: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.raw or {})
        d.update({
            "address": self.address,
            "debank_url": self.debank_url,
            "base_asset": self.base_asset,
            "initial_portfolio_value": self.initial_portfolio_value,
            "funding_account": self.funding_account,
            "enabled": self.enabled,
            "price_symbol": self.price_symbol,
            "cross_mtm": self.cross_mtm,
        })
        return d


def load_tokens(django_settings=None) -> Dict[str, Token]:
    """Build a registry of tokens from settings.

    Supports a preferred `tokens:` top-level key in config.yaml. Falls back
    to the legacy `wallets:` key for backwards compatibility.
    """
    if django_settings is None:
        django_settings = settings

    raw_tokens = getattr(django_settings, "NAV_TOKENS", None) or getattr(django_settings, "NAV_WALLETS", {})
    cross_cfg = getattr(django_settings, "NAV_CROSS_MTM", {})
    exchange_cfg = getattr(django_settings, "NAV_EXCHANGE", {})

    tokens: Dict[str, Token] = {}
    for key, cfg in (raw_tokens or {}).items():
        base_asset = (cfg.get("base_asset") or cfg.get("display_symbol") or "").upper()
        # Determine price symbol: explicit override, otherwise pick sensible default
        price_symbol = cfg.get("price_symbol")
        if not price_symbol:
            if base_asset == "BTC":
                price_symbol = exchange_cfg.get("btc_price_symbol")
            elif base_asset == "ETH":
                price_symbol = exchange_cfg.get("eth_price_symbol")
            else:
                price_symbol = exchange_cfg.get("funding_symbol") or exchange_cfg.get("btc_price_symbol")

        token = Token(
            key=key,
            address=cfg.get("address", ""),
            debank_url=cfg.get("debank_url", ""),
            base_asset=base_asset,
            initial_portfolio_value=cfg.get("initial_portfolio_value", 0.0),
            funding_account=cfg.get("funding_account", "btc"),
            enabled=cfg.get("enabled", False),
            price_symbol=price_symbol,
            cross_mtm=cross_cfg.get(base_asset, {}),
            raw=cfg,
        )
        tokens[key] = token

    return tokens


def enabled_tokens(django_settings=None) -> Dict[str, Token]:
    return {k: v for k, v in load_tokens(django_settings).items() if v.enabled}
