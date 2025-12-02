import json
import time
from django.conf import settings
from django.core.cache import caches
from django.db.models import Sum
from trading.models import ClientAccount, TradeSymbol, TradeLog
from .account_manager import kite_session_manager
from django.utils import timezone

tick_cache = caches['ticks']

def get_tick_size(exchange, segment):
    """Returns the tick size for rounding based on exchange/segment."""
    return settings.TICK_SIZES.get(exchange, 0.05)

def round_to_tick(price, tick):
    """Rounds a price to the nearest tick size."""
    if tick > 0:
        return round(round(price / tick) * tick, 2)
    return price

def calculate_strategy_levels(token, last_price):
    """
    Calculates entry, targets (T1-T10), and initial TSL (Y1) based on 810%/910% rules.
    """
    try:
        symbol_obj = TradeSymbol.objects.get(instrument_token=token)
    except TradeSymbol.DoesNotExist:
        return None

    tick_size = get_tick_size(symbol_obj.exchange, symbol_obj.segment)
    
    # Mock Low/High - In production, this would come from the screener/previous day's data
    low_of_day = last_price * 0.98  # Example: 2% below LTP
    high_of_day = last_price * 1.02 # Example: 2% above LTP
    range_value = high_of_day - low_of_day
    
    # --- 810%/910% Signal Calculation (Requirement 13) ---
    buy_entry_price = low_of_day + (range_value * 0.810)
    sell_entry_price = high_of_day - (range_value * 0.910)

    buy_entry_price = round_to_tick(buy_entry_price, tick_size)
    sell_entry_price = round_to_tick(sell_entry_price, tick_size)
    
    # --- Targets and TSL Calculation (Requirement 4, 5, 6) ---
    targets = {}
    target_step = range_value * 0.05 # Example: 5% of the range as target step

    # Calculate Buy Targets (T1-T10)
    for i in range(1, 11):
        target_price = buy_entry_price + (target_step * i)
        targets[f'T{i}'] = round_to_tick(target_price, tick_size)
        
    # Calculate Sell Targets (S1-S10)
    for i in range(1, 11):
        target_price = sell_entry_price - (target_step * i)
        targets[f'S{i}'] = round_to_tick(target_price, tick_size)
        
    # Initial TSL (Y1) - Assuming 50% of the range below entry as initial SL
    initial_sl_buy = buy_entry_price - (range_value * 0.50)
    initial_sl_sell = sell_entry_price + (range_value * 0.50)
    
    targets['TSL_Y1_BUY'] = round_to_tick(initial_sl_buy, tick_size)
    targets['TSL_Y1_SELL'] = round_to_tick(initial_sl_sell, tick_size)
    
    return {
        'buy_entry': buy_entry_price,
        'sell_entry': sell_entry_price,
        'targets': targets,
    }

def calculate_quantity(symbol_obj):
    """Determines order quantity based on ABS or FORMULA (Requirement 2)."""
    if symbol_obj.qty_type == 'ABS':
        return symbol_obj.absolute_quantity
    # Formula logic would require external data (e.g., current capital)
    # For this blueprint, we only use absolute quantity.
    return symbol_obj.absolute_quantity 


def place_kite_order(kite, symbol_obj, trade_type, quantity, price=0.0):
    """
    Places an order using the client's kite instance.
    Implements order type logic based on price band/segment (Requirement 17).
    """
    # For F&O (Blue band), the first 2 buy and 4 sell orders are MARKET (Requirement 16)
    # This logic is complex and requires daily count tracking.
    
    # Simplistic implementation: all fresh entries are MARKET, all exits are LIMIT/SL-M
    order_type = kite.ORDER_TYPE_MARKET
    
    try:
        # Example order placement
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=symbol_obj.exchange,
            tradingsymbol=symbol_obj.symbol,
            transaction_type=trade_type, # BUY or SELL
            quantity=quantity,
            product=kite.PRODUCT_MIS,
            order_type=order_type,
            price=price if order_type == kite.ORDER_TYPE_LIMIT else 0,
            tag='WE_ALL_WIN_ALGO'
        )
        print(f"Order placed successfully. ID: {order_id}")
        return order_id
    except Exception as e:
        print(f"Order placement failed for {symbol_obj.symbol}: {e}")
        return None

def check_and_manage_client(client_account):
    """
    Main loop function to run the strategy for a single client.
    """
    user_id = client_account.user.id
    username = client_account.user.username
    print(f"\n--- Checking Strategy for Client: {username} ---")

    # 1. Check Global Kill Switches
    if not client_account.is_live_trading_enabled:
        print(f"Client {username}: Trading is disabled (Kill Switch). Skipping.")
        return
        
    if not client_account.access_token:
        print(f"Client {username}: No valid access token. Skipping.")
        return

    # 2. Get Kite Instance
    kite = kite_session_manager.get_kite_instance(user_id)
    if not kite:
        print(f"Client {username}: Could not get Kite instance. Skipping.")
        return
        
    # 3. Check Max Profit/Loss Limits
    # This requires querying the P&L for today, which would involve API calls or DB checks.
    # We will assume a function `get_current_day_pnl(user_id)` exists.
    # current_pnl = get_current_day_pnl(user_id)
    current_pnl = TradeLog.objects.filter(
        client_account=client_account, 
        exit_time__date=timezone.now().date()
    ).aggregate(Sum('realized_pnl'))['realized_pnl__sum'] or 0.0

    if current_pnl >= client_account.max_daily_profit:
        print(f"Client {username}: Max Profit limit hit. Disabling trading.")
        client_account.is_live_trading_enabled = False
        client_account.save()
        return
    elif current_pnl <= client_account.max_daily_loss:
        print(f"Client {username}: Max Loss limit hit. Disabling trading.")
        client_account.is_live_trading_enabled = False
        client_account.save()
        return

    # 4. Iterate over all active TradeSymbols
    active_symbols = TradeSymbol.objects.filter(is_active=True)
    
    for symbol_obj in active_symbols:
        token = symbol_obj.instrument_token
        tick_data_json = tick_cache.get(f'tick:{token}')
        
        if not tick_data_json:
            continue
        
        tick_data = json.loads(tick_data_json)
        last_price = tick_data.get('last_price', 0)
        
        if last_price == 0:
            continue

        levels = calculate_strategy_levels(token, last_price)
        if not levels:
            continue

        # --- A. Manage OPEN Positions (TSL/Square Off) ---
        open_trades = TradeLog.objects.filter(
            client_account=client_account, 
            symbol=symbol_obj, 
            status='OPEN'
        )
        
        for trade in open_trades:
            # TSL and Target Check
            # This is complex and requires updating the active TSL level (Y1 -> Y2 -> ...)
            # For blueprint, we check only initial SL (TSL_Y1)
            is_buy = trade.trade_type == 'BUY'
            current_sl_key = 'TSL_Y1_BUY' if is_buy else 'TSL_Y1_SELL'
            current_sl = trade.targets.get(current_sl_key, 0)
            
            tsl_hit = (is_buy and last_price <= current_sl) or (not is_buy and last_price >= current_sl)
            
            if tsl_hit:
                # Square off due to TSL (Requirement 6)
                print(f"Client {username}: TSL Hit on {symbol_obj.symbol}. Squaring off.")
                exit_type = 'SELL' if is_buy else 'BUY'
                place_kite_order(kite, symbol_obj, exit_type, trade.quantity, price=last_price)
                
                trade.status = 'CLOSED'
                trade.exit_price = last_price
                trade.exit_time = timezone.now()
                # PnL calculation: Entry - Exit * Qty (Reversed for SELL)
                trade.realized_pnl = (last_price - trade.entry_price) * trade.quantity * (1 if is_buy else -1)
                trade.save()
                
            # Target Hit Logic (T1-T10) would be here, leading to TSL updates and partial square-offs.
            # Example: if is_buy and last_price >= trade.targets.get('T1'): # Update TSL logic
            

        # --- B. Check for NEW Entry Signal ---
        if not open_trades.exists():
            
            # Check for no new entry time (Requirement 18)
            now_time = timezone.now().time()
            if now_time >= client_account.no_new_entry_after:
                continue

            quantity = calculate_quantity(symbol_obj)
            new_trade_log = None
            
            # BUY Signal
            if last_price >= levels['buy_entry']:
                print(f"Client {username}: BUY Signal for {symbol_obj.symbol} at {last_price}")
                order_id = place_kite_order(kite, symbol_obj, 'BUY', quantity)
                if order_id:
                    new_trade_log = TradeLog(
                        client_account=client_account, symbol=symbol_obj, trade_type='BUY',
                        entry_price=last_price, quantity=quantity, entry_order_id=order_id,
                        targets=levels['targets']
                    )
            
            # SELL Signal
            elif last_price <= levels['sell_entry']:
                print(f"Client {username}: SELL Signal for {symbol_obj.symbol} at {last_price}")
                order_id = place_kite_order(kite, symbol_obj, 'SELL', quantity)
                if order_id:
                    new_trade_log = TradeLog(
                        client_account=client_account, symbol=symbol_obj, trade_type='SELL',
                        entry_price=last_price, quantity=quantity, entry_order_id=order_id,
                        targets=levels['targets']
                    )

            if new_trade_log:
                new_trade_log.save()


def run_multi_client_strategy_loop():
    """
    The main infinite loop that processes all clients sequentially.
    This function should be executed by a dedicated worker (e.g., Celery or a management command).
    """
    print("\n\n#############################################")
    print("Multi-Client Strategy Loop Started...")
    print("#############################################")
    
    # Placeholder: In a real environment, you would first ensure the MarketDataHandler is running.
    # Fetch a client's credentials to initialize the data handler if it's not running.
    # e.g., handler = MarketDataHandler(api_key, token); handler.start_ticker()

    while True:
        try:
            # Fetch all accounts that are ENABLED
            active_accounts = ClientAccount.objects.filter(is_live_trading_enabled=True)
            
            for account in active_accounts:
                check_and_manage_client(account)
                
            time.sleep(1) # Check all clients every 1 second
            
        except Exception as e:
            print(f"FATAL ERROR in Main Strategy Loop: {e}")
            time.sleep(5)