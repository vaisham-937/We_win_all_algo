from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone

# --- 1. Client Credentials and System Configuration ---

class ClientAccount(models.Model):
    """Stores Kite Connect credentials and user-specific system settings."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, primary_key=True)
    phone_number = models.CharField(max_length=15, null=True, blank=True)
    is_phone_verified = models.BooleanField(default=False)
    is_email_verified = models.BooleanField(default=False)

    # Kite Credentials (Per Client)
    api_key = models.CharField(max_length=100)
    api_secret = models.CharField(max_length=100)
    access_token = models.CharField(max_length=256, null=True, blank=True)
    
    # Kill Switch and Approval (Requirement 1 & 19)
    is_live_trading_enabled = models.BooleanField(default=False, verbose_name="Client Kill Switch")
    is_broker_approved = models.BooleanField(default=False, verbose_name="Broker/Admin Approved")
    
    # Max P&L/Loss Limits (Requirement 1)
    max_daily_profit = models.FloatField(default=10000.00, verbose_name="Max Profit to Stop Trading")
    max_daily_loss = models.FloatField(default=-5000.00, verbose_name="Max Loss to Stop Trading")

    # Time Constraints (Requirement 18, 21)
    no_new_entry_after = models.TimeField(default=timezone.make_aware(timezone.datetime(2000, 1, 1, 15, 0, 0)).time(), verbose_name="No New Entry After")
    square_off_time = models.TimeField(default=timezone.make_aware(timezone.datetime(2000, 1, 1, 15, 15, 0)).time(), verbose_name="Daily Square Off Time")

    def __str__(self):
        return f"Account for {self.user.username}"

# --- 2. Trade Symbol Configuration ---

class TradeSymbol(models.Model):
    """Defines the tradable scrips and their specific strategy settings."""
    # Note: TradeSymbol is not directly linked to ClientAccount.
    # The list of scrips should ideally be common, but the positions are client-specific.
    symbol = models.CharField(max_length=50, unique=True) # E.g., RELIANCE, NIFTY24SEP19500CE
    instrument_token = models.CharField(max_length=20, unique=True, help_text="Zerodha Instrument Token")
    exchange = models.CharField(max_length=10, choices=[('NSE', 'NSE'), ('BSE', 'BSE')])
    segment = models.CharField(max_length=10, choices=[('EQ', 'Equity'), ('FUT', 'FUT'), ('OPT', 'OPT')], default='EQ')

    # Quantity/Exposure (Requirement 2)
    qty_type = models.CharField(max_length=10, choices=[('ABS', 'Absolute'), ('FORMULA', 'Formula')], default='ABS')
    absolute_quantity = models.IntegerField(default=1) 
    exposure_formula = models.TextField(blank=True, null=True, help_text="Exposure formula based on capital/margin.")

    # Price Band Info (Requirement 17)
    # The requirement is visual, but storing the type helps the engine decide order type.
    price_band_color = models.CharField(max_length=10, default='GREEN', choices=[
        ('BLUE', 'Blue F&O (1% order rule)'), 
        ('GREEN', '20% (Equity)'), 
        ('BROWN', '10% (Equity)'), 
        ('RED', '5% (Equity)')
    ])

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.symbol
    
    class Meta:
        verbose_name_plural = "Trade Symbols (Scrips)"

# --- 3. Trade Log and Position Tracking ---

class TradeLog(models.Model):
    """Tracks every executed trade and its current open/closed status."""
    client_account = models.ForeignKey(ClientAccount, on_delete=models.CASCADE)
    symbol = models.ForeignKey(TradeSymbol, on_delete=models.CASCADE)
    
    # Execution Details
    trade_type = models.CharField(max_length=4, choices=[('BUY', 'Buy'), ('SELL', 'Sell')])
    quantity = models.IntegerField()
    status = models.CharField(max_length=20, default='OPEN', choices=[('OPEN', 'Open'), ('CLOSED', 'Closed'), ('CANCELLED', 'Cancelled')])
    
    # Entry Details
    entry_time = models.DateTimeField(auto_now_add=True)
    entry_price = models.FloatField()
    
    # Exit Details (P&L Tracking - Requirement 1)
    exit_time = models.DateTimeField(null=True, blank=True)
    exit_price = models.FloatField(null=True, blank=True)
    realized_pnl = models.FloatField(default=0.0)

    # Strategy Targets (Requirement 4, 5)
    # Stores the calculated T1-T10, S1-S10, and TSL_Y1-Y10 levels.
    targets = models.JSONField(default=dict, help_text="Calculated T1-T10, S1-S10, TSL levels.")
    
    # Kite Order IDs
    entry_order_id = models.CharField(max_length=50, blank=True)
    squareoff_order_id = models.CharField(max_length=50, blank=True, null=True)

    def __str__(self):
        return f"[{self.client_account.user.username}] {self.trade_type} {self.symbol.symbol} ({self.status})"


class LadderState(models.Model):
    MODE_CHOICES = [('BUY', 'Buy Ladder'), ('SELL', 'Sell Ladder'), ('STOPPED', 'Stopped')]
    ENTRY_TYPE_CHOICES = [('QUANTITY', 'Fixed Quantity'), ('CAPITAL', 'Fixed Capital')]
    LADDER_TYPES = (
    ('MARKET', 'Market Ladder'),      # Gainers / Losers
    ('CHARTINK', 'Chartink Ladder'),   # Alerts
)
    ladder_type = models.CharField(
        max_length=20,
        choices=LADDER_TYPES,
        default='MARKET'
    )

    client = models.ForeignKey('ClientAccount', on_delete=models.CASCADE)
    symbol = models.ForeignKey('TradeSymbol', on_delete=models.CASCADE)
    
    is_active = models.BooleanField(default=False)
    current_mode = models.CharField(max_length=10, choices=MODE_CHOICES, default='STOPPED')
    
    # --- Entry Mode Configuration ---
    entry_type = models.CharField(
        max_length=10, 
        choices=ENTRY_TYPE_CHOICES, 
        default='CAPITAL',
        help_text="Choose if the initial order is based on quantity or total capital"
    )
    fixed_quantity = models.IntegerField(default=0, help_text="Used if entry_type is QUANTITY")
    trade_capital = models.FloatField(default=10000.0, help_text="Used if entry_type is CAPITAL")
    
    # --- Dynamic State Tracking ---
    entry_price = models.FloatField(default=0.0)
    last_add_price = models.FloatField(default=0.0)
    extreme_price = models.FloatField(default=0.0)
    current_qty = models.IntegerField(default=0)
    level_count = models.IntegerField(default=0)
    
    # --- Strategy Parameters ---
    increase_pct = models.FloatField(default=1.0)
    tsl_pct = models.FloatField(default=1.0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('client', 'symbol')

    def __str__(self):
        return f"{self.symbol.symbol} - {self.current_mode}"
    

class ChartinkAlert(models.Model):
    """Stores alerts received from Chartink via Webhook."""
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    scan_name = models.CharField(max_length=255)
    stocks = models.TextField(help_text="Comma-separated list of symbols")
    trigger_price = models.FloatField(default=0.0)
    timestamp = models.DateTimeField(default=timezone.now)
    is_processed = models.BooleanField(default=False)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.scan_name} at {self.timestamp}"

