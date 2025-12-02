from django.contrib import admin
from django.utils.html import format_html
from .models import ClientAccount, TradeSymbol, TradeLog

# --- 1. CLIENT ACCOUNT ADMIN ---
@admin.register(ClientAccount)
class ClientAccountAdmin(admin.ModelAdmin):
    # लिस्ट में ये कॉलम दिखेंगे
    list_display = (
        'user_info', 
        'phone_number', 
        'status_badge', 
        'broker_approval',
        'pnl_limits',
        'last_login'
    )
    
    # सर्च बार (Username, Email, Phone या API Key से ढूँढें)
    search_fields = ('user__username', 'user__email', 'phone_number', 'api_key')
    
    # साइडबार फिल्टर्स
    list_filter = ('is_live_trading_enabled', 'is_broker_approved', 'is_phone_verified')
    
    # एडमिन के अंदर फॉर्म का लेआउट
    fieldsets = (
        ('Personal Info', {
            'fields': ('user', 'phone_number', 'is_phone_verified')
        }),
        ('Trading Controls', {
            'fields': ('is_live_trading_enabled', 'is_broker_approved'),
            'description': "Enable/Disable trading for this client manually."
        }),
        ('Risk Management', {
            'fields': ('max_daily_profit', 'max_daily_loss', 'no_new_entry_after', 'square_off_time')
        }),
        ('API Credentials', {
            'fields': ('api_key', 'api_secret', 'access_token'),
            'classes': ('collapse',), # यह सेक्शन डिफ़ॉल्ट रूप से बंद रहेगा
        }),
    )

    # --- CUSTOM COLUMNS FOR LIST DISPLAY ---
    
    def user_info(self, obj):
        return f"{obj.user.username} ({obj.user.email})"
    user_info.short_description = "Client Name & Email"

    def status_badge(self, obj):
        if obj.is_live_trading_enabled:
            return format_html('<span style="color:white; background:green; padding:3px 8px; border-radius:10px;">Active</span>')
        return format_html('<span style="color:white; background:red; padding:3px 8px; border-radius:10px;">Stopped</span>')
    status_badge.short_description = "Trading Status"

    def broker_approval(self, obj):
        return obj.is_broker_approved
    broker_approval.boolean = True # यह टिक/क्रॉस आइकन दिखाएगा

    def pnl_limits(self, obj):
        return f"+{obj.max_daily_profit} / {obj.max_daily_loss}"
    pnl_limits.short_description = "Max Profit / Loss"
    
    def last_login(self, obj):
        return obj.user.last_login
    last_login.short_description = "Last Active"


# --- 2. TRADE SYMBOL ADMIN ---
@admin.register(TradeSymbol)
class TradeSymbolAdmin(admin.ModelAdmin):
    list_display = ('symbol', 'exchange', 'segment', 'qty_type', 'absolute_quantity', 'is_active')
    list_filter = ('exchange', 'segment', 'is_active')
    search_fields = ('symbol', 'instrument_token')
    list_editable = ('is_active', 'absolute_quantity') # लिस्ट में ही एडिट करें


# --- 3. TRADE LOG ADMIN ---
@admin.register(TradeLog)
class TradeLogAdmin(admin.ModelAdmin):
    list_display = ('client_account', 'symbol', 'trade_type', 'quantity', 'entry_price', 'pnl_display', 'status', 'entry_time')
    list_filter = ('status', 'trade_type', 'entry_time')
    search_fields = ('client_account__user__username', 'symbol__symbol')
    
    def pnl_display(self, obj):
        if obj.realized_pnl > 0:
            return format_html('<b style="color:green;">+{}</b>', obj.realized_pnl)
        elif obj.realized_pnl < 0:
            return format_html('<b style="color:red;">{}</b>', obj.realized_pnl)
        return obj.realized_pnl
    pnl_display.short_description = "Realized P&L"