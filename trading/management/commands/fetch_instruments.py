import json
from django.core.management.base import BaseCommand
from django.core.cache import cache
from trading.models import ClientAccount
from trading.kite_engine.account_manager import kite_session_manager

class Command(BaseCommand):
    help = 'Fetches all instruments from Kite and stores them in Redis for searching'

    def handle(self, *args, **options):
        self.stdout.write("Connecting to Kite to fetch instruments master list...")

        # 1. Get an active user to use their session
        active_account = ClientAccount.objects.filter(access_token__isnull=False).first()
        if not active_account:
            self.stdout.write(self.style.ERROR("Error: No logged-in client found. Please login to Kite first."))
            return

        try:
            # 2. Initialize Kite
            kite = kite_session_manager.get_kite_instance(active_account.user.id)
            if not kite:
                self.stdout.write(self.style.ERROR("Could not get Kite instance."))
                return

            # 3. Fetch Instruments (This downloads a large CSV/List)
            self.stdout.write("Downloading instruments list (this may take a few seconds)...")
            instruments = kite.instruments() # Fetches ALL exchanges (NSE, BSE, NFO, MCX)

            # 4. Filter and Format for Redis
            # We only want NSE/BSE Equity and NFO (Futures/Options)
            # Storing everything is too heavy, so we keep only what's needed for search.
            master_list = []
            count = 0
            
            for instr in instruments:
                if instr['exchange'] in ['NSE', ]:
                    master_list.append({
                        'symbol': instr['tradingsymbol'],
                        'name': instr.get('name', ''),
                        'token': instr['instrument_token'],
                        'exchange': instr['exchange'],
                        'segment': instr['segment'],
                        'lot_size': instr['lot_size']
                    })
                    count += 1

            # 5. Save to Redis (Cache timeout: 24 hours)
            # We compress it slightly by dumping to JSON
            cache.set('master_instruments_list', json.dumps(master_list), timeout=86400)

            self.stdout.write(self.style.SUCCESS(f"Successfully fetched and cached {count} instruments in Redis."))

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Failed to fetch instruments: {e}"))