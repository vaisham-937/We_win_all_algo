import json
from celery import shared_task  # New Import
from django.core.cache import cache
from trading.models import ClientAccount
from trading.kite_engine.account_manager import kite_session_manager

@shared_task  # This decorator makes it a Celery task
def fetch_instruments_task():
    """
    Downloads instrument list from Kite to Redis via Celery.
    """
    print("[Celery] Task Started: Fetching instruments...")
    
    # 1. Find a logged-in user
    acc = ClientAccount.objects.filter(access_token__isnull=False).first()
    if not acc:
        return "Failed: No logged-in user found."

    try:
        kite = kite_session_manager.get_kite_instance(acc.user.id)
        if not kite:
            return "Failed: Could not initialize Kite."

        instruments = kite.instruments() 
        
        master_list = []
        for i in instruments:
            if i['exchange'] in ['NSE' ]:
                master_list.append({
                    'symbol': i['tradingsymbol'],
                    'token': i['instrument_token'],
                    'exchange': i['exchange'],
                    'segment': i['segment'],
                    'lot_size': i['lot_size']
                })
        
        cache.set('master_instruments_list', json.dumps(master_list), timeout=86400)
        return f"Success: Cached {len(master_list)} instruments."
        
    except Exception as e:
        return f"Error: {str(e)}"
    
