from tracker.config import load_tokens


class DummySettings:
    NAV_TOKENS = {
        "xlTEST": {
            "address": "0x1234567890abcdef1234567890abcdef12345678",
            "debank_url": "https://debank.com/profile/0x1234567890abcdef1234567890abcdef12345678",
            "base_asset": "BTC",
            "initial_portfolio_value": 0.5,
            "funding_account": "btc",
            "enabled": True,
            "price_symbol": "BTC/USDC:USDC",
        }
    }
    NAV_CROSS_MTM = {
        "BTC": {"entry_cost": 0.1, "position_size": 0.01, "direction": 1}
    }
    NAV_EXCHANGE = {
        "btc_price_symbol": "BTC/USDC:USDC",
        "eth_price_symbol": "ETH/USDC:USDC",
        "funding_symbol": "BTC/USDC:USDC",
    }


class LegacyDummySettings:
    NAV_WALLETS = {
        "xlLEGACY": {
            "address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "debank_url": "https://debank.com/profile/0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "base_asset": "ETH",
            "initial_portfolio_value": 2.0,
            "funding_account": "btc",
            "enabled": True,
        }
    }
    NAV_CROSS_MTM = {
        "ETH": {"entry_cost": 0.2, "position_size": 0.02, "direction": 1}
    }
    NAV_EXCHANGE = {
        "btc_price_symbol": "BTC/USDC:USDC",
        "eth_price_symbol": "ETH/USDC:USDC",
        "funding_symbol": "BTC/USDC:USDC",
    }


def test_load_tokens_from_nav_tokens():
    tokens = load_tokens(DummySettings)
    assert "xlTEST" in tokens
    token = tokens["xlTEST"]
    assert token.address.startswith("0x1234")
    assert token.base_asset == "BTC"
    assert token.price_symbol == "BTC/USDC:USDC"
    assert token.cross_mtm == {"entry_cost": 0.1, "position_size": 0.01, "direction": 1}


def test_load_tokens_falls_back_to_legacy_wallets():
    tokens = load_tokens(LegacyDummySettings)
    assert "xlLEGACY" in tokens
    token = tokens["xlLEGACY"]
    assert token.base_asset == "ETH"
    assert token.price_symbol == "ETH/USDC:USDC"
    assert token.cross_mtm == {"entry_cost": 0.2, "position_size": 0.02, "direction": 1}
