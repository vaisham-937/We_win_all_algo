import json
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.db.models import Sum
from .models import ClientAccount, TradeLog,  TradeSymbol
from .kite_engine.account_manager import kite_session_manager
from django.conf import settings
from datetime import date
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.core.cache import cache
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth import login, logout, authenticate
from .forms import SignUpForm
from django.contrib import messages
from django.core.mail import send_mail
import random
import threading


def root_redirect_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    else:
        return redirect('signup')

# --- 2. SEND OTP API (FIXED: NOW THREADED) ---
def send_verification_otp(request):
    target = request.GET.get('target') # Should be 'email'
    value = request.GET.get('value')
    
    if not value or target != 'email':
        return JsonResponse({'status': 'error', 'message': 'Valid email required'})

    # Generate OTP
    otp = str(random.randint(100000, 999999))
    
    # Save to Session
    request.session['signup_otp'] = otp
    request.session['signup_email'] = value
    request.session['is_email_verified'] = False # Reset verification

    # Send Email in Background
    subject = 'Your Verification OTP'
    message = f'Hello,\n\nYour OTP is: {otp}\n\nUse this to verify your account.'
    
    EmailThread(subject, message, settings.EMAIL_HOST_USER, [value]).start()
    
    return JsonResponse({'status': 'success', 'message': 'OTP sent'})
    

# ---  VERIFY OTP API (FIXED: ADDS SECURITY FLAG) ---
def verify_otp_check(request):
    user_otp = request.GET.get('otp')
    saved_otp = request.session.get('signup_otp')
    
    if saved_otp and user_otp == saved_otp:
        request.session['is_email_verified'] = True # MARK AS VERIFIED
        return JsonResponse({'status': 'success'})
    else:
        return JsonResponse({'status': 'error', 'message': 'Invalid OTP'})


# --- HELPER CLASS: BACKGROUND EMAIL SENDER ---
class EmailThread(threading.Thread):
    def __init__(self, subject, message, from_email, recipient_list):
        self.subject = subject
        self.message = message
        self.from_email = from_email
        self.recipient_list = recipient_list
        threading.Thread.__init__(self)

    def run(self):
        try:
            send_mail(
                self.subject, 
                self.message, 
                self.from_email, 
                self.recipient_list, 
                fail_silently=True
            )
            print(f"✅ Background Email sent successfully to {self.recipient_list}")
        except Exception as e:
            print(f"❌ Failed to send email: {e}")


# --- SIGNUP VIEW (UPDATED) ---
def signup_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard')

    if request.method == 'POST':
        form = SignUpForm(request.POST)
        # SECURITY CHECK: Did they actually verify email?
        if not request.session.get('is_email_verified', False):
            messages.error(request, "Please verify your email address first.")
            return render(request, 'trading/signup.html', {'form': form})

        if form.is_valid():
            # Create User
            user = form.save(commit=False)
            # Ensure we use the verified email from session, not just what they typed
            user.email = request.session.get('signup_email', form.cleaned_data['email'])
            user.save()
            
            # Create Client Account
            ClientAccount.objects.create(
                user=user,
                phone_number=form.cleaned_data.get('phone_number'),
                is_phone_verified=False,
                is_email_verified=True
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
    master_json = cache.get('master_instruments_list')
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

@login_required
def dashboard_view(request):
    """Client Dashboard to view P&L, positions, and controls."""
    try:
        account = ClientAccount.objects.get(user=request.user)
    except ClientAccount.DoesNotExist:
        return redirect('credentials')
    # Fetch today's P&L
    today = timezone.now().date()
    trades_today = TradeLog.objects.filter(
        client_account=account, 
        entry_time__date=today
    )
    realized_pnl = trades_today.filter(status='CLOSED').aggregate(Sum('realized_pnl'))['realized_pnl__sum'] or 0.0
    # Fetch Open Positions
    open_positions = trades_today.filter(status='OPEN').select_related('symbol')
    active_scrips = TradeSymbol.objects.filter(is_active=True)
    # Calculate Unrealized P&L (Requires getting latest tick data)
    # This is handled via the separate API endpoint (get_realtime_pnl) for responsiveness.
    context = {
        'account': account,
        'realized_pnl': round(realized_pnl, 2),
        'open_positions': open_positions,
        'active_scrips': active_scrips,
        'kill_switch_state': account.is_live_trading_enabled,
        'kite_logged_in': bool(account.access_token),
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

