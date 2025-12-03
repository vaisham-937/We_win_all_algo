import json
import logging
from django.conf import settings
from django.core.cache import caches
from django.utils import timezone
from trading.models import LadderState, ClientAccount, TradeSymbol
from .account_manager import kite_session_manager

logger = logging.getLogger(__name__)

# Use default cache for locking mechanisms
redis_client = caches['default']

def place_order(client, symbol, transaction_type, qty, tag):
    """
    Places an MIS Market Order via Kite Connect.
    """
    try:
        kite = kite_session_manager.get_kite_instance(client.user.id)
        if not kite:
            logger.error(f"No Kite instance for user {client.user.username}")
            return None

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
        logger.info(f"Order Placed: {transaction_type} {symbol.symbol} Qty: {qty} ID: {order_id}")
        return order_id
    except Exception as e:
        logger.error(f"Order Placement Failed: {e}")
        return None

def process_ladder_strategy(tick_data):
    """
    Main Logic Loop: Called for every tick of monitored symbols.
    Checks if any active ladder needs action (Add Qty, TSL Exit, or Circuit Exit).
    """
    token = tick_data['token']
    ltp = tick_data['ltp']
    
    # Circuit Limits (if available in tick data)
    upper_circuit = tick_data.get('upper_circuit_limit')
    lower_circuit = tick_data.get('lower_circuit_limit')
    
    # 1. Find ACTIVE ladders for this symbol
    active_ladders = LadderState.objects.filter(
        symbol__instrument_token=token, 
        is_active=True
    ).select_related('client', 'symbol')

    if not active_ladders.exists():
        return

    for ladder in active_ladders:
        # --- RACE CONDITION PROTECTION ---
        lock_key = f"ladder_lock:{ladder.id}"
        if not redis_client.add(lock_key, "LOCKED", timeout=2): 
            # If lock exists, skip this tick to prevent double processing
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
            redis_client.delete(lock_key)

def manage_buy_ladder(ladder, ltp, upper_circuit=None):
    # 1. Update Highest Price Seen (For TSL Calculation)
    if ltp > ladder.extreme_price:
        ladder.extreme_price = ltp
        ladder.save(update_fields=['extreme_price'])

    # 2. Check Circuit Limit Exit (New Requirement)
    # If price hits Upper Circuit, we assume we should exit profit
    if upper_circuit and ltp >= upper_circuit:
        logger.info(f"Upper Circuit Hit for BUY {ladder.symbol}. Exiting...")
        close_ladder(ladder, ltp, "UC_EXIT")
        return

    # 3. Check TSL Hit (Exit & Reverse Condition)
    # Logic: If price drops X% from the Highest Point
    drop_pct = ((ladder.extreme_price - ltp) / ladder.extreme_price) * 100
    
    if drop_pct >= ladder.tsl_pct:
        logger.info(f"TSL Hit for BUY {ladder.symbol}. Reversing to SELL...")
        
        # A. Square Off Current Buy Position
        place_order(ladder.client, ladder.symbol, 'SELL', ladder.current_qty, "TSL_EXIT")
        
        # B. Start Reverse Sell Ladder
        # We reset state and immediately enter Sell
        ladder.current_mode = 'SELL'
        ladder.entry_price = ltp
        ladder.extreme_price = ltp # Reset high/low tracking for new leg
        ladder.last_add_price = ltp
        ladder.level_count = 1
        
        # Determine new quantity based on capital
        new_qty = int(ladder.trade_capital / ltp)
        if new_qty < 1: new_qty = 1
        
        ladder.current_qty = new_qty
        ladder.save()
        
        # Place Entry Sell Order
        place_order(ladder.client, ladder.symbol, 'SELL', new_qty, "REVERSE_ENTRY")
        return

    # 4. Check Pyramid Add (Add on Rise)
    # Logic: If price rises X% from the LAST Entry/Add price
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
    # 1. Update Lowest Price Seen (For TSL Calculation)
    if ltp < ladder.extreme_price or ladder.extreme_price == 0:
        ladder.extreme_price = ltp
        ladder.save(update_fields=['extreme_price'])

    # 2. Check Circuit Limit Exit (New Requirement)
    # If price hits Lower Circuit, we exit short position
    if lower_circuit and ltp <= lower_circuit:
        logger.info(f"Lower Circuit Hit for SELL {ladder.symbol}. Exiting...")
        close_ladder(ladder, ltp, "LC_EXIT")
        return

    # 3. Check TSL Hit (Exit & Reverse Condition)
    # Logic: If price rises X% from the Lowest Point
    rise_pct = ((ltp - ladder.extreme_price) / ladder.extreme_price) * 100
    
    if rise_pct >= ladder.tsl_pct:
        logger.info(f"TSL Hit for SELL {ladder.symbol}. Reversing to BUY...")
        
        # A. Square Off Current Sell Position
        place_order(ladder.client, ladder.symbol, 'BUY', ladder.current_qty, "TSL_EXIT")
        
        # B. Start Reverse Buy Ladder
        ladder.current_mode = 'BUY'
        ladder.entry_price = ltp
        ladder.extreme_price = ltp
        ladder.last_add_price = ltp
        ladder.level_count = 1
        
        new_qty = int(ladder.trade_capital / ltp)
        if new_qty < 1: new_qty = 1
        
        ladder.current_qty = new_qty
        ladder.save()
        
        # Place Entry Buy Order
        place_order(ladder.client, ladder.symbol, 'BUY', new_qty, "REVERSE_ENTRY")
        return

    # 4. Check Pyramid Add (Add on Fall)
    # Logic: If price falls X% from the LAST Entry/Add price
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
    """Initializes a Buy Ladder from the Dashboard Trigger."""
    qty = int(ladder.trade_capital / ltp)
    if qty < 1: qty = 1 
    
    oid = place_order(ladder.client, ladder.symbol, 'BUY', qty, "LADDER_START")
    if oid:
        ladder.current_mode = 'BUY'
        ladder.is_active = True
        ladder.entry_price = ltp
        ladder.last_add_price = ltp
        ladder.extreme_price = ltp # Start TSL tracking from entry
        ladder.current_qty = qty
        ladder.level_count = 1
        ladder.save()

def start_sell_ladder(ladder, ltp):
    """Initializes a Sell Ladder from the Dashboard Trigger."""
    qty = int(ladder.trade_capital / ltp)
    if qty < 1: qty = 1
    
    oid = place_order(ladder.client, ladder.symbol, 'SELL', qty, "LADDER_START")
    if oid:
        ladder.current_mode = 'SELL'
        ladder.is_active = True
        ladder.entry_price = ltp
        ladder.last_add_price = ltp
        ladder.extreme_price = ltp # Start TSL tracking from entry
        ladder.current_qty = qty
        ladder.level_count = 1
        ladder.save()