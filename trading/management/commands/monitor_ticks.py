from django.core.management.base import BaseCommand
from django.conf import settings
import redis
import json
import time
import datetime

class Command(BaseCommand):
    help = 'Monitors Redis for incoming market data ticks'

    def handle(self, *args, **options):
        # Connect directly to Redis DB 1 (where ticks are stored)
        # We use strict redis client to bypass Django cache abstraction for raw inspection
        r = redis.Redis(host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=1)

        self.stdout.write(self.style.SUCCESS('--- Monitoring Redis DB 1 for Ticks ---'))
        self.stdout.write(self.style.WARNING('Press CTRL+C to stop'))

        try:
            while True:
                # Find all keys that look like tick data
                # Django-redis usually adds a prefix like ":1:", so we search for *tick*
                keys = r.keys('*tick:*')
                
                count = len(keys)
                current_time = datetime.datetime.now().strftime("%H:%M:%S")

                if count == 0:
                    print(f"[{current_time}] No tick data found in Redis yet...", end='\r')
                else:
                    print(f"\n[{current_time}] Found {count} active instruments:")
                    
                    for key in keys[:5]: # Show first 5 only to keep screen clean
                        val = r.get(key)
                        if val:
                            try:
                                # Django-redis pickles data by default, but we stored JSON strings.
                                # If we stored pure JSON strings, we can decode them.
                                # If using Django cache.set(), it might be pickled.
                                # Let's try to decode as string first.
                                data_str = val.decode('utf-8')
                                try:
                                    data = json.loads(data_str)
                                    ltp = data.get('last_price', 'N/A')
                                    token = data.get('instrument_token', 'Unknown')
                                    print(f" -> Token: {token} | LTP: {ltp}")
                                except:
                                    # If it's not JSON, it might be raw pickle (Django default)
                                    print(f" -> Key: {key} (Data exists but format is raw)")
                            except Exception as e:
                                print(f" -> Error reading key {key}: {e}")
                    
                    if count > 5:
                        print(f" -> ... and {count - 5} more.")

                time.sleep(1) # Refresh every second

        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS('\nMonitor stopped.'))