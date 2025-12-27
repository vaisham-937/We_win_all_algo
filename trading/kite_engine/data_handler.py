import json
import logging
from kiteconnect import KiteTicker, KiteConnect
from django.conf import settings
from django_redis import get_redis_connection

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Redis Connection (Raw)
redis_client = get_redis_connection("ticks")

class MarketDataHandler:
    def __init__(self, api_key, access_token):
        self.api_key = api_key
        self.access_token = access_token
        self.kws = KiteTicker(api_key, access_token)
        
        # Internal Maps
        self.tokens_map = {} 
        self.fno_set = set(settings.FNO_LIST)
        self.monitored_set = set(settings.MONITORED_SYMBOLS)

        # 1. FETCH MASTER LIST & MAP TO SETTINGS
        self.initialize_symbols_from_kite()

    def initialize_symbols_from_kite(self):
        """
        Fetches Instrument Master List from Kite.
        """
        logger.info("⬇️ Downloading Master Instrument List from Kite...")
        try:
            kite = KiteConnect(api_key=self.api_key)
            kite.set_access_token(self.access_token)
            
            instruments = kite.instruments() 
            mapped_count = 0
            
            # Clear previous active list in Redis
            redis_client.delete("active_tokens")

            for instr in instruments:
                tradingsymbol = instr['tradingsymbol']
                
                # Check if this symbol is in our Monitored List
                if tradingsymbol in self.monitored_set and instr['exchange'] == 'NSE':
                    token = int(instr['instrument_token'])
                    
                    self.tokens_map[token] = {
                        'symbol': tradingsymbol,
                        'token': token,
                        'exchange': instr['exchange'],
                        'segment': instr['segment'],
                        'lot_size': instr['lot_size'],
                        'is_fno': tradingsymbol in self.fno_set
                    }
                    
                    # Store Token in Redis Set
                    redis_client.sadd("active_tokens", token)
                    mapped_count += 1
            
            logger.info(f"✅ Mapped {mapped_count} symbols from Settings.py")

        except Exception as e:
            logger.error(f"❌ Error fetching instruments: {e}")

    def start_ticker(self):
        if not self.tokens_map:
            logger.error("❌ No symbols mapped! Check settings.MONITORED_SYMBOLS")
            return
        self.kws.on_ticks = self.on_ticks
        self.kws.on_connect = self.on_connect
        self.kws.on_close = self.on_close
        self.kws.on_error = self.on_error
        
        self.kws.connect(threaded=True)
        while True:
            pass

    def on_connect(self, ws, response):
        logger.info("🟢 Connected to Kite Ticker")
        tokens = list(self.tokens_map.keys())
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            logger.info(f"📡 Subscribed to {len(tokens)} tokens in MODE_FULL")

    def on_close(self, ws, code, reason):
        logger.error(f"🔴 Connection Closed: {code} - {reason}")

    def on_error(self, ws, code, reason):
        logger.error(f"⚠️ Ticker Error: {code} - {reason}")

    def on_ticks(self, ws, ticks):
        """Process live ticks, calculate metrics, update Redis state."""
        for tick in ticks:
            token = tick['instrument_token']
            meta = self.tokens_map.get(token)
            if not meta: continue
            ltp = tick['last_price']
            
            # Extract OHLC
            ohlc = tick.get('ohlc', {})
            close = ohlc.get('close', ltp)
            high = ohlc.get('high', ltp)
            low = ohlc.get('low', ltp)
            
            # --- TURNOVER & VOLUME CALCULATION ---
            # Volume is the total quantity traded today
            volume = tick.get('volume_traded', tick.get('volume', 0))
            avg_price = tick.get('average_price', ltp)
            # Turnover = Volume Traded * Average Price
            turnover = (volume * avg_price) if avg_price > 0 else (volume * ltp)

            # 2. PERCENTAGE CALCULATIONS
            pct_change = ((ltp - close) / close) * 100 if close > 0 else 0
            
            # PERCENTAGE FROM DAY LOW
            # Logic: How much has it recovered from the bottom?
            pct_from_low = ((ltp - low) / low) * 100 if low > 0 else 0
            
            pct_from_high = ((ltp - high) / high) * 100 if high > 0 else 0
            
            # Color Logic
            color = 'WHITE'
            if abs(pct_change) >= 20: color = 'DARKGREEN'
            elif abs(pct_change) >= 10: color = 'BROWN'
            elif abs(pct_change) >= 5: color = 'RED'
            
            color_border = 'BLUE' if meta['is_fno'] else 'NONE'

            # Packet Construction
            data_packet = {
                'symbol': meta['symbol'],
                'token': token,
                'ltp': ltp,
                'volume': int(volume),
                'turnover': round(turnover, 2),
                'pct_change': round(pct_change, 2),
                'pct_from_high': round(pct_from_high, 2),
                'pct_from_low': round(pct_from_low, 2),  # <--- STORED HERE
                'color': color,
                'border': color_border,
                'is_fno': meta['is_fno'],
                'upper_circuit_limit': tick.get('upper_circuit_limit', 0), 
                'lower_circuit_limit': tick.get('lower_circuit_limit', 0)
            }
            
            json_packet = json.dumps(data_packet)

            # 1. UPDATE STATE IN REDIS
            redis_client.set(f"tick:{token}", json_packet, ex=86400)
            
            # 2. PUBLISH STREAM
            redis_client.publish("live_ticks", json_packet)
            
            
            # 3. STRATEGY HOOK
            try:
                from trading.kite_engine.strategy_manager import process_ladder_strategy
                process_ladder_strategy(data_packet)
            except Exception as e:
                logger.error(f"Strategy Error: {e}")

