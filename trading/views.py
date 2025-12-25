import json, time, logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from .models import ClientAccount, TradeLog, TradeSymbol, LadderState, ChartinkAlert
from .kite_engine.account_manager import kite_session_manager
from django.conf import settings
from datetime import date
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.core.cache import caches
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import login, logout, authenticate
from .forms import SignUpForm
from django.contrib import messages
from django_redis import get_redis_connection
from .kite_engine.strategy_manager import start_buy_ladder, start_sell_ladder
from django.http import JsonResponse, HttpResponse
from django.contrib.auth.models import User
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)


redis_client = get_redis_connection("ticks")


def root_redirect_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    else:
        return redirect('signup')

# --- SIGNUP VIEW (VERIFICATION REMOVED) ---
def signup_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            # Create User directly
            user = form.save()
            
            # Create Client Account
            ClientAccount.objects.create(
                user=user,
                phone_number=form.cleaned_data.get('phone_number'),
                is_phone_verified=False,
                is_email_verified=False 
            )           
            messages.success(request, "Account created successfully! Please login.")
            return redirect('login')  
        else:
            print("Form Errors:", form.errors)         
    else:
        form = SignUpForm()
    
    return render(request, 'trading/signup.html', {'form': form})

def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            login(request, form.get_user())
            return redirect('dashboard')
    else:
        form = AuthenticationForm()
    return render(request, 'trading/login.html', {'form': form})

def logout_view(request):
    logout(request)
    return redirect('login')

@login_required
@require_http_methods(["POST"])
def toggle_kill_switch(request):
    """Toggles the live trading status instantly."""
    try:
        account = ClientAccount.objects.get(user=request.user)
        # Toggle the status
        account.is_live_trading_enabled = not account.is_live_trading_enabled
        account.save()
        
        status_text = "LIVE" if account.is_live_trading_enabled else "STOPPED"
        return JsonResponse({ 'status': 'success', 'is_enabled': account.is_live_trading_enabled, 'message': f"Trading is now {status_text}"})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

# --- 4. DASHBOARD & STRATEGY ---
@login_required
def dashboard_view(request):
    try:
        account = ClientAccount.objects.get(user=request.user)
    except ClientAccount.DoesNotExist:
        return redirect('credentials')
    
    today = timezone.now().date()
    trades_today = TradeLog.objects.filter(client_account=account, entry_time__date=today)
    realized_pnl = trades_today.filter(status='CLOSED').aggregate(Sum('realized_pnl'))['realized_pnl__sum'] or 0.0
    open_positions = trades_today.filter(status='OPEN').select_related('symbol')
    
    # Generate list of keys from active scrips
    active_tokens = redis_client.smembers("active_tokens")    
    market_data = []
    if active_tokens:
        # Prepare keys: [tick:123, tick:456]
        keys = [f"tick:{int(t)}" for t in active_tokens]
        # Bulk Fetch (MGET returns a LIST, not a Dict)
        if keys:
            raw_data = redis_client.mget(keys)
            # --- FIX STARTS HERE ---
            # OLD BROKEN LINE: for key, val in raw_data.items():
            # NEW CORRECT LOOP (Iterate over the list directly):
            for val in raw_data:
                if val:
                    try: 
                        market_data.append(json.loads(val))
                    except: pass
    gainers = sorted(market_data, key=lambda x: x.get('pct_change', 0), reverse=True)
    losers = sorted(market_data, key=lambda x: x.get('pct_change', 0))
    
    final_gainers = [x for x in gainers if x.get('pct_change', 0) > 0][:10]
    final_losers = [x for x in losers if x.get('pct_change', 0) < 0][:10]
    
    context = {
        'account': account,
        'realized_pnl': round(realized_pnl, 2),
        'open_positions': open_positions,
        'active_scrips': active_tokens,
        'gainers': final_gainers,
        'losers': final_losers,
    }
    return render(request, 'trading/dashboard.html', context)

@login_required
def credentials_view(request):
    """Allows client to input API Key/Secret and manage their account."""
    account, created = ClientAccount.objects.get_or_create(user=request.user)
    message = ""
    
    if request.method == 'POST':
        api_key = request.POST.get('api_key')
        api_secret = request.POST.get('api_secret')
        
        if api_key and api_secret:
            account.api_key = api_key
            account.api_secret = api_secret
            account.access_token = None # Invalidate old token
            account.save()
            message = "Credentials updated successfully. Please login to Kite."
        else:
            message = "Please provide both API Key and Secret."
            
        # Handle Kill Switch toggle
        if 'toggle_switch' in request.POST:
            account.is_live_trading_enabled = not account.is_live_trading_enabled
            account.save()
            message = f"Kill Switch set to: {'ENABLED' if account.is_live_trading_enabled else 'DISABLED'}."
            
    context = { 'account': account,'message': message }
    return render(request, 'trading/credentials.html', context)

@login_required
def kite_login(request):
    """Redirects the client to the Kite login page."""
    try:
        account = ClientAccount.objects.get(user=request.user)
        if not account.api_key or not account.api_secret:
            return redirect('credentials')
        
        login_url = kite_session_manager.get_login_url(account.api_key)
        return redirect(login_url)
        
    except ClientAccount.DoesNotExist:
        return redirect('credentials')

def kite_callback(request):
    """Handles the redirect from Kite after successful login."""
    request_token = request.GET.get('request_token')
    
    if request_token and request.user.is_authenticated:
        success = kite_session_manager.generate_session(request.user, request_token)
        if success:
            return redirect('dashboard')
        else:
            # Handle error (e.g., token expired, API key mismatch)
            return render(request, 'trading/dashboard.html', {'error': 'Kite login failed or token expired.'})
    
    return redirect('dashboard')


@login_required
def get_realtime_pnl(request):
    """
    API endpoint to fetch real-time P&L for the client's open positions.
    Called asynchronously by the dashboard.
    """
    from django.core.cache import caches
    tick_cache = caches['ticks']
    
    try:
        account = ClientAccount.objects.get(user=request.user)
    except ClientAccount.DoesNotExist:
        return JsonResponse({'error': 'Account not configured'}, status=400)

    open_positions = TradeLog.objects.filter(client_account=account, status='OPEN').select_related('symbol')
    
    unrealized_pnl = 0.0
    positions_data = []

    for trade in open_positions:
        tick_data_json = tick_cache.get(f'tick:{trade.symbol.instrument_token}')
        if tick_data_json:
            tick_data = json.loads(tick_data_json)
            ltp = tick_data.get('last_price', trade.entry_price)
            
            pnl = (ltp - trade.entry_price) * trade.quantity * (1 if trade.trade_type == 'BUY' else -1)
            unrealized_pnl += pnl
            
            positions_data.append({
                'symbol': trade.symbol.symbol,
                'entry_price': trade.entry_price,
                'ltp': ltp,
                'pnl': round(pnl, 2)
            })   
    return JsonResponse({
        'total_unrealized_pnl': round(unrealized_pnl, 2),'positions': positions_data,'timestamp': timezone.now().strftime("%H:%M:%S")})


@csrf_exempt
@login_required
def trigger_ladder(request):
    try:
        data = json.loads(request.body)
        token = str(data.get('token'))
        action = data.get('action')
        
        # --- NEW: Get Entry Type and Values ---
        entry_type = data.get('entry_type', 'CAPITAL') # 'CAPITAL' or 'QUANTITY'
        entry_value = float(data.get('entry_value', 10000.0))
        
        account = ClientAccount.objects.get(user=request.user)
        
        # 1. FETCH REDIS DATA
        tick_json = redis_client.get(f"tick:{token}")
        if not tick_json:
            return JsonResponse({'status': 'error', 'message': 'No Live Data in Redis. Check Ticker.'})
        
        tick_data = json.loads(tick_json)
        
        # 2. FETCH OR RECOVER SYMBOL
        symbol = TradeSymbol.objects.filter(instrument_token=token).first()
        if not symbol:
            if 'symbol' in tick_data:
                color = 'GREEN'
                if tick_data.get('is_fno'): color = 'BLUE'
                symbol = TradeSymbol.objects.create(
                    symbol=tick_data['symbol'],
                    instrument_token=token,
                    exchange=tick_data.get('exchange', 'NSE'),
                    segment=tick_data.get('segment', 'EQ'),
                    absolute_quantity=1,
                    price_band_color=color,
                    is_active=True
                )
            else:
                return JsonResponse({'status': 'error', 'message': 'Symbol metadata missing. Restart Ticker.'})

        # 3. UPDATE STRATEGY STATE
        ladder, _ = LadderState.objects.get_or_create(client=account, symbol=symbol)
        
        # --- NEW: Assign values based on Entry Type ---
        ladder.entry_type = entry_type
        if entry_type == 'QUANTITY':
            ladder.fixed_quantity = int(entry_value)
            ladder.trade_capital = 0.0 # Clear capital to avoid confusion
        else:
            ladder.trade_capital = entry_value
            ladder.fixed_quantity = 0 # Clear quantity to avoid confusion

        ladder.increase_pct = float(data.get('increase', 1.0))
        ladder.tsl_pct = float(data.get('tsl', 1.0))
        ladder.save()
        
        # 4. START STRATEGY
        from .kite_engine.strategy_manager import start_buy_ladder, start_sell_ladder
        
        current_ltp = tick_data.get('ltp', 0)
        if current_ltp > 0:
            if action == 'BUY': 
                start_buy_ladder(ladder, current_ltp)
            elif action == 'SELL': 
                start_sell_ladder(ladder, current_ltp)
            
            return JsonResponse({'status': 'success', 'message': f'{action} Ladder Initialized'})
        else:
            return JsonResponse({'status': 'error', 'message': 'LTP is zero. Cannot start ladder.'})
        
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

# --- NEW: AJAX DATA API (REDIS FETCH) ---
@login_required
def get_dashboard_data(request):
    try:
        account = ClientAccount.objects.get(user=request.user)
        
        # 1. P&L (DB hit is unavoidable for TradeLog, but fast)
        today = timezone.now().date()
        realized = TradeLog.objects.filter(client_account=account, entry_time__date=today, status='CLOSED')\
            .aggregate(Sum('realized_pnl'))['realized_pnl__sum'] or 0.0
        
        # 2. FAST REDIS FETCH (NO DB FOR SYMBOLS)
        # Fetch the set of active tokens created by the Ticker
        active_tokens = redis_client.smembers("active_tokens") # Returns {b'123', b'456'}
        
        market_data = []
        if active_tokens:
            # Prepare keys: [tick:123, tick:456]
            keys = [f"tick:{int(t)}" for t in active_tokens]
            
            # FIX FOR RAW REDIS MGET:
            if keys:
                raw_data = redis_client.mget(keys) # Returns a list of values
                
                for val in raw_data:
                    if val:
                        try: market_data.append(json.loads(val))
                        except: pass

        # 3. Process Data (Sort/Rank)
        gainers = sorted(market_data, key=lambda x: x.get('pct_change', 0), reverse=True)
        losers = sorted(market_data, key=lambda x: x.get('pct_change', 0))

        # 4. Open Positions (Needs Live LTP)
        unrealized = 0.0
        open_pos_data = []
        open_positions = TradeLog.objects.filter(client_account=account, status='OPEN').select_related('symbol')
        
        # Create Map for O(1) Access
        live_map = {str(m['token']): m for m in market_data}

        for pos in open_positions:
            token = str(pos.symbol.instrument_token)
            if token in live_map:
                ltp = live_map[token]['ltp']
                curr_val = (ltp - pos.entry_price) * pos.quantity
                if pos.trade_type == 'SELL': curr_val *= -1
                unrealized += curr_val
                
                open_pos_data.append({
                    'id': token, 
                    'ltp': ltp, 
                    'pnl': round(curr_val, 2)
                })
        return JsonResponse({
            'status': 'success',
            'realized_pnl': round(realized, 2),
            'unrealized_pnl': round(unrealized, 2),
            'gainers': [x for x in gainers if x.get('pct_change', 0) > 0][:20], # Top 20
            'losers': [x for x in losers if x.get('pct_change', 0) < 0][:20],   # Top 20
            'positions': open_pos_data
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})


from django.core.cache import cache
# @csrf_exempt
# def chartink_webhook(request, user_id):
#     if request.method != 'POST':
#         return HttpResponse("Listening...")
#     try:
#         data = json.loads(request.body)
#         stocks_str = data.get('stocks', '')
#         scan_name = data.get('scan_name', 'Chartink Alert')

#         ist = pytz.timezone("Asia/Kolkata")
#         now = datetime.now(ist)
#         today = now.strftime("%Y-%m-%d")

#         redis_key = f"chartink_alerts:{user_id}:{today}"
#         seen_key = f"chartink_seen:{user_id}:{today}:{scan_name}"

#         seen = redis_client.smembers(seen_key)
#         stocks = []
#         for s in stocks_str.split(','):
#             s = s.strip()
#             if s and s not in seen:
#                 stocks.append(s)
#                 redis_client.sadd(seen_key, s)
#         if not stocks:
#             return JsonResponse({"status": "ignored"})
#         alert_packet = {
#             "id": int(time.time() * 1000),
#             "scan_name": scan_name,
#             "stocks": stocks,
#             "datetime": now.strftime("%a, %b %d, %Y %I:%M %p"),
#             "timestamp": int(time.time())
#         }
#         redis_client.lpush(redis_key, json.dumps(alert_packet))
#         redis_client.ltrim(redis_key, 0, 50)
#         return JsonResponse({"status": "success"})
#     except Exception as e:
#         return JsonResponse({"status": "error", "message": str(e)}, status=400)


from django.core.cache import cache
@csrf_exempt
def chartink_webhook(request, user_id):
    if request.method != 'POST':
        return HttpResponse("Listening...")
    try:
        data = json.loads(request.body)
        stocks_str = data.get('stocks', '')
        scan_name = data.get('scan_name', 'Chartink Alert')

        ist = pytz.timezone("Asia/Kolkata")
        now = datetime.now(ist)
        today = now.strftime("%Y-%m-%d")

        redis_key = f"chartink_alerts:{user_id}:{today}"
        seen_key = f"chartink_seen:{user_id}:{today}:{scan_name}"

        seen = redis_client.smembers(seen_key)
        cash_master = cache.get("NSE_CASH_MASTER", {})

        stocks_payload = []

        for s in stocks_str.split(','):
            s = s.strip()
            if not s or s.encode() in seen:
                continue

            # ✅ CASH VALIDATION
            if s not in cash_master:
                continue

            token = cash_master[s]["token"]

            # ✅ GET LIVE LTP FROM REDIS
            tick = redis_client.get(f"tick:{token}")
            ltp = json.loads(tick)["ltp"] if tick else None

            stocks_payload.append({
                "symbol": s,
                "token": token,
                "ltp": ltp
            })
            redis_client.sadd(seen_key, s)

        if not stocks_payload:
            return JsonResponse({"status": "ignored"})

        alert_packet = {
            "id": int(time.time() * 1000),
            "scan_name": scan_name,
            "stocks": stocks_payload,
            "datetime": now.strftime("%a, %b %d, %Y %I:%M %p"),
            "timestamp": int(time.time())
        }
        redis_client.lpush(redis_key, json.dumps(alert_packet))
        redis_client.ltrim(redis_key, 0, 50)

        return JsonResponse({"status": "success"})

    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)



# ========== Chartink Trigger API 
# +==================================
@csrf_exempt
@login_required
def trigger_chartink_ladder(request):
    try:
        data = json.loads(request.body)
        symbol_name = data.get('symbol')
        action = data.get('action', 'BUY')

        account = ClientAccount.objects.get(user=request.user)

        # 1️⃣ Symbol resolve
        symbol = TradeSymbol.objects.filter(symbol=symbol_name).first()
        if not symbol:
            return JsonResponse({'status': 'error','message': f'{symbol_name} not in monitored list' })

        # 2️⃣ Ladder create
        ladder, _ = LadderState.objects.get_or_create(
            client=account,
            symbol=symbol,
            ladder_type='CHARTINK'
        )
        ladder.entry_type = data.get('entry_type', 'CAPITAL')
        ladder.trade_capital = float(data.get('entry_value', 10000))
        ladder.fixed_quantity = int(data.get('entry_value', 1))
        ladder.tsl_pct = float(data.get('tsl', 1.0))
        ladder.increase_pct = float(data.get('increase', 1.0))
        ladder.save()

        # 3️⃣ LTP optional (Chartink safe mode)
        tick = redis_client.get(f"tick_symbol:{symbol.symbol}")
        ltp = json.loads(tick)['ltp'] if tick else None

        from .kite_engine.strategy_manager import start_chartink_ladder
        start_chartink_ladder(ladder, ltp, action)

        return JsonResponse({'status': 'success', 'message': 'Chartink ladder started'})

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})


@login_required
def get_alerts_api(request):
    try:
        ist = pytz.timezone("Asia/Kolkata")
        today = datetime.now(ist).strftime("%Y-%m-%d")

        redis_key = f"chartink_alerts:{request.user.id}:{today}"

        raw_alerts = redis_client.lrange(redis_key, 0, -1)
        alerts = [json.loads(a) for a in raw_alerts]

        return JsonResponse({"status": "success","alerts": alerts,"date": today})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)})

@csrf_exempt
@login_required
def execute_alert_trade(request):
    """Start ladder trade from: -1. Top Gainers / Losers (token available) -2. Chartink Alerts (only symbol available) """
    if request.method != "POST":
        return JsonResponse({"status": "error", "message": "Invalid method"}, status=405)
    try:
        data = json.loads(request.body)

        token = data.get("token")          # may be None for Chartink
        symbol_name = data.get("symbol")   # MUST for Chartink
        side = data.get("action", "BUY")

        if not symbol_name:
            return JsonResponse({"status": "error", "message": "Symbol missing"}, status=400)

        # ---------------------------------------------------
        # 1️⃣ Resolve TradeSymbol
        # ---------------------------------------------------
        symbol_obj = None

        if token:
            symbol_obj = TradeSymbol.objects.filter(instrument_token=token).first()

        if not symbol_obj:
            symbol_obj = TradeSymbol.objects.filter( symbol=symbol_name).first()

        if not symbol_obj:
            return JsonResponse({"status": "error","message": f"{symbol_name} not found in monitored watchlist"}, status=400)

        # ---------------------------------------------------
        # 2️⃣ Get client account
        # ---------------------------------------------------
        account = ClientAccount.objects.get(user=request.user)

        ladder, _ = LadderState.objects.get_or_create(client=account, symbol=symbol_obj)

        if ladder.is_active:
            return JsonResponse({"status": "error", "message": "Ladder already running"}, status=400)

        # ---------------------------------------------------
        # 3️⃣ Get live price from Redis
        # ---------------------------------------------------
        tick_data = redis_client.get(f"tick:{symbol_obj.instrument_token}")
        if not tick_data:
            return JsonResponse({ "status": "error","message": "Live price not available"}, status=400)

        ltp = json.loads(tick_data)["ltp"]

        # ---------------------------------------------------
        # 4️⃣ Start ladder
        # ---------------------------------------------------
        if side == "BUY":
            start_buy_ladder(ladder, ltp)
        else:
            start_sell_ladder(ladder, ltp)

        return JsonResponse({"status": "success","message": f"{side} ladder started for {symbol_name}"})
    except Exception as e:
        print("❌ execute_alert_trade error:", e)
        return JsonResponse({ "status": "error", "message": str(e)}, status=500)
