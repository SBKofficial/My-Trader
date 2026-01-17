import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import json
import os
import io
import subprocess
from datetime import datetime

# --- LOAD SECRETS ---
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- CONFIGURATION ---
MAX_POSITIONS = 2
PORTFOLIO_FILE = 'portfolio.json'

def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def git_commit_push(message):
    subprocess.run(["git", "config", "--global", "user.email", "actions@github.com"])
    subprocess.run(["git", "config", "--global", "user.name", "Trading Bot"])
    subprocess.run(["git", "add", PORTFOLIO_FILE])
    subprocess.run(["git", "commit", "-m", message])
    subprocess.run(["git", "push"])

def load_portfolio():
    if not os.path.exists(PORTFOLIO_FILE):
        return {"cash": 25000, "holdings": [], "last_update_id": 0}
    with open(PORTFOLIO_FILE, 'r') as f:
        return json.load(f)

def save_portfolio(data):
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def check_telegram_commands(portfolio):
    last_id = portfolio.get('last_update_id', 0)
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={last_id + 1}"
    try:
        response = requests.get(url, timeout=10).json()
    except: return portfolio, False

    changes_made = False
    for item in response.get('result', []):
        update_id = item['update_id']
        message = item.get('message', {}).get('text', '').strip().upper()
        
        if message.startswith('/BUY'):
            parts = message.split()
            if len(parts) >= 3:
                symbol = parts[1]
                shares = int(parts[2])
                portfolio['holdings'].append({"symbol": symbol, "shares": shares})
                changes_made = True
                send_telegram(f"✅ *System Updated:* Added {shares} shares of {symbol}.")

        elif message.startswith('/SELL'):
            parts = message.split()
            if len(parts) >= 2:
                symbol = parts[1]
                new_holdings = [h for h in portfolio['holdings'] if h['symbol'] != symbol]
                if len(new_holdings) < len(portfolio['holdings']):
                    portfolio['holdings'] = new_holdings
                    changes_made = True
                    send_telegram(f"✅ *System Updated:* Removed {symbol} from holdings.")
        
        elif message == '/RESET':
            portfolio['holdings'] = []
            changes_made = True
            send_telegram(f"⚠️ *System Reset:* All holdings cleared.")

        portfolio['last_update_id'] = update_id

    return portfolio, changes_made

def get_nifty100_live():
    try:
        url = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers, timeout=10)
        df = pd.read_csv(io.BytesIO(response.content))
        # Filter out "DUMMY" or other bad tickers
        tickers = [f"{x}.NS" for x in df['Symbol'].tolist() if "DUMMY" not in str(x).upper()]
        return tickers
    except:
        return ["RELIANCE.NS", "HDFCBANK.NS", "INFY.NS", "TCS.NS", "ITC.NS"]

def main():
    # PHASE 1: SYNC
    portfolio = load_portfolio()
    portfolio, updated = check_telegram_commands(portfolio)
    if updated:
        save_portfolio(portfolio)
        try: git_commit_push("Auto-update")
        except: pass

    # PHASE 2: ANALYSIS
    holdings = portfolio['holdings']
    my_symbols = [x['symbol'] for x in holdings]
    tickers = get_nifty100_live()
    all_tickers = list(set(tickers + [f"{s}.NS" for s in my_symbols]))
    
    # Identify Schedule
    today = datetime.now()
    is_rebalance_period = today.day <= 7
    
    # Download Data (Robust)
    # Using 'threads=False' is slower but more stable if you are getting 404 errors
    data = yf.download(all_tickers, period="2y", group_by='ticker', progress=False, threads=True)
    nifty = yf.download("^NSEI", period="2y", progress=False)
    
    if isinstance(nifty.columns, pd.MultiIndex): nifty.columns = nifty.columns.get_level_values(0)
    nifty['SMA_200'] = ta.sma(nifty['Close'], length=200)
    market_safe = nifty['Close'].iloc[-1] > nifty['SMA_200'].iloc[-1]
    
    # Calculate Ranks
    rank_scores = {}
    for t in tickers:
        try:
            df = data[t].copy()
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            
            # --- SAFETY CHECKS ---
            df.dropna(subset=['Close'], inplace=True)
            if len(df) < 250: continue 
            
            # FIX: Added fill_method=None to silence warning
            score = df['Close'].pct_change(periods=21, fill_method=None).iloc[-1] * 100
            rank_scores[t.replace('.NS','')] = score
        except: continue
    
    sorted_ranks = sorted(rank_scores.items(), key=lambda x: x[1], reverse=True)
    top_15 = [x[0] for x in sorted_ranks[:15]]

    # BUILD REPORT
    report = []
    report.append(f"📅 *Report for {today.strftime('%d %b %Y')}*")
    report.append(f"Market Status: {'✅ GREEN' if market_safe else '⛔ RED (EXIT ALL)'}")
    report.append(f"MODE: {'🔄 Monthly Rebalance' if is_rebalance_period else '🛡️ Daily Safety Check'}")
    report.append("------------------------")
    
    if holdings:
        report.append("*🔍 YOUR POSITIONS:*")
        for h in holdings:
            sym = h['symbol']
            try:
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
            except:
                report.append(f"⚠️ {sym} (Data Error)")
    else:
        report.append("ℹ️ Portfolio Empty.")

    if market_safe and len(holdings) < MAX_POSITIONS:
        report.append("------------------------")
        report.append("*🚀 BUY SIGNALS:*")
        count = 0
        for stock, score in sorted_ranks:
            if count >= 2: break
            if stock not in my_symbols:
                # Double check 200 DMA for buy candidate
                try:
                    df = data[f"{stock}.NS"].copy()
                    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                    if df['Close'].iloc[-1] > ta.sma(df['Close'], length=200).iloc[-1]:
                        report.append(f"👉 {stock} (Score: {score:.1f}%)")
                        count += 1
                except: continue

    final_msg = "\n".join(report)
    print(final_msg)
    send_telegram(final_msg)

if __name__ == "__main__":
    main()
