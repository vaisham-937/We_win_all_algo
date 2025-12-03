# import json
# import logging
# import threading
# from kiteconnect import KiteTicker
# from django.conf import settings
# from django.core.cache import caches
# from trading.models import TradeSymbol

# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)
# tick_cache = caches['ticks']

# class MarketDataHandler:
#     def __init__(self, api_key, access_token):
#         self.kws = KiteTicker(api_key, access_token)
#         self.monitored_tokens = {}
        
#         try:
#             symbols = TradeSymbol.objects.filter(symbol__in=settings.MONITORED_SYMBOLS, is_active=True)
#             for s in symbols:
#                 self.monitored_tokens[int(s.instrument_token)] = s
#         except: pass

#     def start_ticker(self):
#         self.kws.on_ticks = self.on_ticks
#         self.kws.on_connect = self.on_connect
#         self.kws.connect(threaded=True)

#     def on_connect(self, ws, response):
#         tokens = list(self.monitored_tokens.keys())
#         if tokens:
#             ws.subscribe(tokens)
#             ws.set_mode(ws.MODE_FULL, tokens)

#     def on_ticks(self, ws, ticks):
#         for tick in ticks:
#             token = tick['instrument_token']
#             symbol_obj = self.monitored_tokens.get(token)
#             if not symbol_obj: continue

#             ltp = tick['last_price']
#             ohlc = tick.get('ohlc', {})
#             close = ohlc.get('close', ltp)
#             high = ohlc.get('high', ltp)
#             low = ohlc.get('low', ltp)
#             print(f"🔴 REAL DATA FROM KITE: {symbol_obj.symbol} = ₹{ltp}")

#             pct_change = ((ltp - close) / close) * 100 if close > 0 else 0
#             pct_from_high = ((ltp - high) / high) * 100 if high > 0 else 0
#             pct_from_low = ((ltp - low) / low) * 100 if low > 0 else 0

#             # --- CRITICAL: STORE METADATA FOR AUTO-RECOVERY ---
#             data = {
#                 'symbol': symbol_obj.symbol,
#                 'token': str(token),
#                 'ltp': ltp,
#                 'prev_close': close,
#                 'day_high': high,
#                 'day_low': low,
#                 'pct_change': pct_change,
#                 'pct_from_high': pct_from_high,
#                 'pct_from_low': pct_from_low,
#                 'is_fno': symbol_obj.symbol in settings.FNO_LIST,
#                 # Metadata needed to recreate DB entry if missing
#                 'exchange': symbol_obj.exchange,
#                 'segment': symbol_obj.segment,
#                 'lot_size': symbol_obj.absolute_quantity
#             }
            
#             tick_cache.set(f"tick:{token}", json.dumps(data), timeout=86400)
            
#             try:
#                 from trading.kite_engine.strategy_manager import process_ladder_strategy
#                 threading.Thread(target=process_ladder_strategy, args=(data,)).start()
#             except: pass



import json
import logging
from kiteconnect import KiteTicker
from django.conf import settings
from django.core.cache import caches
from trading.models import TradeSymbol

logger = logging.getLogger(__name__)
# Use the 'ticks' cache defined in settings
redis_client = caches['ticks']

class MarketDataHandler:
    def __init__(self, api_key, access_token):
        self.kws = KiteTicker(api_key, access_token)
        # Load tokens from DB based on settings.MONITORED_SYMBOLS
        self.tokens_map = {} # {token: symbol_name}
        self.fno_set = set(settings.FNO_LIST)
        
        self.initialize_symbols()

    def initialize_symbols(self):
        # Sync DB with Settings list first (simplified logic)
        for sym in settings.MONITORED_SYMBOLS:
            # Assuming you have a way to fetch tokens (e.g., from a master file)
            # For now, let's assume TradeSymbol DB is populated
            try:
                obj = TradeSymbol.objects.get(symbol=sym)
                self.tokens_map[int(obj.instrument_token)] = obj
            except TradeSymbol.DoesNotExist:
                pass
        
    def start_ticker(self):
        self.kws.on_ticks = self.on_ticks
        self.kws.on_connect = self.on_connect
        self.kws.connect(threaded=True)

    def on_connect(self, ws, response):
        tokens = list(self.tokens_map.keys())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        logger.info(f"Subscribed to {len(tokens)} tokens")

    def on_ticks(self, ws, ticks):
        for tick in ticks:
            token = tick['instrument_token']
            symbol_obj = self.tokens_map.get(token)
            
            if not symbol_obj: continue

            ltp = tick['last_price']
            ohlc = tick.get('ohlc', {})
            close = ohlc.get('close', ltp) # Prev Close
            high = ohlc.get('high', ltp)
            low = ohlc.get('low', ltp)
            
            # --- CALCULATIONS FOR DASHBOARD ---
            pct_change = ((ltp - close) / close) * 100 if close > 0 else 0
            pct_from_high = ((ltp - high) / high) * 100 if high > 0 else 0
            pct_from_low = ((ltp - low) / low) * 100 if low > 0 else 0
            
            is_fno = symbol_obj.symbol in self.fno_set
            
            # Rank Color Logic
            color = 'WHITE'
            if abs(pct_change) >= 20: color = 'DARKGREEN'
            elif abs(pct_change) >= 10: color = 'BROWN'
            elif abs(pct_change) >= 5: color = 'RED'
            
            # F&O Highlight
            if is_fno: color_border = 'BLUE' 
            else: color_border = 'NONE'

            data_packet = {
                'symbol': symbol_obj.symbol,
                'token': token,
                'ltp': ltp,
                'pct_change': round(pct_change, 2),
                'pct_from_high': round(pct_from_high, 2),
                'pct_from_low': round(pct_from_low, 2),
                'color': color,
                'border': color_border,
                'is_fno': is_fno,
                # CRITICAL: Pass Circuit Limits for Strategy
                'upper_circuit_limit': tick.get('upper_circuit_limit'), 
                'lower_circuit_limit': tick.get('lower_circuit_limit')
            }

            # Save to Redis
            redis_client.set(f"tick:{token}", json.dumps(data_packet), timeout=86400)
           # --- TRIGGER STRATEGY ENGINE ---
            # FIX: Calling with the full data dictionary, not separate arguments
            try:
                from trading.kite_engine.strategy_manager import process_ladder_strategy
                process_ladder_strategy(data_packet)
            except Exception as e:
                logger.error(f"Strategy Error for {symbol_obj.symbol}: {e}")