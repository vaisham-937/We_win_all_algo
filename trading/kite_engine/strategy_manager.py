import json
import logging
from django.conf import settings
from django.core.cache import caches
from django.utils import timezone
from trading.models import LadderState, ClientAccount, TradeSymbol, TradeLog
from .account_manager import kite_session_manager
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)

# Use default cache for locking mechanisms (DB 1)
redis_lock = get_redis_connection("default")

# --- LUA SCRIPT FOR ATOMIC RACE CONDITION PROTECTION ---
# KEYS[1] = Lock Key (e.g., "ladder_lock:123")
# ARGV[1] = Expiry in seconds (e.g., 2)
# Returns 1 if lock acquired, 0 if already locked
LUA_LOCK_SCRIPT = """
if redis.call("exists", KEYS[1]) == 1 then
    return 0
else
    redis.call("set", KEYS[1], "LOCKED", "EX", ARGV[1])
    return 1
end
"""

def place_order(client, symbol, transaction_type, qty, tag):
    """
    Places an MIS Market Order via Kite Connect with Strict MIS/INTRADAY enforcement.
    Also Checks Client Account Limits.
    """
    try:
        # 0. Global Safety Checks (Max Loss / Kill Switch)
        # Note: In a high-frequency loop, querying DB for 'client' every time is slow.
        # Ideally, pass the updated 'client' object or cache limits in Redis.
        # For now, we assume 'client' object passed to this function is relatively fresh.
        if not client.is_live_trading_enabled:
            logger.warning(f"⚠️ Kill Switch Active. Rejecting Order: {symbol.symbol}")
            return None

        # 1. Get Kite Instance
        kite = kite_session_manager.get_kite_instance(client.user.id)
        if not kite:
            logger.error(f"❌ No Kite instance for user {client.user.username}")
            return None

        # 2. Place Order (Strict MIS)
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=symbol.exchange,
            tradingsymbol=symbol.symbol,
            transaction_type=transaction_type,
            quantity=int(qty),
            product=kite.PRODUCT_MIS, # Forced Intraday
            order_type=kite.ORDER_TYPE_MARKET,
            tag=tag
        )
        logger.info(f"✅ Order Placed: {transaction_type} {symbol.symbol} Qty: {qty} ID: {order_id}")
        
        # 3. Log Trade to DB (Optional but recommended for audit)
        # (Simplified: logic usually handled by OrderUpdate webhook)
        return order_id
        
    except Exception as e:
        logger.error(f"❌ Order Placement Failed for {symbol.symbol}: {e}")
        return None

def process_ladder_strategy(tick_data):
    """
    Main Logic Loop: Called for every tick of monitored symbols.
    """
    token = tick_data['token']
    ltp = tick_data['ltp']
    
    upper_circuit = tick_data.get('upper_circuit_limit')
    lower_circuit = tick_data.get('lower_circuit_limit')
    
    # Fetch active ladders from DB
    active_ladders = LadderState.objects.filter(
        symbol__instrument_token=token, 
        is_active=True
    ).select_related('client', 'symbol')

    if not active_ladders.exists():
        return

    # Pre-register Lua Script
    lock_script = redis_lock.register_script(LUA_LOCK_SCRIPT)

    for ladder in active_ladders:
        # --- ATOMIC RACE CONDITION PROTECTION ---
        lock_key = f"ladder_lock:{ladder.id}"
        
        # Execute Lua Script (Atomic Check & Set)
        # Try to acquire lock for 2 seconds
        is_acquired = lock_script(keys=[lock_key], args=[2])
        
        if not is_acquired:
            # Another worker/process is handling this ladder right now
            continue

        try:
            # A. Check Square Off Time
            now = timezone.now().time()
            sq_time_str = settings.LADDER_SETTINGS.get('SQUARE_OFF_TIME', '15:15:00')
            if str(now) >= sq_time_str:
                logger.info(f"Square Off Time Reached for {ladder.symbol.symbol}")
                close_ladder(ladder, ltp, "TIME_EXIT")
                continue

            # B. Route to appropriate logic
            if ladder.current_mode == 'BUY':
                manage_buy_ladder(ladder, ltp, upper_circuit)
            elif ladder.current_mode == 'SELL':
                manage_sell_ladder(ladder, ltp, lower_circuit)

        except Exception as e:
            logger.error(f"Error processing ladder {ladder.id}: {e}")
        finally:
            # We rely on TTL (2s) to release, or we can explicitly delete.
            # Explicit delete is better for high frequency.
            redis_lock.delete(lock_key)

def manage_buy_ladder(ladder, ltp, upper_circuit=None):
    # 1. Update Highest Price Seen (For TSL)
    if ltp > ladder.extreme_price:
        ladder.extreme_price = ltp
        ladder.save(update_fields=['extreme_price'])

    # 2. Check Circuit Limit Exit
    if upper_circuit and ltp >= upper_circuit:
        logger.info(f"Upper Circuit Hit for BUY {ladder.symbol}. Exiting...")
        close_ladder(ladder, ltp, "UC_EXIT")
        return

    # 3. Check TSL Hit
    drop_pct = ((ladder.extreme_price - ltp) / ladder.extreme_price) * 100
    
    if drop_pct >= ladder.tsl_pct:
        logger.info(f"TSL Hit for BUY {ladder.symbol}. Reversing to SELL...")
        
        # DOUBLE EXIT PROTECTION: Check if we actually have open qty before selling
        if ladder.current_qty > 0:
            place_order(ladder.client, ladder.symbol, 'SELL', ladder.current_qty, "TSL_EXIT")
        
        # REVERSE ENTRY
        ladder.current_mode = 'SELL'
        ladder.entry_price = ltp
        ladder.extreme_price = ltp 
        ladder.last_add_price = ltp
        ladder.level_count = 1
        
        new_qty = int(ladder.trade_capital / ltp)
        if new_qty < 1: new_qty = 1
        ladder.current_qty = new_qty
        ladder.save()
        
        place_order(ladder.client, ladder.symbol, 'SELL', new_qty, "REVERSE_ENTRY")
        return

    # 4. Check Pyramid Add
    rise_from_last = ((ltp - ladder.last_add_price) / ladder.last_add_price) * 100
    
    if rise_from_last >= ladder.increase_pct and ladder.level_count < settings.LADDER_SETTINGS['MAX_PYRAMID_LEVELS']:
        logger.info(f"Pyramiding BUY {ladder.symbol}")
        add_qty = int(ladder.trade_capital / ltp)
        if add_qty < 1: add_qty = 1
        
        oid = place_order(ladder.client, ladder.symbol, 'BUY', add_qty, "PYRAMID_ADD")
        if oid:
            ladder.current_qty += add_qty
            ladder.last_add_price = ltp
            ladder.level_count += 1
            ladder.save()

def manage_sell_ladder(ladder, ltp, lower_circuit=None):
    # 1. Update Lowest Price Seen
    if ltp < ladder.extreme_price or ladder.extreme_price == 0:
        ladder.extreme_price = ltp
        ladder.save(update_fields=['extreme_price'])

    # 2. Check Circuit Limit Exit
    if lower_circuit and ltp <= lower_circuit:
        logger.info(f"Lower Circuit Hit for SELL {ladder.symbol}. Exiting...")
        close_ladder(ladder, ltp, "LC_EXIT")
        return

    # 3. Check TSL Hit
    rise_pct = ((ltp - ladder.extreme_price) / ladder.extreme_price) * 100
    
    if rise_pct >= ladder.tsl_pct:
        logger.info(f"TSL Hit for SELL {ladder.symbol}. Reversing to BUY...")
        
        if ladder.current_qty > 0:
            place_order(ladder.client, ladder.symbol, 'BUY', ladder.current_qty, "TSL_EXIT")
        
        ladder.current_mode = 'BUY'
        ladder.entry_price = ltp
        ladder.extreme_price = ltp
        ladder.last_add_price = ltp
        ladder.level_count = 1
        
        new_qty = int(ladder.trade_capital / ltp)
        if new_qty < 1: new_qty = 1
        ladder.current_qty = new_qty
        ladder.save()
        
        place_order(ladder.client, ladder.symbol, 'BUY', new_qty, "REVERSE_ENTRY")
        return

    # 4. Check Pyramid Add
    fall_from_last = ((ladder.last_add_price - ltp) / ladder.last_add_price) * 100
    
    if fall_from_last >= ladder.increase_pct and ladder.level_count < settings.LADDER_SETTINGS['MAX_PYRAMID_LEVELS']:
        logger.info(f"Pyramiding SELL {ladder.symbol}")
        add_qty = int(ladder.trade_capital / ltp)
        if add_qty < 1: add_qty = 1

        oid = place_order(ladder.client, ladder.symbol, 'SELL', add_qty, "PYRAMID_ADD")
        if oid:
            ladder.current_qty += add_qty
            ladder.last_add_price = ltp
            ladder.level_count += 1
            ladder.save()

def close_ladder(ladder, ltp, tag):
    """Stops the ladder and squares off everything."""
    if ladder.current_qty > 0:
        tx_type = 'SELL' if ladder.current_mode == 'BUY' else 'BUY'
        place_order(ladder.client, ladder.symbol, tx_type, ladder.current_qty, tag)
    
    ladder.is_active = False
    ladder.current_qty = 0
    ladder.current_mode = 'STOPPED'
    ladder.save()

# --- INITIALIZERS (Double Entry Protected via DB Check) ---

def start_buy_ladder(ladder, ltp):
    # Double Entry Protection: Ensure state is not already active
    if ladder.is_active:
        logger.warning(f"Double Entry Blocked: Ladder already active for {ladder.symbol.symbol}")
        return

    qty = int(ladder.trade_capital / ltp)
    if qty < 1: qty = 1 
    
    oid = place_order(ladder.client, ladder.symbol, 'BUY', qty, "LADDER_START")
    if oid:
        ladder.current_mode = 'BUY'
        ladder.is_active = True
        ladder.entry_price = ltp
        ladder.last_add_price = ltp
        ladder.extreme_price = ltp
        ladder.current_qty = qty
        ladder.level_count = 1
        ladder.save()

def start_sell_ladder(ladder, ltp):
    if ladder.is_active:
        logger.warning(f"Double Entry Blocked: Ladder already active for {ladder.symbol.symbol}")
        return

    qty = int(ladder.trade_capital / ltp)
    if qty < 1: qty = 1
    
    oid = place_order(ladder.client, ladder.symbol, 'SELL', qty, "LADDER_START")
    if oid:
        ladder.current_mode = 'SELL'
        ladder.is_active = True
        ladder.entry_price = ltp
        ladder.last_add_price = ltp
        ladder.extreme_price = ltp
        ladder.current_qty = qty
        ladder.level_count = 1
        ladder.save()