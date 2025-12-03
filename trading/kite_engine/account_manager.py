import logging
from kiteconnect import KiteConnect
from django.conf import settings
from trading.models import ClientAccount
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)

# 1. Initialize Raw Redis Connection (DB 1 - Default)
# We use this to save/get raw keys like "access_token:1" without Django's internal versioning prefixes.
redis_db = get_redis_connection("default")

class KiteSessionManager:
    """
    Handles KiteConnect session generation and retrieval per client.
    """
    
    def __init__(self):
        # Base KiteConnect instance for general utility
        self.base_kite = KiteConnect(api_key="TEMP_KEY")

    def get_login_url(self, api_key):
        """Generates the Kite login URL using the client's API Key."""
        self.base_kite.api_key = api_key
        return self.base_kite.login_url()

    def generate_session(self, user, request_token):
        """Generates the access token after successful login and saves to Redis."""
        try:
            # 1. Fetch Credentials from DB
            account = ClientAccount.objects.get(user=user)
            kite = KiteConnect(api_key=account.api_key)
            
            # 2. Exchange Request Token for Access Token
            data = kite.generate_session(request_token, api_secret=account.api_secret)
            access_token = data.get("access_token")
            
            if access_token:
                # 3. UPDATE DATABASE
                account.access_token = access_token
                account.save()
                
                # 4. SAVE TO REDIS (RAW KEYS)
                # Use redis_db.set() to store raw strings.
                # Expiry: 86400 seconds (24 hours)
                redis_db.set(f"access_token:{user.id}", access_token, ex=86400)
                redis_db.set(f"api_key:{user.id}", account.api_key, ex=86400)
                
                logger.info(f"✅ User {user.username}: Tokens saved to Redis (DB 1) [RAW MODE]")
                return True
            
            logger.error(f"Login failed: No access token received for {user.username}")
            return False

        except Exception as e:
            logger.error(f"Login Exception for {user.username}: {e}")
            return False

    def get_kite_instance(self, user_id):
        """
        Retrieves the active KiteConnect instance from Redis (Raw).
        """
        try:
            # 1. Get from Redis (Returns bytes)
            token_bytes = redis_db.get(f"access_token:{user_id}")
            key_bytes = redis_db.get(f"api_key:{user_id}")

            if token_bytes and key_bytes:
                # 2. Decode bytes to string
                access_token = token_bytes.decode("utf-8")
                api_key = key_bytes.decode("utf-8")

                # 3. Create Instance
                kite = KiteConnect(api_key=api_key)
                kite.set_access_token(access_token)
                return kite
                
        except Exception as e:
            logger.error(f"Error restoring Kite instance for user {user_id}: {e}")
            
        return None

kite_session_manager = KiteSessionManager()