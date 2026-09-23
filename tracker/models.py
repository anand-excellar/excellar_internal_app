from django.db import models


class Snapshot(models.Model):
    timestamp = models.DateTimeField()
    segment = models.CharField(max_length=10)
    wallet_address = models.CharField(max_length=100, default="")
    btc_price = models.FloatField(null=True, blank=True)
    eth_price = models.FloatField(null=True, blank=True)
    aave_supply_amount = models.FloatField(null=True, blank=True)
    aave_supply_usd = models.FloatField(null=True, blank=True)
    aave_borrow_usdc = models.FloatField(null=True, blank=True)
    aave_health_rate = models.FloatField(null=True, blank=True)
    aave_net_usd = models.FloatField(null=True, blank=True)
    aave_profit = models.FloatField(null=True, blank=True)
    susds_eth_balance = models.FloatField(null=True, blank=True)
    susds_arbi_balance = models.FloatField(null=True, blank=True)
    # ERC-4626 vault positions (e.g. Gauntlet USDC Prime): USD total, plus the
    # per-vault detail the NAV catalog/report renders as line items.
    vault_usd = models.FloatField(null=True, blank=True)
    vault_positions_json = models.TextField(null=True, blank=True)
    hl_deposit_usdc = models.FloatField(null=True, blank=True)
    # Hyperliquid "available to trade" (withdrawable margin) — equity minus
    # margin currently locked by open positions.
    hl_available_usdc = models.FloatField(null=True, blank=True)
    # USDC collateral / account equity held on Binance (from the exchange API).
    binance_collateral_usdc = models.FloatField(null=True, blank=True)
    # Binance free/available USDC (equity minus margin locked by positions).
    binance_available_usdc = models.FloatField(null=True, blank=True)
    # Deribit account equity in USD (summed across per-currency wallets; equity
    # already folds in unrealized options/futures PnL). Enters NAV like HL/Binance.
    deribit_equity_usd = models.FloatField(null=True, blank=True)
    # Deribit free/available collateral in USD (equity minus margin locked).
    deribit_available_usd = models.FloatField(null=True, blank=True)
    # JSON of per-venue exchange asset balances:
    # {"Binance": [{coin, amount, price, usd, free}], "Hyperliquid": [...],
    #  "Deribit": [...]}.
    exchange_balances_json = models.TextField(null=True, blank=True)
    wallet_balance_usd = models.FloatField(null=True, blank=True)
    # Free native-asset balance held in the tracked wallet (the "Native reserve").
    wallet_native_amount = models.FloatField(null=True, blank=True)
    # JSON list of perp legs across venues: [{venue, symbol, side, size,
    # entry_price, mark_price, notional, unrealized_pnl}]. Populated by the collector.
    perp_positions_json = models.TextField(null=True, blank=True)
    # JSON of aggregated BitGo custody holdings across all wallets:
    # {"holdings": [{symbol, amount, price, usd}], "total_usd": float}.
    bitgo_holdings_json = models.TextField(null=True, blank=True)
    funding_cumulative = models.FloatField(null=True, blank=True)
    price_diff_hl_bn = models.FloatField(null=True, blank=True)
    cross_mtm_value = models.FloatField(null=True, blank=True)
    pnl_usdc = models.FloatField(null=True, blank=True)
    pnl_native = models.FloatField(null=True, blank=True)
    pnl_pct = models.FloatField(null=True, blank=True)
    portfolio_value = models.FloatField(null=True, blank=True)
    annualized_return = models.FloatField(null=True, blank=True)
    raw_debank_json = models.TextField(null=True, blank=True)
    scrape_success = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["segment", "timestamp"])]

    def __str__(self):
        return f"Snapshot({self.segment}, {self.timestamp})"


class DailyPnl(models.Model):
    date = models.DateField()
    segment = models.CharField(max_length=10)
    pnl_usdc = models.FloatField(null=True, blank=True)
    pnl_native = models.FloatField(null=True, blank=True)
    pnl_pct = models.FloatField(null=True, blank=True)
    portfolio_value = models.FloatField(null=True, blank=True)
    annualized_return = models.FloatField(null=True, blank=True)
    funding_total = models.FloatField(null=True, blank=True)
    rolling_max = models.FloatField(null=True, blank=True)
    drawdown = models.FloatField(null=True, blank=True)
    asset_price = models.FloatField(null=True, blank=True)
    price_diff_hl_bn = models.FloatField(null=True, blank=True)
    aave_borrow_value = models.FloatField(null=True, blank=True)
    aave_profit_value = models.FloatField(null=True, blank=True)
    susds_eth_value = models.FloatField(null=True, blank=True)
    susds_arbi_value = models.FloatField(null=True, blank=True)
    cross_mtm_value = models.FloatField(null=True, blank=True)
    snapshot = models.ForeignKey(
        Snapshot, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("date", "segment")]
        indexes = [models.Index(fields=["segment", "date"])]

    def __str__(self):
        return f"DailyPnl({self.segment}, {self.date})"


class CrossMtmConfig(models.Model):
    asset = models.CharField(max_length=10)
    entry_cost = models.FloatField()
    position_size = models.FloatField()
    effective_from = models.DateTimeField()
    effective_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["asset", "effective_until"])]

    def __str__(self):
        return f"CrossMtmConfig({self.asset}, from={self.effective_from})"


class PortfolioDaily(models.Model):
    date = models.DateField(unique=True)
    total_value_usd = models.FloatField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PortfolioDaily({self.date})"
