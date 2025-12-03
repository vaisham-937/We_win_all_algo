from django.core.management.base import BaseCommand
from django.conf import settings
from trading.models import ClientAccount, TradeSymbol
from trading.kite_engine.account_manager import kite_session_manager

class Command(BaseCommand):
    help = 'Syncs MONITORED_SYMBOLS from settings.py to Database with correct Tokens'

    def handle(self, *args, **options):
        self.stdout.write("Connecting to Kite to fetch tokens...")

        # 1. Get Active User for API Session
        acc = ClientAccount.objects.filter(access_token__isnull=False).first()
        if not acc:
            self.stdout.write(self.style.ERROR("Error: No logged-in user. Please login to Kite on Dashboard first."))
            return

        kite = kite_session_manager.get_kite_instance(acc.user.id)
        if not kite:
            self.stdout.write(self.style.ERROR("Error: Could not initialize Kite SDK."))
            return

        # 2. Download Master Instrument List
        self.stdout.write("Downloading instrument master list (this takes 5-10 seconds)...")
        instruments = kite.instruments('NSE') # Assuming NSE for now
        
        # Create a lookup dictionary for fast search
        # Format: {'RELIANCE': {'token': 123456, 'segment': 'NSE', 'lot': 1}, ...}
        master_map = {
            i['tradingsymbol']: {
                'token': i['instrument_token'],
                'segment': i['segment'],
                'lot_size': i['lot_size']
            } 
            for i in instruments
        }

        # 3. Loop through Settings List and Save to DB
        count = 0
        for symbol_name in settings.MONITORED_SYMBOLS:
            if symbol_name in master_map:
                data = master_map[symbol_name]
                
                # Update or Create in Database
                obj, created = TradeSymbol.objects.update_or_create(
                    symbol=symbol_name,
                    defaults={
                        'instrument_token': str(data['token']),
                        'exchange': 'NSE',
                        'segment': data['segment'],
                        'absolute_quantity': data['lot_size'],
                        'is_active': True, # Mark as Active for Ticker
                        'price_band_color': 'BLUE' if symbol_name in settings.FNO_LIST else 'GREEN'
                    }
                )
                action = "Created" if created else "Updated"
                self.stdout.write(f" -> {action}: {symbol_name} (Token: {data['token']})")
                count += 1
            else:
                self.stdout.write(self.style.WARNING(f" -> Skipped: {symbol_name} (Not found in NSE list)"))

        self.stdout.write(self.style.SUCCESS(f"\nSuccessfully synced {count} symbols to Database!"))
        self.stdout.write(self.style.SUCCESS("Now restart 'run_ticker' to see data."))