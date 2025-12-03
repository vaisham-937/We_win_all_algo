import json
import logging
from django.conf import settings
from django.core.cache import caches
from django.utils import timezone
from trading.models import LadderState, ClientAccount, TradeSymbol
from .account_manager import kite_session_manager
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)

# Use default cache for locking mechanisms (DB 1)
# Note: Ensure this matches where you want locks. usually 'default' is fine.
redis_lock = get_redis_connection("default")

def place_order(client, symbol, transaction_type, qty, tag):
    """
    Places an MIS Market Order via Kite Connect.
    """
    try:
        # 1. Get Kite Instance (Using Updated Session Manager)
        # This will now look for "access_token:{user_id}" in Raw Redis
        kite = kite_session_manager.get_kite_instance(client.user.id)
        
        if not kite:
            logger.error(f"❌ No Kite instance for user {client.user.username} (Check Redis Keys)")
            return None

        # 2. Place Order
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=symbol.exchange,
            tradingsymbol=symbol.symbol,
            transaction_type=transaction_type,
            quantity=int(qty),
            product=kite.PRODUCT_MIS,
            order_type=kite.ORDER_TYPE_MARKET,
            tag=tag
        )
        logger.info(f"✅ Order Placed: {transaction_type} {symbol.symbol} Qty: {qty} ID: {order_id}")
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
    
    # Circuit Limits (if available)
    upper_circuit = tick_data.get('upper_circuit_limit')
    lower_circuit = tick_data.get('lower_circuit_limit')
    
    # 1. Find ACTIVE ladders for this symbol
    # We fetch from DB because Strategy State is persistent
    active_ladders = LadderState.objects.filter(
        symbol__instrument_token=token, 
        is_active=True
    ).select_related('client', 'symbol')

    if not active_ladders.exists():
        return

    for ladder in active_ladders:
        # --- RACE CONDITION PROTECTION ---
        # Lock Key: "ladder_lock:123"
        lock_key = f"ladder_lock:{ladder.id}"
        
        # Use raw redis .set(..., nx=True, ex=2) for robust locking
        if not redis_lock.set(lock_key, "LOCKED", nx=True, ex=2): 
            # If lock exists (set returns False), skip this tick
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
            # Release lock
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
        
        # Square Off Buy
        place_order(ladder.client, ladder.symbol, 'SELL', ladder.current_qty, "TSL_EXIT")
        
        # Reverse to Sell
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
        
        # Square Off Sell
        place_order(ladder.client, ladder.symbol, 'BUY', ladder.current_qty, "TSL_EXIT")
        
        # Reverse to Buy
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

# --- INITIALIZERS ---

def start_buy_ladder(ladder, ltp):
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