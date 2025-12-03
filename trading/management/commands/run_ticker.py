import time
import json
import random
from django.core.management.base import BaseCommand
from django.core.cache import caches
from trading.models import TradeSymbol

class Command(BaseCommand):
    help = 'Simulates market data with Gainers and Losers'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('--- STARTING MOCK TICKER (SIMULATION) ---'))
        
        tick_cache = caches['ticks']
        symbols = TradeSymbol.objects.filter(is_active=True)
        
        if not symbols.exists():
            self.stdout.write(self.style.ERROR('No symbols found! Run "python manage.py sync_symbols" first.'))
            return

        # Initialize mock prices
        mock_data = {}
        for sym in symbols:
            mock_data[sym.instrument_token] = {
                'ltp': 1000.0 + random.randint(-50, 50),
                'close': 1000.0, # Prev Close
                'high': 1000.0,
                'low': 1000.0,
                'symbol': sym.symbol
            }

        self.stdout.write(f"Simulating {len(symbols)} stocks...")

        try:
            while True:
                for token, data in mock_data.items():
                    # Random movement
                    move = random.uniform(-2.0, 2.0) 
                    data['ltp'] += move
                    
                    # Update High/Low
                    if data['ltp'] > data['high']: data['high'] = data['ltp']
                    if data['ltp'] < data['low']: data['low'] = data['ltp']

                    # Calculate % Change (Crucial for Dashboard Ranking)
                    pct_change = ((data['ltp'] - data['close']) / data['close']) * 100
                    pct_from_high = ((data['ltp'] - data['high']) / data['high']) * 100
                    pct_from_low = ((data['ltp'] - data['low']) / data['low']) * 100

                    # Prepare Packet
                    packet = {
                        'symbol': data['symbol'],
                        'token': int(token),
                        'ltp': round(data['ltp'], 2),
                        'prev_close': data['close'],
                        'day_high': round(data['high'], 2),
                        'day_low': round(data['low'], 2),
                        'pct_change': round(pct_change, 2),
                        'pct_from_high': round(pct_from_high, 2),
                        'pct_from_low': round(pct_from_low, 2),
                        'is_fno': False
                    }
                    
                    # Save to Redis
                    tick_cache.set(f"tick:{token}", json.dumps(packet), timeout=86400)

                print(f"Sent updates for {len(symbols)} stocks...", end='\r')
                time.sleep(1) 

        except KeyboardInterrupt:
            self.stdout.write('\nStopped.')