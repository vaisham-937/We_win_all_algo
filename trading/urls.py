from django.urls import path
from . import views
from django.contrib.auth import views as auth_views


urlpatterns = [
    path('', views.root_redirect_view, name='root'),
     # --- AUTHENTICATION ---
    path('signup/', views.signup_view, name='signup'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    
    # --- VERIFICATION APIs ---
    path('api/send-otp/', views.send_verification_otp, name='send_verification_otp'),
    path('api/verify-otp/', views.verify_otp_check, name='verify_otp_check'),
    # Main Dashboard
    path('dashboard/', views.dashboard_view, name='dashboard'),
    
    # Client Credentials Management
    path('credentials/', views.credentials_view, name='credentials'),
    
    # Kite Authentication Flow (Per Client)
    path('kite/login/', views.kite_login, name='kite_login'),
    path('kite/callback/', views.kite_callback, name='kite_callback'),
    
    # API endpoints (for dashboard real-time data)
    path('api/pnl/', views.get_realtime_pnl, name='api_pnl'),
    path('api/search-instruments/', views.search_instruments, name='search_instruments'),
    path('api/add-symbol/', views.add_symbol, name='add_symbol'),
    path('api/remove-symbol/', views.remove_symbol, name='remove_symbol'),
    path('api/toggle-kill-switch/', views.toggle_kill_switch, name='toggle_kill_switch'),

]
# path('trading/auth/callback/', views.kite_callback, name='kite_callback_legacy'),