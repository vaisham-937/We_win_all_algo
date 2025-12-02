from kiteconnect import KiteConnect
from django.conf import settings
from trading.models import ClientAccount
from django.core.cache import cache

# Use the default cache for storing access tokens
REDIS_KEY_PREFIX = "kite_session:"

class KiteSessionManager:
    """Handles KiteConnect session generation and retrieval per client."""
    
    def __init__(self):
        # Base KiteConnect instance for general utility (e.g., getting login URL)
        self.base_kite = KiteConnect(api_key="TEMP_KEY") # Key is temporary, replaced during login

    def get_login_url(self, api_key):
        """Generates the Kite login URL using the client's API Key."""
        self.base_kite.api_key = api_key
        return self.base_kite.login_url()

    def generate_session(self, user, request_token):
        """Generates the access token after successful login."""
        try:
            account = ClientAccount.objects.get(user=user)
            kite = KiteConnect(api_key=account.api_key)
            data = kite.generate_session(request_token, api_secret=account.api_secret)
            access_token = data.get("access_token")
            
            # 1. Store the new access token in the database
            account.access_token = access_token
            account.save()
            
            # 2. Store the KiteConnect object in Redis for quick access by the engine
            cache.set(REDIS_KEY_PREFIX + str(user.id), kite, timeout=None) # No expiry
            
            print(f"User {user.username} - Kite login successful. Token stored.")
            return True
        except ClientAccount.DoesNotExist:
            print(f"Error: ClientAccount not found for user {user.username}.")
            return False
        except Exception as e:
            print(f"User {user.username} - Kite login failed: {e}")
            return False

    def get_kite_instance(self, user_id):
        """Retrieves the active KiteConnect instance from Redis."""
        kite = cache.get(REDIS_KEY_PREFIX + str(user_id))
        if kite:
            return kite
        
        # If not in cache, try to create from DB token (useful after restart)
        try:
            account = ClientAccount.objects.get(user_id=user_id)
            if account.access_token:
                kite = KiteConnect(api_key=account.api_key)
                kite.set_access_token(account.access_token)
                # Cache it now
                cache.set(REDIS_KEY_PREFIX + str(user_id), kite, timeout=None)
                return kite
        except ClientAccount.DoesNotExist:
            pass
        except Exception as e:
            print(f"Error restoring Kite instance for user {user_id}: {e}")
            
        return None

kite_session_manager = KiteSessionManager()