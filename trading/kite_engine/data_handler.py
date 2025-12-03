import json
import logging
import threading
from kiteconnect import KiteTicker
from django.conf import settings
from django.core.cache import caches
from trading.models import TradeSymbol

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
tick_cache = caches['ticks']

class MarketDataHandler:
    def __init__(self, api_key, access_token):
        self.kws = KiteTicker(api_key, access_token)
        self.monitored_tokens = {}
        
        try:
            symbols = TradeSymbol.objects.filter(symbol__in=settings.MONITORED_SYMBOLS, is_active=True)
            for s in symbols:
                self.monitored_tokens[int(s.instrument_token)] = s
        except: pass

    def start_ticker(self):
        self.kws.on_ticks = self.on_ticks
        self.kws.on_connect = self.on_connect
        self.kws.connect(threaded=True)

    def on_connect(self, ws, response):
        tokens = list(self.monitored_tokens.keys())
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

    def on_ticks(self, ws, ticks):
        for tick in ticks:
            token = tick['instrument_token']
            symbol_obj = self.monitored_tokens.get(token)
            if not symbol_obj: continue

            ltp = tick['last_price']
            ohlc = tick.get('ohlc', {})
            close = ohlc.get('close', ltp)
            high = ohlc.get('high', ltp)
            low = ohlc.get('low', ltp)

            pct_change = ((ltp - close) / close) * 100 if close > 0 else 0
            pct_from_high = ((ltp - high) / high) * 100 if high > 0 else 0
            pct_from_low = ((ltp - low) / low) * 100 if low > 0 else 0

            # --- CRITICAL: STORE METADATA FOR AUTO-RECOVERY ---
            data = {
                'symbol': symbol_obj.symbol,
                'token': str(token),
                'ltp': ltp,
                'prev_close': close,
                'day_high': high,
                'day_low': low,
                'pct_change': pct_change,
                'pct_from_high': pct_from_high,
                'pct_from_low': pct_from_low,
                'is_fno': symbol_obj.symbol in settings.FNO_LIST,
                # Metadata needed to recreate DB entry if missing
                'exchange': symbol_obj.exchange,
                'segment': symbol_obj.segment,
                'lot_size': symbol_obj.absolute_quantity
            }
            
            tick_cache.set(f"tick:{token}", json.dumps(data), timeout=86400)
            
            try:
                from trading.kite_engine.strategy_manager import process_ladder_strategy
                threading.Thread(target=process_ladder_strategy, args=(data,)).start()
            except: pass