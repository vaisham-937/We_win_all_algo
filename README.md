We All Win Algo - Professional Multi-Client Trading System

We All Win Algo is a robust, multi-client algorithmic trading platform built on the Zerodha Kite Connect API. It allows users to create accounts, verify their email, and automate trading strategies based on the 810%/910% rule with dynamic trailing stop losses.

🚀 Key Features

🔐 Authentication & Security

Secure Signup: Email verification flow using OTP (One Time Password).

Background Processing: Emails are sent via background threads/workers to prevent UI lag.

Credential Management: Secure storage for API Key & Secret with Show/Hide visibility toggles.

Session Security: CSRF protection and secure session handling.

📈 Automated Trading Logic

810% / 910% Rules: Automated entry and exit signal generation based on day range.

Dynamic TSL: Trailing Stop Loss (Y1-Y10) mechanism to lock in profits.

Multi-Target: T1-T10 target levels for systematic profit booking.

🛡️ Risk Management

Kill Switch: Emergency toggle button in the dashboard to instantly stop all trading for a specific client.

Auto Square Off: Automatic closing of positions at 3:15 PM via Celery Beat.

Max Limits: Predefined daily Profit/Loss limits to safeguard capital.

💻 Modern Dashboard (Dark Theme)

Real-time P&L: Live Unrealized P&L updates without refreshing the page.

Active Watchlist: Add/Remove scripts directly from the Redis Master List using a search modal.

Visual Feedback: Green/Red indicators for profit/loss and status.

🛠️ Tech Stack

Backend: Python, Django Framework

Database: SQLite (Dev) / PostgreSQL (Prod)

Broker: Redis (for Caching, Celery, and Real-time Ticks)

Task Queue: Celery (for Scheduling & Background Jobs)

Frontend: HTML5, Tailwind CSS (Glassmorphism Design)

API: Zerodha Kite Connect SDK

⚙️ Installation Guide

Prerequisites

Python 3.10+

Redis Server (Running via WSL on Windows).

Zerodha Developer Account.

Gmail Account (for SMTP) or Twilio (for SMS).

Step 1: Clone & Setup

git clone [https://github.com/yourusername/we-all-win-algo.git](https://github.com/yourusername/we-all-win-algo.git)
cd we-all-win-algo

# Create Virtual Environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate


Step 2: Install Dependencies

pip install -r requirements.txt


Step 3: Database & Admin

python manage.py makemigrations trading
python manage.py migrate
python manage.py createsuperuser


▶️ How to Run the System (The 5-Terminal Setup)

Since this is a real-time system with background tasks, you need to run multiple processes simultaneously.

Terminal 1: Redis Server

Make sure Redis is running (use WSL if on Windows).

# In WSL Terminal
sudo service redis-server start
redis-cli ping  # Should return PONG


Terminal 2: Django Web Server

This runs the Website, Dashboard, and APIs.

python manage.py runserver


Access: http://127.0.0.1:8000/

Terminal 3: Celery Worker (Background Tasks)

Handles email sending and heavy background calculations.

Windows Note: You MUST use --pool=solo.

# Windows
python -m celery -A algosystem worker --pool=solo -l info

# Linux/Mac
celery -A algosystem worker -l info


Terminal 4: Celery Beat (Scheduler)

Handles daily tasks like fetching instrument tokens at 9:00 AM.

python -m celery -A algosystem beat -l info


Terminal 5: Strategy Engine (The Brain)

This script runs the infinite loop to check prices and place orders.

python manage.py run_strategy


(Optional) Terminal 6: Ticker

Fetches live data. Only run this after at least one user has logged into Kite via the Dashboard.

python manage.py run_ticker


📂 Project Structure

algo_multi_client/
├── algosystem/                  # Settings, URLs, Celery Config
│   ├── settings.py
│   ├── celery.py                # Celery App Definition
│   └── __init__.py              # App Loading
├── trading/                     # Main Application
│   ├── kite_engine/             # Core Logic
│   │   ├── strategy_manager.py  # 810% Logic
│   │   └── data_handler.py      # WebSocket
│   ├── management/commands/     # Custom Runners
│   │   ├── run_strategy.py
│   │   └── run_ticker.py
│   ├── templates/trading/       # UI (Login, Signup, Dashboard)
│   ├── tasks.py                 # Celery Tasks (Fetch Instruments)
│   ├── views.py                 # Views & Email Threading
│   ├── forms.py                 # Custom Forms
│   └── models.py                # DB Schema
├── manage.py
└── requirements.txt
