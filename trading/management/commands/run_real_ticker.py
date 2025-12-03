import time
import logging
from django.core.management.base import BaseCommand
from django_redis import get_redis_connection
from trading.kite_engine.data_handler import MarketDataHandler

# Setup Logging
logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Starts the REAL KiteConnect WebSocket Ticker using Redis Credentials (No DB)'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('--- INITIALIZING REAL KITE TICKER (REDIS MODE) ---'))
        
        # 1. Connect to 'default' Redis (DB 1) where TOKENS are stored
        # DO NOT use "ticks" cache here, as that is DB 2
        try:
            session_redis = get_redis_connection("default")
            
            # Scan for keys starting with "access_token:"
            # Redis returns a LIST of bytes: [b"access_token:1", b"access_token:14"]
            active_session_keys = session_redis.keys("access_token:*")
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Redis Connection Error: {e}"))
            return

        if not active_session_keys:
            self.stdout.write(self.style.ERROR('❌ No active session found in Redis (DB 1)!'))
            self.stdout.write(self.style.WARNING('   -> Please login to Kite on the Dashboard to save credentials.'))
            return

        # 2. AUTO-SELECT THE FIRST ACTIVE USER
        # We take the first key found. 
        # key_bytes is bytes (b'access_token:1'), we need to decode it to string
        token_key_bytes = active_session_keys[0]
        token_key_str = token_key_bytes.decode('utf-8') # "access_token:1"
        
        # Extract User ID (assuming format "access_token:{user_id}")
        try:
            user_id = token_key_str.split(":")[-1]
        except IndexError:
            self.stdout.write(self.style.ERROR(f"Invalid key format: {token_key_str}"))
            return
        
        # 3. FETCH CREDENTIALS
        # Keys for API Key and Access Token
        # We use the session_redis connection we established above
        api_key_bytes = session_redis.get(f"api_key:{user_id}")
        access_token_bytes = session_redis.get(f"access_token:{user_id}")

        if not api_key_bytes or not access_token_bytes:
            self.stdout.write(self.style.ERROR(f'❌ Found key {token_key_str}, but corresponding values are missing.'))
            return

        # Decode bytes to strings for KiteConnect
        api_key = api_key_bytes.decode('utf-8')
        access_token = access_token_bytes.decode('utf-8')

        self.stdout.write(self.style.SUCCESS(f"✅ Found Active Session!"))
        self.stdout.write(f"   -> User ID:   {user_id}")
        self.stdout.write(f"   -> API Key:   {api_key}")
        self.stdout.write(f"   -> Token:     {access_token[:10]}... (masked)")

        # 4. START THE TICKER
        # This will internally connect to 'ticks' Redis to publish data
        self.stdout.write(self.style.SUCCESS('--- STARTING WEBSOCKET STREAM ---'))
        try:
            handler = MarketDataHandler(api_key, access_token)
            handler.start_ticker()
        except KeyboardInterrupt:
            self.stdout.write(self.style.SUCCESS("\nTicker Stopped by User."))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Ticker Crash: {e}"))