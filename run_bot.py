import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import json
import os
import io
import subprocess
import time
from datetime import datetime

# --- LOAD SECRETS ---
# Ensure these are set in your Github Secrets or Environment Variables
TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# --- CONFIGURATION ---
MAX_POSITIONS = 2
PORTFOLIO_FILE = 'portfolio.json'

# --- UTILITY FUNCTIONS ---

def send_telegram(message):
    """Sends a message to your Telegram bot."""
    if not TOKEN or not CHAT_ID:
        print("⚠️ Telegram Token/Chat ID missing. Skipping message.")
        return
        
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

def git_commit_push(message):
    """Commits changes to portfolio.json and pushes to repo."""
    try:
        subprocess.run(["git", "config", "--global", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "--global", "user.name", "Trading Bot"], check=True)
        subprocess.run(["git", "add", PORTFOLIO_FILE], check=True)
        subprocess.run(["git", "commit", "-m", message], check=True)
        subprocess.run(["git", "push"], check=True)
        print("✅ Git Push Successful")
    except Exception as e:
        print(f"⚠️ Git Push Failed (Might be local run): {e}")

def load_portfolio():
    """Loads portfolio state from JSON."""
    if not os.path.exists(PORTFOLIO_FILE):
        return {"cash": 25000, "holdings": [], "last_update_id": 0}
    with open(PORTFOLIO_FILE, 'r') as f:
        return json.load(f)

def save_portfolio(data):
    """Saves portfolio state to JSON."""
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(data, f, indent=4)

# --- CORE LOGIC ---

def check_telegram_commands(portfolio):
    """Checks for remote commands via Telegram."""
    if not TOKEN: return portfolio, False

    last_id = portfolio.get('last_update_id', 0)
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={last_id + 1}"
    
    try:
        response = requests.get(url, timeout=10).json()
    except Exception as e: 
        print(f"⚠️ Telegram Update Check Failed: {e}")
        return portfolio, False

    changes_made = False
    for item in response.get('result', []):
        update_id = item['update_id']
        message = item.get('message', {}).get('text', '').strip().upper()

        # COMMAND: /BUY SYMBOL QTY
        if message.startswith('/BUY'):
            parts = message.split()
            if len(parts) >= 3:
                symbol = parts[1].upper()
                try:
                    shares = int(parts[2])
                    portfolio['holdings'].append({"symbol": symbol, "shares": shares})
                    changes_made = True
                    send_telegram(f"✅ *Manual Entry:* Added {shares} shares of {symbol}.")
                except ValueError:
                    send_telegram("❌ Invalid quantity.")

        # COMMAND: /SELL SYMBOL
        elif message.startswith('/SELL'):
            parts = message.split()
            if len(parts) >= 2:
                symbol = parts[1].upper()
                original_count = len(portfolio['holdings'])
                portfolio['holdings'] = [h for h in portfolio['holdings'] if h['symbol'] != symbol]
                
                if len(portfolio['holdings']) < original_count:
                    changes_made = True
                    send_telegram(f"✅ *Manual Exit:* Removed {symbol}.")
                else:
                    send_telegram(f"⚠️ Symbol {symbol} not found in holdings.")

        # COMMAND: /RESET
        elif message == '/RESET':
            portfolio['holdings'] = []
            changes_made = True
            send_telegram(f"⚠️ *System Reset:* All holdings cleared.")

        portfolio['last_update_id'] = update_id

    return portfolio, changes_made

def get_nifty100_live():
    """Fetches Nifty 100 tickers securely with headers and fallback."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": "https://www.nseindia.com/"
    }
    
    try:
        print("⏳ Fetching Nifty 100 list from NSE...")
        url = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"
        s = requests.Session()
        response = s.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        df = pd.read_csv(io.BytesIO(response.content))
        # Filter strictly for NSE tickers
        tickers = [f"{x}.NS" for x in df['Symbol'].tolist() if "DUMMY" not in str(x).upper()]
        print(f"✅ Successfully fetched {len(tickers)} tickers.")
        return tickers

    except Exception as e:
        print(f"⚠️ NSE Download Failed ({e}). Using Hardcoded Backup.")
        # Expanded Backup List (Top 30 by Weight)
        return [
            "RELIANCE.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "TCS.NS", "ITC.NS",
            "LICI.NS", "BHARTIARTL.NS", "SBIN.NS", "HINDUNILVR.NS", "LT.NS", "KOTAKBANK.NS",
            "AXISBANK.NS", "HCLTECH.NS", "ULTRACEMCO.NS", "SUNPHARMA.NS", "TITAN.NS",
            "BAJFINANCE.NS", "MARUTI.NS", "ASIANPAINT.NS", "M&M.NS", "TATASTEEL.NS",
            "NTPC.NS", "POWERGRID.NS", "ADANIENT.NS", "TATAMOTORS.NS", "COALINDIA.NS",
            "ONGC.NS", "BEL.NS", "HAL.NS"
        ]

def main():
    print("🚀 Starting Wolf Strategy Bot...")

    # PHASE 1: SYNC TELEGRAM COMMANDS
    portfolio = load_portfolio()
    portfolio, updated = check_telegram_commands(portfolio)
    if updated:
        save_portfolio(portfolio)
        git_commit_push("Auto-update from Telegram Command")

    # PHASE 2: PREPARE DATA
    holdings = portfolio['holdings']
    my_symbols = [x['symbol'] for x in holdings]
    
    tickers = get_nifty100_live()
    # Combine market tickers with our holdings to ensure we always have data for what we own
    all_tickers = list(set(tickers + [f"{s}.NS" for s in my_symbols]))

    # Identify Schedule (Monthly Rebalance: Days 1-7)
    today = datetime.now()
    is_rebalance_period = today.day <= 7

    print(f"📊 Analyzing {len(all_tickers)} stocks...")

    # Download Data (Robust Mode)
    # Using threads=True for speed, but catching errors later
    try:
        data = yf.download(all_tickers, period="6mo", group_by='ticker', progress=False, threads=True)
        nifty = yf.download("^NSEI", period="6mo", progress=False)
    except Exception as e:
        print(f"❌ Critical Data Download Error: {e}")
        send_telegram(f"❌ Bot Failed: Data Download Error - {e}")
        return

    # Check Nifty Trend (Market Filter)
    try:
        if isinstance(nifty.columns, pd.MultiIndex): nifty.columns = nifty.columns.get_level_values(0)
        nifty['SMA_200'] = ta.sma(nifty['Close'], length=200)
        market_safe = nifty['Close'].iloc[-1] > nifty['SMA_200'].iloc[-1]
    except:
        print("⚠️ Could not calculate Nifty 200DMA. Assuming Market Safe.")
        market_safe = True

    # Calculate Ranks
    rank_scores = {}
    
    for t in tickers:
        try:
            # Handle MultiIndex Data
            df = data[t].copy() if t in data else pd.DataFrame()
            if df.empty: continue
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)

            # --- DATA VALIDATION ---
            # 1. Fill small gaps
            df['Close'] = df['Close'].ffill()
            # 2. Check if enough data exists for 21-day calc
            if len(df) < 30: continue 

            # Calculate Momentum (21 Day Return)
            score = df['Close'].pct_change(periods=21).iloc[-1] * 100
            
            # 3. Sanity Check: If score > 200% (likely a glitch), ignore
            if score > 200: continue

            rank_scores[t.replace('.NS','')] = score
        except Exception as e:
            # Silent continue is okay here to keep logs clean, but printing helps debug
            # print(f"Skipping {t}: {e}")
            continue

    # Sort Ranks
    sorted_ranks = sorted(rank_scores.items(), key=lambda x: x[1], reverse=True)
    top_15 = [x[0] for x in sorted_ranks[:15]]

    # PHASE 3: BUILD REPORT
    report = []
    report.append(f"📅 *Report for {today.strftime('%d %b %Y')}*")
    report.append(f"Market Status: {'✅ GREEN' if market_safe else '⛔ RED (EXIT ALL)'}")
    report.append(f"MODE: {'🔄 Monthly Rebalance' if is_rebalance_period else '🛡️ Daily Safety Check'}")
    report.append("------------------------")

    # Analyze Holdings
    if holdings:
        report.append("*🔍 YOUR POSITIONS:*")
        for h in holdings:
            sym = h['symbol']
            try:
                # Fetch specific stock data
                df = data[f"{sym}.NS"].copy()
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                
                current = df['Close'].iloc[-1]
                sma = ta.sma(df['Close'], length=200).iloc[-1]

                # THE 3 RULES
                if not market_safe:
                    report.append(f"🚨 SELL {sym} (Market Crash)")
                elif current < sma:
                    report.append(f"❌ SELL {sym} (Trend Broken)")
                elif is_rebalance_period and sym not in top_15:
                    report.append(f"❌ SELL {sym} (Rank Drop - Out of Top 15)")
                else:
                    report.append(f"✅ HOLD {sym} (₹{int(current)})")
            except Exception as e:
                report.append(f"⚠️ {sym} (Data Error: {e})")
    else:
        report.append("ℹ️ Portfolio Empty.")

    # Generate Buy Signals (Only if Market Safe & Slots Open)
    if market_safe and len(holdings) < MAX_POSITIONS:
        report.append("------------------------")
        report.append("*🚀 BUY SIGNALS:*")
        count = 0
        for stock, score in sorted_ranks:
            if count >= 2: break # Only show top 2 options
            if stock not in my_symbols:
                # Double check 200 DMA for buy candidate
                try:
                    df = data[f"{stock}.NS"].copy()
                    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                    
                    # Buy only if above 200 DMA
                    if df['Close'].iloc[-1] > ta.sma(df['Close'], length=200).iloc[-1]:
                        report.append(f"👉 {stock} (Score: {score:.1f}%)")
                        count += 1
                except: continue

    # Send Final Report
    final_msg = "\n".join(report)
    print(final_msg)
    send_telegram(final_msg)
    print("✅ Bot run complete.")

if __name__ == "__main__":
    main()
