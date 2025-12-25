import json, time, redis
from celery import shared_task  # New Import
from trading.models import ClientAccount, LadderState
from trading.kite_engine.account_manager import kite_session_manager
from django.core.cache import cache
import logging
from django_redis import get_redis_connection
from trading.kite_engine.strategy_manager import start_buy_ladder, start_sell_ladder
import redis, json
from kiteconnect import KiteConnect
from django.conf import settings

logger = logging.getLogger(__name__)
redis_client = get_redis_connection("default")


# @shared_task(bind=True,autoretry_for=(Exception,),retry_backoff=30,retry_kwargs={"max_retries": 3})
# def cache_nse_cash_instruments(self):
#     """ Cache NSE CASH instruments safely (Celery-safe)"""

#     logger.info("[CELERY] 🚀 Caching NSE CASH instruments")

#     # 1️⃣ Get ANY logged-in client (token must exist in Redis)
#     acc = ClientAccount.objects.filter(is_live_trading_enabled=True).first()
#     if not acc:
#         logger.error("❌ No client with access_token in DB")
#         return "NO_CLIENT"
    
#     # 2️ RAW Redis check (DB-1 guaranteed)
#     token = redis_db.get(f"access_token:{acc.user.id}")
#     api_key = redis_db.get(f"api_key:{acc.user.id}")
#     if not token or not api_key:
#         logger.error("❌ Redis access_token/api_key missing")
#         return "NO_REDIS_TOKEN"

#     # 2️⃣ Restore Kite
#     kite = kite_session_manager.get_kite_instance(acc.user.id)
#     if not kite:
#         logger.error("❌ Kite instance not restored (Redis token missing)")
#         return "NO_KITE"

#     # 3️⃣ SAFE instruments fetch (ONLY NSE)
#     try:
#         instruments = kite.instruments("NSE")
#         cash_map = {
#             i["tradingsymbol"]: {
#                 "token": i["instrument_token"],
#                 "exchange": "NSE"
#             }
#             for i in instruments
#             if i.get("instrument_type") == "EQ"
#         }
#         cache.set("NSE_CASH_MASTER", cash_map, timeout=86400)
#         logger.info(f"✅ Cached {len(cash_map)} NSE CASH stocks")
#         return "SUCCESS"
#     except Exception as e:
#         logger.error(f"Error fetching instruments: {e}")
#         raise self.retry(exc=e)


    
# @shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=5)
# def run_ladder(self, ladder_id, ltp):
#     ladder = LadderState.objects.get(id=ladder_id)
#     print(f"▶ Running ladder {ladder_id} @ LTP {ltp}")

#     # strategy logic here
# @shared_task
# def run_active_ladders():
#     """
#     Beat-safe task: no arguments
#     """
    
#     from trading.tasks import run_ladder

#     r = redis.Redis(host='localhost', port=6379, db=0)

#     ladders = LadderState.objects.filter(is_active=True)

#     for ladder in ladders:
#         tick = r.get(f"tick:{ladder.symbol.instrument_token}")
#         if not tick:
#             continue

#         data = json.loads(tick)
#         ltp = data.get("ltp")
#         if not ltp:
#             continue

#         run_ladder.delay(ladder.id, ltp)


# @shared_task
# def run_chartink_ladder(ladder_id, action):
#     ladder = LadderState.objects.get(id=ladder_id)

#     while True:
#         tick = redis_client.get(f"tick:{ladder.symbol.instrument_token}")
#         if tick:
#             ltp = json.loads(tick)['ltp']
#             break
#         time.sleep(2)

#     logger.info(f"[CHARTINK EXEC] {ladder.symbol.symbol} @ {ltp}")

#     if action == "BUY":
#         start_buy_ladder(ladder, ltp)
#     else:
#         start_sell_ladder(ladder, ltp)

redis_db = redis.Redis(
    host="127.0.0.1",
    port=6379,
    db=1,
    decode_responses=True
)

@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=30, retry_kwargs={"max_retries": 3})
def cache_nse_cash_instruments(self):
    logger.info("[CELERY] 🚀 Caching NSE CASH instruments")

    acc = None

    # 1️⃣ Pick ONLY that user whose token exists in Redis
    for a in ClientAccount.objects.all():
        if redis_db.get(f"access_token:{a.user.id}") and redis_db.get(f"api_key:{a.user.id}"):
            acc = a
            break

    if not acc:
        logger.error("❌ No ClientAccount found with Redis token")
        return "NO_CLIENT_WITH_TOKEN"

    uid = acc.user.id
    logger.info(f"✅ Using Redis-authenticated user {uid}")

    # 2️⃣ Create Kite instance
    try:
        kite = KiteConnect(api_key=redis_db.get(f"api_key:{uid}"))
        kite.set_access_token(redis_db.get(f"access_token:{uid}"))
        kite.profile()  # validate token
    except Exception as e:
        logger.error(f"❌ Kite auth failed for user {uid}: {e}")
        return "NO_KITE"

    # 3️⃣ Fetch NSE CASH instruments
    try:
        instruments = kite.instruments("NSE")
        cash_map = {
            i["tradingsymbol"]: {
                "token": i["instrument_token"],
                "exchange": "NSE"
            }
            for i in instruments
            if i.get("instrument_type") == "EQ"
        }
        cache.set("NSE_CASH_MASTER", cash_map, timeout=86400)
        logger.info(f"✅ Cached {len(cash_map)} NSE CASH stocks (user {uid})")
        return "SUCCESS"

    except Exception as e:
        logger.error(f"❌ Instrument fetch failed: {e}")
        raise self.retry(exc=e)



@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=5)
def run_ladder(self, ladder_id, ltp):
    try:
        ladder = LadderState.objects.get(id=ladder_id, is_active=True)
        # Yahan strategy logic execute karein
        logger.info(f"▶ Running ladder {ladder.id} for {ladder.symbol} @ LTP {ltp}")
    except LadderState.DoesNotExist:
        return "LADDER_NOT_FOUND_OR_INACTIVE"

@shared_task
def run_active_ladders():
    """Periodic task to trigger active ladders"""
    ladders = LadderState.objects.filter(is_active=True)
    
    for ladder in ladders:
        # Consistency: Use the same redis client
        tick_data = redis_client.get(f"tick:{ladder.symbol.instrument_token}")
        if not tick_data:
            continue
        try:
            data = json.loads(tick_data)
            ltp = data.get("ltp")
            if ltp:
                run_ladder.delay(ladder.id, ltp)
        except (json.JSONDecodeError, TypeError):
            continue

@shared_task(bind=True, max_retries=5)
def run_chartink_ladder(self, ladder_id, action):
    """Avoids infinite while loop using Celery retry"""
    try:
        ladder = LadderState.objects.get(id=ladder_id)
        tick_data = redis_client.get(f"tick:{ladder.symbol.instrument_token}")

        if not tick_data:
            # If tick not found, retry after 5 seconds instead of blocking the worker
            logger.warning(f"Tick missing for {ladder.symbol}, retrying...")
            raise self.retry(countdown=5)

        ltp = json.loads(tick_data)['ltp']
        logger.info(f"[CHARTINK EXEC] {ladder.symbol.symbol} Action: {action} @ {ltp}")

        if action == "BUY":
            start_buy_ladder(ladder, ltp)
        else:
            start_sell_ladder(ladder, ltp)
            
    except LadderState.DoesNotExist:
        logger.error(f"Ladder {ladder_id} not found")
    except Exception as e:
        logger.error(f"Error in chartink execution: {e}")
        raise self.retry(exc=e, countdown=10)