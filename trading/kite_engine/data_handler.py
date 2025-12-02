import json
import time
from kiteconnect import KiteTicker
from django.conf import settings
from django.core.cache import caches
from trading.models import TradeSymbol

# Use the dedicated cache for ticks
tick_cache = caches['ticks']

class MarketDataHandler:
    """Manages a single Kite Ticker connection for all active symbols."""
    
    def __init__(self, api_key, access_token):
        self.kws = None
        self.api_key = api_key
        self.access_token = access_token
        self.tokens_to_subscribe = set()
        self.is_connected = False

    def start_ticker(self):
        """Initializes and connects the Kite Ticker."""
        if self.kws:
            self.kws.close()
        
        self.kws = KiteTicker(self.api_key, self.access_token)
        self.kws.on_ticks = self._on_ticks
        self.kws.on_connect = self._on_connect
        self.kws.on_close = self._on_close
        
        # The Ticker must run in a separate thread/process. 
        # For simplicity here, we assume it's run via a management command/worker.
        print("Kite Ticker attempting connection...")
        self.kws.connect(daemon=True)

    def _on_ticks(self, ws, ticks):
        """Callback on receiving real-time ticks. Stores in Redis."""
        for tick in ticks:
            token = tick['instrument_token']
            # Store the latest tick data in the dedicated Redis cache for ticks.
            # Key: 'tick:<token>', Value: JSON string of the tick. TTL: 2 seconds
            tick_cache.set(f'tick:{token}', json.dumps(tick), timeout=2) 
            
            # Note: The strategy manager will read from this cache.

    def _on_connect(self, ws, response):
        """Subscribes to all required tokens upon successful connection."""
        print("Kite Ticker connected.")
        self.is_connected = True
        self.update_subscriptions()
        
    def _on_close(self, ws, code, reason):
        print(f"Kite Ticker closed. Code: {code}, Reason: {reason}")
        self.is_connected = False
        # Add reconnection logic here

    def update_subscriptions(self):
        """Updates the list of subscribed tokens based on active TradeSymbols."""
        new_tokens = set([s.instrument_token for s in TradeSymbol.objects.filter(is_active=True)])
        
        # Tokens to unsubscribe
        tokens_to_unsubscribe = list(self.tokens_to_subscribe - new_tokens)
        if tokens_to_unsubscribe and self.is_connected:
            self.kws.unsubscribe(tokens_to_unsubscribe)
            print(f"Unsubscribed from {len(tokens_to_unsubscribe)} tokens.")
            
        # Tokens to subscribe
        tokens_to_subscribe = list(new_tokens - self.tokens_to_subscribe)
        if tokens_to_subscribe and self.is_connected:
            self.kws.subscribe(tokens_to_subscribe)
            self.kws.set_mode(self.kws.MODE_FULL, tokens_to_subscribe) # MODE_FULL for OHLC/Volume
            print(f"Subscribed to {len(tokens_to_subscribe)} new tokens.")

        self.tokens_to_subscribe = new_tokens

# Note: The actual instantiation and running of this worker must be done
# via a Django management command or a separate script in production.
# For simplicity, we create a placeholder instance that needs a valid token.
# In a real setup, we would need to fetch a valid token from *any* client to initiate this handler.
# For now, let's use placeholder keys.
# handler = MarketDataHandler("COMMON_API_KEY", "VALID_ACCESS_TOKEN") 
# handler.start_ticker()