import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Sum
from .models import ClientAccount, TradeLog, TradeSymbol, LadderState
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
        return JsonResponse({
            'status': 'success', 
            'is_enabled': account.is_live_trading_enabled,
            'message': f"Trading is now {status_text}"
        })
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

# --- NEW: SEARCH SYMBOLS API ---
@login_required
def search_instruments(request):
    """Searches the Redis master list for a query string."""
    query = request.GET.get('q', '').upper().strip()
    if len(query) < 2:
        return JsonResponse([], safe=False)
    # Fetch master list from Redis
    master_json = redis_client.get('master_instruments_list')
    if not master_json:
        return JsonResponse({'error': 'Instruments list not found. Run "python manage.py fetch_instruments"'}, status=500)

    master_list = json.loads(master_json)
    
    # Filter Logic (Simple Python Filter)
    # Returns top 10 matches where query is in the symbol name
    results = []
    for instr in master_list:
        if query in instr['symbol']:
            # Friendly display name
            display_name = f"{instr['exchange']}: {instr['symbol']}"
            if instr['name']:
                display_name += f" ({instr['name']})"
                
            results.append({
                'label': display_name, # What is shown in dropdown
                'value': instr         # The full data object
            })
            
            if len(results) >= 20: # Limit to 20 results for speed
                break
    
    return JsonResponse(results, safe=False)

# --- NEW: ADD SYMBOL TO DB ---
@login_required
@require_http_methods(["POST"])
def add_symbol(request):
    """Adds the selected JSON object from search to the TradeSymbol DB."""
    try:
        data = json.loads(request.body)
        instr = data.get('instrument')
        
        # Check if already exists
        if TradeSymbol.objects.filter(instrument_token=instr['token']).exists():
            return JsonResponse({'status': 'error', 'message': 'Symbol already added!'})

        # Determine Price Band Color automatically (Default logic)
        color = 'GREEN'
        if instr['segment'] in ['NFO-FUT', 'NFO-OPT']:
            color = 'BLUE'

        # Create the entry
        TradeSymbol.objects.create(
            symbol=instr['symbol'],
            instrument_token=str(instr['token']),
            exchange=instr['exchange'],
            segment=instr['segment'],
            qty_type='ABS',
            absolute_quantity=instr['lot_size'] if instr['lot_size'] > 0 else 1,
            price_band_color=color,
            is_active=True
        )
        
        return JsonResponse({'status': 'success', 'message': f"Added {instr['symbol']} successfully!"})
        
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

@login_required
@require_http_methods(["POST"])
def remove_symbol(request):
    """Deletes a symbol from the watchlist."""
    try:
        data = json.loads(request.body)
        symbol_id = data.get('id')
        symbol = TradeSymbol.objects.get(id=symbol_id)
        name = symbol.symbol
        symbol.delete()
        return JsonResponse({'status': 'success', 'message': f'Removed {name} from watchlist.'})
    except TradeSymbol.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Symbol not found.'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

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
            
    context = {
        'account': account,
        'message': message
    }
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
        'total_unrealized_pnl': round(unrealized_pnl, 2),
        'positions': positions_data,
        'timestamp': timezone.now().strftime("%H:%M:%S")
    })


# --- TRIGGER LADDER (AUTO-RECOVERY FIX) ---
@login_required
@require_http_methods(["POST"])
def trigger_ladder(request):
    try:
        data = json.loads(request.body)
        token = str(data.get('token'))
        action = data.get('action')
        custom_capital = float(data.get('capital', 10000.0))
        
        account = ClientAccount.objects.get(user=request.user)
        
        # 1. FETCH REDIS DATA FIRST
        tick_json = caches['ticks'].get(f"tick:{token}")
        if not tick_json:
            return JsonResponse({'status': 'error', 'message': 'No Live Data in Redis. Check Ticker.'})
        
        tick_data = json.loads(tick_json)
        
        # 2. AUTO-RECOVER SYMBOL IF MISSING IN DB
        symbol = TradeSymbol.objects.filter(instrument_token=token).first()
        if not symbol:
            # Create it from Redis Data
            if 'exchange' in tick_data:
                symbol = TradeSymbol.objects.create(
                    symbol=tick_data['symbol'],
                    instrument_token=token,
                    exchange=tick_data['exchange'],
                    segment=tick_data['segment'],
                    absolute_quantity=tick_data.get('lot_size', 1),
                    is_active=True
                )
                print(f"Recovered {symbol.symbol} from Redis")
            else:
                return JsonResponse({'status': 'error', 'message': 'Restart Ticker to update metadata.'})

        # 3. START STRATEGY
        ladder, _ = LadderState.objects.get_or_create(client=account, symbol=symbol)
        ladder.trade_capital = custom_capital
        ladder.increase_pct = float(data.get('increase', 1.0))
        ladder.tsl_pct = float(data.get('tsl', 1.0))
        ladder.save()
        
        from trading.kite_engine.strategy_manager import start_buy_ladder, start_sell_ladder
        if action == 'BUY': start_buy_ladder(ladder, tick_data['ltp'])
        elif action == 'SELL': start_sell_ladder(ladder, tick_data['ltp'])
            
        return JsonResponse({'status': 'success', 'message': f'{action} Ladder Started'})
        
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