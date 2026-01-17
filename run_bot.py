import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import json
import os
import io
import subprocess

# --- LOAD SECRETS (From GitHub Actions) ---
TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# --- CONFIGURATION ---
MAX_POSITIONS = 2
PORTFOLIO_FILE = 'portfolio.json'

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def git_commit_push(message):
    """Commits the updated portfolio.json back to GitHub"""
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
    """Reads Telegram for /BUY, /SELL, /RESET commands"""
    last_id = portfolio.get('last_update_id', 0)
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={last_id + 1}"
    
    try:
        response = requests.get(url).json()
    except: 
        return portfolio, False # Fail silently if network issue

    changes_made = False
    
    for item in response.get('result', []):
        update_id = item['update_id']
        message = item.get('message', {}).get('text', '').strip().upper()
        
        # COMMAND 1: /BUY SYMBOL SHARES (e.g., /BUY VEDL 18)
        if message.startswith('/BUY'):
            parts = message.split()
            if len(parts) >= 3:
                symbol = parts[1]
                shares = int(parts[2])
                portfolio['holdings'].append({"symbol": symbol, "shares": shares})
                changes_made = True
                send_telegram(f"✅ *System Updated:* Added {shares} shares of {symbol}.")

        # COMMAND 2: /SELL SYMBOL (e.g., /SELL VEDL)
        elif message.startswith('/SELL'):
            parts = message.split()
            if len(parts) >= 2:
                symbol = parts[1]
                new_holdings = [h for h in portfolio['holdings'] if h['symbol'] != symbol]
                if len(new_holdings) < len(portfolio['holdings']):
                    portfolio['holdings'] = new_holdings
                    changes_made = True
                    send_telegram(f"✅ *System Updated:* Removed {symbol} from holdings.")
        
        # COMMAND 3: /RESET (Emergency wipe)
        elif message == '/RESET':
            portfolio['holdings'] = []
            changes_made = True
            send_telegram(f"⚠️ *System Reset:* All holdings cleared.")

        # Update ID to prevent re-reading old messages
        portfolio['last_update_id'] = update_id

    return portfolio, changes_made

def get_nifty100_live():
    try:
        url = "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv"
        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(url, headers=headers)
        df = pd.read_csv(io.BytesIO(response.content))
        return [f"{x}.NS" for x in df['Symbol'].tolist()]
    except:
        return ["RELIANCE.NS", "HDFCBANK.NS", "INFY.NS", "TCS.NS", "ITC.NS"]

def main():
    # --- PHASE 1: SYNC (Read Telegram -> Git Push) ---
    portfolio = load_portfolio()
    portfolio, updated = check_telegram_commands(portfolio)
    
    if updated:
        save_portfolio(portfolio)
        try:
            git_commit_push("Auto-update portfolio from Telegram")
        except Exception as e:
            print(f"Git push failed (non-critical): {e}")

    # --- PHASE 2: ANALYSIS (The Morning Scan) ---
    holdings = portfolio['holdings']
    my_symbols = [x['symbol'] for x in holdings]
    
    tickers = get_nifty100_live()
    all_tickers = list(set(tickers + [f"{s}.NS" for s in my_symbols]))
    
    # Download Data (2 Years for valid 200 DMA)
    data = yf.download(all_tickers, period="2y", group_by='ticker', progress=False)
    nifty = yf.download("^NSEI", period="2y", progress=False)
    
    if isinstance(nifty.columns, pd.MultiIndex): nifty.columns = nifty.columns.get_level_values(0)
    nifty['SMA_200'] = ta.sma(nifty['Close'], length=200)
    market_safe = nifty['Close'].iloc[-1] > nifty['SMA_200'].iloc[-1]
    
    report = []
    report.append(f"📅 *Report for {pd.Timestamp.now().strftime('%d %b %Y')}*")
    report.append(f"Market Status: {'✅ GREEN' if market_safe else '⛔ RED (EXIT ALL)'}")
    report.append("------------------------")
    
    # Check Holdings
    if holdings:
        report.append("*🔍 YOUR POSITIONS:*")
        for h in holdings:
            sym = h['symbol']
            try:
                df = data[f"{sym}.NS"].copy()
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                current = df['Close'].iloc[-1]
                sma = ta.sma(df['Close'], length=200).iloc[-1]
                
                if not market_safe:
                    report.append(f"🚨 SELL {sym} (Market Crash)")
                elif current < sma:
                    report.append(f"❌ SELL {sym} (Trend Broken)")
                else:
                    report.append(f"✅ HOLD {sym} (₹{int(current)})")
            except:
                report.append(f"⚠️ {sym} (Data Error)")
    else:
        report.append("ℹ️ Portfolio Empty.")

    # Recommendations (Only if slots open)
    if market_safe and len(holdings) < MAX_POSITIONS:
        report.append("------------------------")
        report.append("*🚀 BUY SIGNALS (Top 2):*")
        candidates = []
        for t in tickers:
            try:
                df = data[t].copy()
                if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
                df.dropna(subset=['Close'], inplace=True)
                if len(df) < 250: continue
                close = df['Close'].iloc[-1]
                sma = ta.sma(df['Close'], length=200).iloc[-1]
                score = df['Close'].pct_change(periods=21).iloc[-1] * 100
                
                # Filter: Price > 200 DMA
                if close > sma:
                    candidates.append({'symbol': t.replace('.NS',''), 'score': score})
            except: continue
        
        candidates.sort(key=lambda x: x['score'], reverse=True)
        
        count = 0
        for c in candidates:
            if count >= 2: break
            if c['symbol'] not in my_symbols:
                report.append(f"👉 {c['symbol']} (Score: {c['score']:.1f}%)")
                count += 1
                
    final_msg = "\n".join(report)
    print(final_msg)
    send_telegram(final_msg)

if __name__ == "__main__":
    main()

