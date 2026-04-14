from flask import Flask, render_template_string, jsonify, request
import requests
import re
import time
import json
import yfinance as yf
import pytz
import concurrent.futures
import threading
from datetime import datetime, timedelta

app = Flask(__name__)

# --- Config ---
EVENTS_URL = "https://gamma-api.polymarket.com/events/keyset"
DATA_API_POSITIONS = "https://data-api.polymarket.com/positions"
PROFILE_API = "https://gamma-api.polymarket.com/public-profile"
TAG_SLUG = "earnings"
DEFAULT_WALLET = "0x0000000000000000000000000000000000000000"

# --- Cache System ---
cache = {"data": None, "last_fetch": 0}
CACHE_TTL = 3600  
cache_lock = threading.Lock()
fetch_lock = threading.Lock() # Ensures only one fetch runs at a time

# --- Connection Pooling & Crumb Pre-warming ---
# We no longer pass a custom requests.Session because newer yfinance versions 
# often require curl_cffi or manage their own sessions more strictly.

# PRE-WARM: Generate the Yahoo Crumb auth synchronously so concurrent threads don't crash
try:
    _ = yf.Ticker("AAPL").info
except Exception:
    pass

# --- Data Fetching Logic ---
def extract_ticker(title):
    match = re.search(r"\(([A-Z]{1,5})\)", title)
    return match.group(1) if match else None

def fetch_live_earnings_date(ticker):
    try:
        stock = yf.Ticker(ticker)
        et_tz = pytz.timezone('America/New_York')
        now_et = datetime.now(et_tz)
        # Cutoff to ignore dates that are more than 2 days in the past
        cutoff = now_et - timedelta(days=2)
        
        # 1. Try stock.info first - it contains earningsTimestamp which is fast and includes time
        info = stock.info
        ts = info.get('earningsTimestamp')
        
        if ts:
            dt_utc = datetime.fromtimestamp(ts, tz=pytz.utc)
            dt = dt_utc.astimezone(et_tz)
            
            if dt > cutoff:
                category = "unknown"
                time_str = ""
                if dt.hour != 0 or dt.minute != 0:
                    category = "pre" if dt.hour < 12 else "post"
                    time_str = dt.strftime("%I:%M %p ET")
                    
                return {
                    "date": dt.strftime("%Y-%m-%d"),
                    "time": time_str,
                    "category": category,
                    "timestamp": dt.timestamp() 
                }

        # 2. Fallback to get_earnings_dates if info doesn't have the timestamp or it is a past date
        df = stock.get_earnings_dates(limit=10)
        if df is not None and not df.empty:
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize('UTC').tz_convert(et_tz)
            else:
                df.index = df.index.tz_convert(et_tz)
            
            future_dates = df[df.index > cutoff].index
            if not future_dates.empty:
                dt = future_dates.min()
                
                category = "unknown"
                time_str = ""
                if dt.hour != 0 or dt.minute != 0:
                    category = "pre" if dt.hour < 12 else "post"
                    time_str = dt.strftime("%I:%M %p ET")
                    
                return {
                    "date": dt.strftime("%Y-%m-%d"),
                    "time": time_str,
                    "category": category,
                    "timestamp": dt.timestamp() 
                }
    except Exception:
        pass
    return None

def fetch_and_parse_events():
    headers = {"User-Agent": "PolymarketEarningsUI/9.0", "Accept": "application/json"}
    events = []
    
    # Keyset Pagination Loop
    limit = 100
    cursor = ""
    
    print("[FETCH] Starting Polymarket event fetch...")
    while True:
        params = {"tag_slug": TAG_SLUG, "closed": "false", "limit": limit}
        if cursor:
            params["cursor"] = cursor
            
        try:
            resp = requests.get(EVENTS_URL, params=params, headers=headers, timeout=10)
            if resp.status_code != 200: 
                print(f"[FETCH] Error: Polymarket API returned {resp.status_code}")
                break
            
            data = resp.json()
            batch = data.get("events", []) if isinstance(data, dict) else data
            if not batch: break
            events.extend(batch)
            
            cursor = data.get("nextCursor") or data.get("next_cursor")
            if not cursor: break
            
        except Exception as e:
            print(f"[FETCH] Exception during Polymarket fetch: {e}")
            break

    print(f"[FETCH] Total raw events found: {len(events)}")
    parsed_events = []
    date_pattern = re.compile(r"release earnings on ([A-Za-z]+\s\d{1,2},?\s\d{4})")
    now_utc = datetime.now(pytz.utc)
    
    # Concurrent YFin fetch using the pre-warmed session pool
    tickers = [extract_ticker(e.get("title") or e.get("question") or "No Title") for e in events]
    print(f"[FETCH] Starting concurrent Yahoo Finance fetch for {len(tickers)} tickers...")
    
    start_yf = time.time()
    # Reduced max_workers to 10 to avoid Yahoo Finance rate limits / blocks
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        earnings_results = list(executor.map(lambda t: fetch_live_earnings_date(t) if t else None, tickers))
    print(f"[FETCH] Yahoo Finance fetch complete in {time.time() - start_yf:.2f}s")
 
    for e, event_info in zip(events, earnings_results):
        desc = e.get("description", "")
        title = e.get("title") or e.get("question") or "No Title"
        slug = e.get("slug", "")
        icon_url = e.get("icon") or e.get("image") or ""
        
        is_new = False
        created_at_str = e.get("createdAt")
        if created_at_str:
            try:
                clean_dt_str = re.sub(r'\.\d+Z$', 'Z', created_at_str).replace("Z", "+00:00")
                dt_created = datetime.fromisoformat(clean_dt_str)
                if (now_utc - dt_created).total_seconds() <= (3 * 24 * 3600):
                    is_new = True
            except Exception:
                pass
        
        token_map = {}
        yes_odds = None
        
        for m in e.get("markets", []):
            ids = m.get("clobTokenIds")
            if isinstance(ids, str):
                try: ids = json.loads(ids)
                except: continue
            if ids and len(ids) >= 2:
                token_map[ids[0]] = "Y"
                token_map[ids[1]] = "N"
            
            if yes_odds is None:
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    try: prices = json.loads(prices)
                    except: pass
                if isinstance(prices, list) and len(prices) > 0:
                    try:
                        yes_odds = int(round(float(prices[0]) * 100))
                    except Exception:
                        pass

        ticker = extract_ticker(title)
        
        # Regex Date Extraction Fallback
        if not event_info:
            match = date_pattern.search(desc)
            if match:
                try:
                    date_str = match.group(1).replace(",", "")
                    dt = datetime.strptime(date_str, "%B %d %Y").date()
                    event_info = {
                        "date": dt.strftime("%Y-%m-%d"),
                        "time": "",
                        "category": "unknown",
                        "timestamp": datetime.combine(dt, datetime.min.time()).timestamp()
                    }
                except ValueError:
                    pass
                    
        if event_info:
            if event_info.get("category") == "unknown":
                end_date_str = e.get("endDate")
                if end_date_str:
                    try:
                        clean_dt_str = re.sub(r'\.\d+Z$', 'Z', end_date_str).replace("Z", "+00:00")
                        dt_end = datetime.fromisoformat(clean_dt_str)
                        et_tz = pytz.timezone('America/New_York')
                        dt_end_et = dt_end.astimezone(et_tz)
                        
                        if dt_end_et.hour != 0 or dt_end_et.minute != 0:
                            event_info["category"] = "pre" if dt_end_et.hour < 12 else "post"
                            event_info["time"] = dt_end_et.strftime("%I:%M %p ET")
                    except Exception:
                        pass
                        
            parsed_events.append({
                "ticker": ticker if ticker else title[:10] + "...",
                "slug": slug, 
                "icon": icon_url,
                "date": event_info["date"],
                "time": event_info["time"],
                "category": event_info["category"],
                "timestamp": event_info["timestamp"],
                "url": f"https://polymarket.com/event/{slug}",
                "token_map": token_map,
                "is_new": is_new,
                "yes_odds": yes_odds
            })

    print(f"[FETCH] Successfully parsed {len(parsed_events)} events.")
    return parsed_events

# --- API Endpoints ---

# BACKGROUND PRELOADER
def init_cache():
    global cache
    with cache_lock:
        if cache["data"]:
            return
            
    print("[INIT] Pre-warming cache...")
    try:
        new_data = fetch_and_parse_events()
        with cache_lock:
            cache["data"] = new_data
            cache["last_fetch"] = time.time()
        print(f"[INIT] Cache pre-warmed with {len(new_data)} events")
    except Exception as e:
        print(f"[INIT] Cache pre-warm failed: {e}")

@app.route('/api/data')
def get_data():
    flush = request.args.get('flush') == 'true'
    current_time = time.time()
    
    # 1. Check if we need to fetch
    should_fetch = False
    with cache_lock:
        if flush or not cache["data"] or (current_time - cache["last_fetch"] > CACHE_TTL):
            should_fetch = True
            
    # 2. Perform fetch outside cache_lock to not block other readers
    if should_fetch:
        # Use fetch_lock to ensure multiple requests don't trigger simultaneous fetches
        if fetch_lock.acquire(blocking=False):
            try:
                print(f"[API] Refreshing data (flush={flush})...")
                new_data = fetch_and_parse_events()
                with cache_lock:
                    cache["data"] = new_data
                    cache["last_fetch"] = time.time()
                print(f"[API] Refresh complete: {len(new_data)} events")
            except Exception as e:
                print(f"[API] Refresh failed: {e}")
            finally:
                fetch_lock.release()
        else:
            # Another thread is already fetching.
            if not cache["data"]:
                with fetch_lock: # Wait for the other thread if no data exists
                    pass

    with cache_lock:
        data = cache["data"] if cache["data"] else []
        
    return jsonify(data)

@app.route('/api/profile')
def get_profile():
    addr = request.args.get("address")
    if not addr: return jsonify({"name": "Unknown", "avatar": ""})
    try:
        u_resp = requests.get(PROFILE_API, params={"address": addr}, timeout=5)
        if u_resp.status_code == 200:
            u_data = u_resp.json()
            name = u_data.get("name") or u_data.get("pseudonym") or f"{addr[:6]}...{addr[-4:]}"
            av = u_data.get("avatar") or u_data.get("image") or u_data.get("profileImage") or ""
            if av and av.startswith("/"): av = "https://polymarket.com" + av
            return jsonify({"name": name, "avatar": av})
    except: pass
    return jsonify({"name": f"{addr[:6]}...{addr[-4:]}", "avatar": ""})

@app.route('/api/refresh_odds')
def refresh_odds():
    headers = {"User-Agent": "PolymarketEarningsUI/9.0", "Accept": "application/json"}
    events = []
    limit = 100
    cursor = ""
    
    while True:
        params = {"tag_slug": TAG_SLUG, "closed": "false", "limit": limit}
        if cursor:
            params["cursor"] = cursor
            
        try:
            resp = requests.get(EVENTS_URL, params=params, headers=headers, timeout=10)
            if resp.status_code != 200: break
            
            data = resp.json()
            batch = data.get("events", []) if isinstance(data, dict) else data
            if not batch: break
            events.extend(batch)
            
            cursor = data.get("nextCursor") or data.get("next_cursor")
            if not cursor: break
        except Exception:
            break

    odds_map = {}
    for e in events:
        slug = e.get("slug", "")
        for m in e.get("markets", []):
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: pass
            if isinstance(prices, list) and len(prices) > 0:
                try:
                    odds_map[slug] = int(round(float(prices[0]) * 100))
                    break 
                except Exception:
                    pass
    return jsonify(odds_map)

@app.route('/api/positions')
def get_positions():
    addr = request.args.get("address", DEFAULT_WALLET)
    if not addr or "0x" not in addr: 
        return jsonify({"positions": [], "closed_positions": [], "username": "", "avatar": ""})
    
    output = {"positions": [], "closed_positions": [], "username": "", "avatar": ""}
    
    def fetch_closed(offset):
        try:
            r = requests.get(DATA_API_POSITIONS.replace("positions", "closed-positions"), 
                             params={"user": addr, "limit": 50, "offset": offset}, timeout=10)
            if r.status_code == 200: return r.json()
        except: pass
        return []

    def fetch_open(offset):
        try:
            r = requests.get(DATA_API_POSITIONS, 
                             params={"user": addr, "limit": 500, "offset": offset}, timeout=10)
            if r.status_code == 200: return r.json()
        except: pass
        return []

    closed_positions = []
    positions = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        closed_futures = [executor.submit(fetch_closed, off) for off in range(0, 2500, 50)]
        open_futures = [executor.submit(fetch_open, off) for off in range(0, 5000, 500)]
        
        for f in concurrent.futures.as_completed(closed_futures):
            res = f.result()
            if isinstance(res, list): closed_positions.extend(res)
            
        for f in concurrent.futures.as_completed(open_futures):
            res = f.result()
            if isinstance(res, list): positions.extend(res)

    seen_closed = set()
    for p in closed_positions:
        asset = p.get("asset")
        if asset and asset not in seen_closed:
            seen_closed.add(asset)
            output["closed_positions"].append(p)
            
    seen_open = set()
    for p in positions:
        asset = p.get("asset")
        if asset and asset not in seen_open:
            seen_open.add(asset)
            output["positions"].append(p)
    
    try:
        u_resp = requests.get(PROFILE_API, params={"address": addr}, timeout=5)
        if u_resp.status_code == 200:
            u_data = u_resp.json()
            output["username"] = u_data.get("name") or u_data.get("pseudonym") or f"{addr[:6]}...{addr[-4:]}"
            av = u_data.get("avatar") or u_data.get("image") or u_data.get("profileImage") or ""
            if av and av.startswith("/"): av = "https://polymarket.com" + av
            output["avatar"] = av
    except: pass
    
    return jsonify(output)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

# --- UI ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Earnings Calendar</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📊</text></svg>">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700;900&family=Inter:wght@400;500;700;900&display=swap" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #0f1115; color: #ffffff; }
        .mono { font-family: 'JetBrains Mono', monospace; }
        .glass-card { background: #111111; border: 1px solid #222222; border-radius: 8px; transition: all 0.2s; }
        .glass-card:hover { border-color: #bef264; background: #161616; }
        .neon-lime { color: #bef264; }
        .sync-loading { animation: spin 1s linear infinite; }
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        
        .custom-scrollbar::-webkit-scrollbar { width: 4px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: transparent; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }
        
        .dropdown-open { opacity: 1 !important; transform: scale(1) !important; pointer-events: auto !important; }
    </style>
</head>
<body class="p-8">
    <div class="max-w-[1400px] mx-auto">
        
        <header class="flex justify-between items-center mb-8 border-b border-white/10 pb-6">
            <div class="flex items-center gap-4">
                <h1 class="text-3xl font-black tracking-tight mono uppercase text-white flex items-center gap-3">
                    <img src="https://cdnjs.cloudflare.com/ajax/libs/twemoji/14.0.2/svg/1f1fa-1f1f8.svg" class="w-8 h-8 drop-shadow-lg" alt="US Markets">
                    <span>Earnings <span class="neon-lime">Calendar</span></span>
                </h1>
                
                <button onclick="refreshMarkets()" id="refreshBtn" title="Update Live Odds" class="hidden bg-[#15181e] border border-white/10 text-slate-400 hover:text-white hover:border-neon-lime text-xs font-black mono p-1.5 rounded transition uppercase items-center justify-center active:scale-95 shadow-md">
                    <svg id="refreshIcon" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                </button>
            </div>
            
            <div id="controlsContainer" class="hidden items-center gap-3">
                
                <div id="mappingControls" class="flex items-center gap-2">
                    <button onclick="showLoginModal()" id="mapBtn" class="bg-[#bef264] text-black text-[11px] font-black mono px-6 py-2.5 rounded transition uppercase flex items-center justify-center gap-1.5 hover:opacity-90 active:scale-95 shadow-md shadow-lime-900/20">
                        <svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1" /></svg>
                        <span>Login</span>
                    </button>
                </div>

                <div id="userProfile" class="hidden relative pl-3 border-l border-white/10 z-50">
                    <button id="profileTrigger" onclick="toggleDropdown(event)" class="flex items-center gap-2 hover:bg-white/5 p-1.5 pr-3 rounded-lg transition cursor-pointer focus:outline-none active:scale-95">
                        <div class="w-8 h-8 rounded-full bg-slate-800 border border-slate-600 flex items-center justify-center overflow-hidden shrink-0 shadow-inner">
                            <img id="userAvatar" src="" class="w-full h-full object-cover hidden">
                            <span id="userInitials" class="text-[10px] font-bold text-slate-400 hidden"></span>
                        </div>
                        <div class="flex flex-col items-start text-left">
                            <span class="text-[9px] text-slate-500 uppercase tracking-widest font-semibold leading-none">Account</span>
                            <span id="usernameVal" class="text-[11px] font-bold text-white truncate max-w-[120px] mt-0.5 leading-none"></span>
                        </div>
                        <svg class="w-3 h-3 text-slate-500 ml-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M19 9l-7 7-7-7"></path></svg>
                    </button>
                    
                    <div id="profileDropdown" class="absolute right-0 mt-2 w-48 bg-[#111318] border border-white/10 rounded-xl shadow-2xl py-2 flex flex-col transform transition-all opacity-0 scale-95 pointer-events-none origin-top-right">
                        </div>
                </div>
            </div>
        </header>

        <main class="relative w-full">
            <div id="loading" class="flex flex-col border border-white/10 rounded-xl bg-[#111318] w-full overflow-hidden shadow-2xl">
                <div class="py-3 flex justify-center items-center border-b border-white/10 bg-[#15181e]/80">
                    <div class="h-3 w-24 bg-white/10 rounded animate-pulse"></div>
                </div>
                <div class="relative flex items-center border-b border-white/10 bg-[#15181e]/80 py-4 shrink-0">
                    <div class="absolute left-6 w-5 h-5 bg-white/10 rounded animate-pulse"></div>
                    <div class="grid grid-cols-5 w-full divide-x divide-white/10 text-center">
                        <script>
                            for(let i=0; i<5; i++) {
                                document.write(`<div class="flex justify-center items-baseline gap-1.5"><div class="h-3 w-8 bg-white/20 rounded animate-pulse"></div><div class="h-4 w-6 bg-white/30 rounded animate-pulse"></div></div>`);
                            }
                        </script>
                    </div>
                    <div class="absolute right-6 w-5 h-5 bg-white/10 rounded animate-pulse"></div>
                </div>
                <div class="grid grid-cols-5 divide-x divide-white/10 w-full h-[500px]">
                    <script>
                        const counts = [4, 3, 5, 2, 3];
                        counts.forEach(count => {
                            let colHtml = `<div class="flex flex-col h-full min-h-0"><div class="p-2 flex-1 overflow-hidden">
                                    <div class="flex items-center justify-center my-4"><div class="h-px bg-white/10 flex-1"></div><div class="h-2 w-16 bg-white/10 rounded mx-2"></div><div class="h-px bg-white/10 flex-1"></div></div>`;
                            for(let i=0; i<count; i++) {
                                colHtml += `<div class="p-3 mb-2 flex justify-between items-center rounded-lg">
                                    <div class="flex items-center gap-3">
                                        <div class="w-8 h-8 rounded-full bg-white/5 animate-pulse shrink-0"></div>
                                        <div class="space-y-2"><div class="h-3 w-12 bg-white/20 rounded animate-pulse"></div><div class="h-2 w-20 bg-white/5 rounded animate-pulse"></div></div>
                                    </div>
                                    <div class="space-y-1 items-end flex flex-col"><div class="h-3 w-8 bg-white/20 rounded animate-pulse"></div><div class="h-2 w-6 bg-white/5 rounded animate-pulse"></div></div>
                                </div>`;
                            }
                            colHtml += `</div></div>`;
                            document.write(colHtml);
                        });
                    </script>
                </div>
            </div>

            <div id="viewCalendar" class="hidden flex-col border border-white/10 rounded-xl bg-[#111318] w-full overflow-hidden shadow-2xl"></div>

            <div id="viewAnalytics" class="hidden flex-col w-full">
                <div class="grid grid-cols-5 gap-4 mb-6">
                    <div class="border border-white/10 rounded-xl bg-[#111318] p-4 shadow-xl relative overflow-hidden">
                        <div class="absolute -right-4 -top-4 w-12 h-12 bg-emerald-400/10 rounded-full blur-xl"></div>
                        <p class="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-1">Realized PnL</p>
                        <h2 id="statRealized" class="text-xl font-black mono text-emerald-400">$--.--</h2>
                    </div>
                    <div class="border border-white/10 rounded-xl bg-[#111318] p-4 shadow-xl relative overflow-hidden">
                        <div class="absolute -right-4 -top-4 w-12 h-12 bg-blue-400/10 rounded-full blur-xl"></div>
                        <p class="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-1">Unrealized PnL</p>
                        <h2 id="statUnrealized" class="text-xl font-black mono text-white">$--.--</h2>
                    </div>
                    <div class="border border-white/10 rounded-xl bg-[#111318] p-4 shadow-xl relative overflow-hidden">
                        <div class="absolute -right-4 -top-4 w-12 h-12 bg-purple-400/10 rounded-full blur-xl"></div>
                        <p class="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-1">Total PnL</p>
                        <h2 id="statTotal" class="text-xl font-black mono text-white">$--.--</h2>
                    </div>
                    <div class="border border-white/10 rounded-xl bg-[#111318] p-4 shadow-xl relative overflow-hidden">
                        <div class="absolute -right-4 -top-4 w-12 h-12 bg-amber-400/10 rounded-full blur-xl"></div>
                        <p class="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-1">Closed Trades</p>
                        <h2 id="statTrades" class="text-xl font-black mono text-white">--</h2>
                    </div>
                    <div class="border border-white/10 rounded-xl bg-[#111318] p-4 shadow-xl relative overflow-hidden">
                        <div class="absolute -right-4 -top-4 w-12 h-12 bg-rose-400/10 rounded-full blur-xl"></div>
                        <p class="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-1">Hit Rate</p>
                        <h2 id="statHitRate" class="text-xl font-black mono text-white">--%</h2>
                    </div>
                </div>
                
                <div class="border border-white/10 rounded-xl bg-[#111318] w-full h-[500px] shadow-2xl relative flex flex-col">
                    <div class="py-4 px-6 border-b border-white/10 flex justify-between items-center shrink-0">
                        <h3 class="text-sm font-bold text-white uppercase tracking-wider mono">Cumulative Realized Profit by Resolution Date</h3>
                    </div>
                    <div id="plotlyChart" class="w-full flex-1"></div>
                </div>
            </div>

        </main>
    </div>

    <div id="loginModal" class="fixed inset-0 bg-[#0f1115]/60 backdrop-blur-sm z-[100] hidden flex-col items-center justify-center">
        <div class="bg-[#111318] border border-white/10 rounded-2xl w-[400px] shadow-2xl overflow-hidden relative">
            <button onclick="closeLoginModal()" class="absolute top-4 right-4 text-slate-500 hover:text-white transition"><svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"/></svg></button>
            <div class="p-6 border-b border-white/10 bg-[#15181e]/80">
                <h2 class="text-lg font-black text-white uppercase mono tracking-widest">Select Account</h2>
            </div>
            <div class="p-6 space-y-4">
                <div id="hardcodedAccounts" class="space-y-2">
                    <div class="text-center text-slate-500 text-xs py-2 mono flex items-center justify-center gap-2">
                        <svg class="w-3 h-3 sync-loading" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                        Loading profiles...
                    </div>
                </div>
                <div class="flex items-center gap-4 my-4 opacity-50">
                    <div class="h-px bg-white/10 flex-1"></div>
                    <span class="text-[10px] text-white mono font-bold uppercase tracking-widest">OR</span>
                    <div class="h-px bg-white/10 flex-1"></div>
                </div>
                <div class="space-y-2">
                    <label class="text-[10px] font-bold text-slate-500 uppercase tracking-widest">Custom Wallet Address</label>
                    <input id="customAddrInput" type="text" placeholder="0x..." oninput="clearAccountSelection()" class="bg-[#0f1115] border border-white/10 text-xs mono rounded-lg px-4 py-3 w-full focus:outline-none focus:border-neon-lime/50 text-white placeholder-slate-600 transition">
                    <div id="loginError" class="text-red-400 text-[10px] mono hidden font-bold mt-1">Invalid Ethereum Wallet Address</div>
                </div>
                <button id="customLoginBtn" onclick="handleLogin()" class="w-full bg-[#bef264] text-black text-[11px] font-black mono px-5 py-3 rounded-lg transition uppercase flex items-center justify-center gap-2 hover:opacity-90 shadow-md">
                    Continue
                </button>
            </div>
        </div>
    </div>

    <script>
        let events = [], positions = [], closed_positions = [], curWeekStart = null, minWeek, maxWeek;
        let currentAddress = "";
        let hardcodedAccountsHtml = "";
        let selectedHardcodedAddress = null;
        
        const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
        const earningsRegex = /will.*?beat.*?quarterly/i;

        const hardcodedWallets = [
            "0x58c028e7c41ba7ef2bde2142cd371cf491661c8e",
            "0x59a7b865c5e9422245f0a4921fad2fcbb4e83b81"
        ];

        function showLoginModal() {
            document.getElementById('loginModal').classList.remove('hidden');
            document.getElementById('loginModal').classList.add('flex');
            
            const container = document.getElementById('hardcodedAccounts');
            if (hardcodedAccountsHtml !== "") {
                container.innerHTML = hardcodedAccountsHtml;
            }
        }

        function closeLoginModal() {
            document.getElementById('loginModal').classList.add('hidden');
            document.getElementById('loginModal').classList.remove('flex');
            document.getElementById('customAddrInput').value = "";
            document.getElementById('loginError').classList.add('hidden');
            document.getElementById('customLoginBtn').innerHTML = 'Continue';
            clearAccountSelection();
        }

        function selectAccount(addr, el) {
            selectedHardcodedAddress = addr;
            document.getElementById('customAddrInput').value = ""; 
            
            document.querySelectorAll('.hardcoded-account').forEach(node => {
                node.classList.remove('border-[#bef264]', 'bg-white/10');
                node.classList.add('border-white/5', 'bg-white/5');
                node.querySelector('.check-icon').classList.add('hidden');
            });
            
            el.classList.remove('border-white/5', 'bg-white/5');
            el.classList.add('border-[#bef264]', 'bg-white/10');
            el.querySelector('.check-icon').classList.remove('hidden');
        }

        function clearAccountSelection() {
            selectedHardcodedAddress = null;
            document.querySelectorAll('.hardcoded-account').forEach(node => {
                node.classList.remove('border-[#bef264]', 'bg-white/10');
                node.classList.add('border-white/5', 'bg-white/5');
                node.querySelector('.check-icon').classList.add('hidden');
            });
        }

        async function preloadHardcodedAccounts() {
            if (hardcodedAccountsHtml !== "") return;
            
            let tempHtml = "";
            for(let w of hardcodedWallets) {
                try {
                    const res = await fetch(`/api/profile?address=${w}`);
                    const p = await res.json();
                    
                    let avatarHtml = p.avatar 
                        ? `<img src="${p.avatar}" class="w-8 h-8 rounded-full object-cover border border-white/10 shrink-0">`
                        : `<div class="w-8 h-8 rounded-full bg-slate-800 flex items-center justify-center text-[10px] font-bold text-slate-400 border border-white/10 shrink-0">${p.name.substring(0,2).toUpperCase()}</div>`;
                        
                    tempHtml += `
                        <button onclick="selectAccount('${w}', this)" class="hardcoded-account w-full flex justify-between items-center p-3 rounded-lg border border-white/5 bg-white/5 hover:bg-white/10 hover:border-white/20 transition group">
                            <div class="flex items-center gap-3">
                                ${avatarHtml}
                                <div class="flex flex-col items-start text-left">
                                    <span class="text-xs font-bold text-white transition">${p.name}</span>
                                    <span class="text-[9px] text-slate-500 mono">${w.substring(0,6)}...${w.substring(w.length-4)}</span>
                                </div>
                            </div>
                            <svg class="check-icon w-4 h-4 text-[#bef264] hidden transition" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" /></svg>
                        </button>
                    `;
                } catch(e) {}
            }
            hardcodedAccountsHtml = tempHtml;
            const container = document.getElementById('hardcodedAccounts');
            if (container && document.getElementById('loginModal').classList.contains('flex')) {
                container.innerHTML = hardcodedAccountsHtml;
            }
        }

        async function handleLogin() {
            const err = document.getElementById('loginError');
            const btn = document.getElementById('customLoginBtn');
            let addrToUse = "";
            
            if (selectedHardcodedAddress) {
                addrToUse = selectedHardcodedAddress;
            } else {
                const addr = document.getElementById('customAddrInput').value.trim();
                if (!/^0x[a-fA-F0-9]{40}$/.test(addr)) {
                    err.classList.remove('hidden');
                    return;
                }
                addrToUse = addr;
            }
            
            err.classList.add('hidden');
            await loginUser(addrToUse, btn);
        }

        async function loginUser(address, btnElement = null) {
            currentAddress = address;
            
            const updateProgress = (percent) => {
                if (btnElement) {
                    btnElement.innerHTML = `
                        <div class="w-full flex justify-center items-center py-1">
                            <div class="w-3/4 bg-black/10 rounded-full h-2 overflow-hidden border border-black/20">
                                <div class="bg-black h-2 transition-all duration-300 ease-out" style="width: ${percent}%"></div>
                            </div>
                        </div>`;
                }
            };

            updateProgress(20);

            try {
                updateProgress(50);
                const res = await fetch(`/api/positions?address=${address}`);
                const data = await res.json();
                
                updateProgress(80);
                positions = data.positions || [];
                closed_positions = data.closed_positions || [];
                
                document.getElementById('mappingControls').classList.add('hidden');
                document.getElementById('mappingControls').classList.remove('flex');
                
                const profileDiv = document.getElementById('userProfile');
                profileDiv.classList.remove('hidden');
                profileDiv.classList.add('flex');
                
                if(data.username) {
                    document.getElementById('usernameVal').innerText = data.username;
                } else {
                    document.getElementById('usernameVal').innerText = address.substring(0,6) + "..." + address.substring(address.length-4);
                }
                
                const avatarImg = document.getElementById('userAvatar');
                const initialsDiv = document.getElementById('userInitials');
                
                if (data.avatar) {
                    avatarImg.src = data.avatar;
                    avatarImg.classList.remove('hidden');
                    initialsDiv.classList.add('hidden');
                } else {
                    avatarImg.classList.add('hidden');
                    initialsDiv.classList.remove('hidden');
                    initialsDiv.innerText = (data.username ? data.username : address).substring(0, 2).toUpperCase();
                }
                
                updateProgress(100);
                
                setTimeout(() => {
                    renderCalendar();
                    closeLoginModal();
                }, 300);
            } catch(e) {
                closeLoginModal();
            }
        }

        document.addEventListener('click', function(event) {
            const dropdown = document.getElementById('profileDropdown');
            const trigger = document.getElementById('profileTrigger');
            if (dropdown && trigger && !trigger.contains(event.target) && !dropdown.contains(event.target)) {
                dropdown.classList.remove('dropdown-open');
            }
        });

        function updateDropdownMenu() {
            const isAnalytics = !document.getElementById('viewAnalytics').classList.contains('hidden');
            const dd = document.getElementById('profileDropdown');
            
            let navBtn = isAnalytics ? `
                <button onclick="switchView('calendar')" class="text-left px-5 py-2.5 text-[11px] font-bold text-white hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg class="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
                    Calendar
                </button>
            ` : `
                <button onclick="switchView('analytics')" class="text-left px-5 py-2.5 text-[11px] font-bold text-neon-lime hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg class="w-4 h-4 text-neon-lime" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z"></path></svg>
                    Analytics
                </button>
            `;

            dd.innerHTML = navBtn + `
                <div class="h-px bg-white/10 my-1 mx-2"></div>
                <button onclick="syncPositions()" class="text-left px-5 py-2.5 text-[11px] font-bold text-emerald-400 hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg id="syncPosIcon" class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                    Reload Portfolio
                </button>
                <div class="h-px bg-white/10 my-1 mx-2"></div>
                <button onclick="logoutUser()" class="text-left px-5 py-2.5 text-[11px] font-bold text-red-400 hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg class="w-4 h-4 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" /></svg>
                    Logout
                </button>
            `;
        }

        function toggleDropdown(e) {
            e.stopPropagation();
            updateDropdownMenu();
            document.getElementById('profileDropdown').classList.toggle('dropdown-open');
        }

        function switchView(viewName) {
            document.getElementById('profileDropdown').classList.remove('dropdown-open');
            
            const vCal = document.getElementById('viewCalendar');
            const vAna = document.getElementById('viewAnalytics');
            const rBtn = document.getElementById('refreshBtn');
            
            vCal.classList.add('hidden'); vCal.classList.remove('flex');
            vAna.classList.add('hidden'); vAna.classList.remove('flex');
            rBtn.classList.add('hidden'); rBtn.classList.remove('flex');
            
            if (viewName === 'calendar') {
                vCal.classList.remove('hidden');
                vCal.classList.add('flex');
                rBtn.classList.remove('hidden');
                rBtn.classList.add('flex');
            } else if (viewName === 'analytics') {
                vAna.classList.remove('hidden');
                vAna.classList.add('flex');
                triggerAnalyticsRender();
            }
        }

        function getMonday(d) {
            d = new Date(d);
            let day = d.getDay();
            let diff = d.getDate() - day + (day == 0 ? -6 : 1);
            let monday = new Date(d.setDate(diff));
            monday.setHours(0, 0, 0, 0);
            return monday;
        }

        function formatLocalDate(d) {
            const y = d.getFullYear();
            const m = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            return `${y}-${m}-${day}`;
        }

        function getPnl(p) {
            let { realized, unrealized } = extractPnL(p);
            return realized + unrealized;
        }

        function getOddsColor(odds) {
            if (odds === null || odds === undefined) return '#94a3b8';
            const hue = Math.floor((odds / 100) * 120);
            return `hsl(${hue}, 80%, 50%)`;
        }

        function renderCalendar() {
            const monthYearOptions = { month: 'long', year: 'numeric' };
            const monthYearStr = curWeekStart.toLocaleDateString('en-US', monthYearOptions);

            const prevDisabled = curWeekStart <= minWeek ? 'disabled' : '';
            const nextDisabled = curWeekStart >= maxWeek ? 'disabled' : '';

            const cal = document.getElementById('viewCalendar');
            
            let html = `
            <div class="py-3 text-center text-sm font-bold text-slate-400 uppercase tracking-widest border-b border-white/10 bg-[#15181e]/80 rounded-t-xl shrink-0">
                ${monthYearStr}
            </div>
            
            <div class="relative flex items-center border-b border-white/10 bg-[#15181e]/80 py-4 shrink-0">
                <button onclick="move(-1)" ${prevDisabled} class="absolute left-6 p-2 text-slate-400 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed z-10">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M15 19l-7-7 7-7"></path></svg>
                </button>
                
                <div class="grid grid-cols-5 divide-x divide-white/10 w-full text-center">
            `;
            
            for (let i = 0; i < 5; i++) {
                const day = new Date(curWeekStart); day.setDate(day.getDate() + i);
                const dayNameShort = days[i].substring(0, 3);
                const dayNum = day.getDate();
                html += `<div class="flex justify-center items-baseline gap-1.5">
                            <span class="text-xs font-semibold text-slate-500">${dayNameShort}</span>
                            <span class="text-base font-bold text-white">${dayNum}</span>
                         </div>`;
            }
            
            html += `
                </div>
                
                <button onclick="move(1)" ${nextDisabled} class="absolute right-6 p-2 text-slate-400 hover:text-white transition disabled:opacity-20 disabled:cursor-not-allowed z-10">
                    <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M9 5l7 7-7 7"></path></svg>
                </button>
            </div>
            
            <div class="grid grid-cols-5 divide-x divide-white/10 w-full h-[500px]">
            `;
            
            for (let i = 0; i < 5; i++) {
                const day = new Date(curWeekStart); day.setDate(day.getDate() + i);
                const dStr = formatLocalDate(day);
                const dayEvs = events.filter(e => e.date === dStr);
                
                html += `<div class="flex flex-col h-full min-h-0"><div class="flex-1 p-2 pb-6 overflow-y-auto custom-scrollbar">`;
                    
                ['pre', 'post', 'unknown'].forEach(cat => {
                    const filtered = dayEvs.filter(e => e.category === cat).sort((a,b) => a.timestamp - b.timestamp);
                    if (filtered.length) {
                        let displayCat = cat;
                        if (cat === 'pre') displayCat = 'Pre Market';
                        if (cat === 'post') displayCat = 'Post Market';
                        if (cat === 'unknown') displayCat = 'Time Unknown';
                        
                        html += `
                        <div class="flex items-center justify-center my-4 opacity-70">
                            <div class="h-px bg-white/10 flex-1"></div>
                            <span class="text-[10px] text-slate-500 uppercase tracking-widest px-3 font-semibold">${displayCat}</span>
                            <div class="h-px bg-white/10 flex-1"></div>
                        </div>`;
                        
                        filtered.forEach(e => {
                            let badgesHtml = "";
                            let totalPnl = 0;
                            let hasPnl = false;
                            
                            if (e.token_map) {
                                Object.entries(e.token_map).forEach(([tid, side]) => {
                                    const pos = positions.find(p => p.asset === tid);
                                    const cPos = closed_positions.find(p => p.asset === tid);
                                    
                                    if (pos || cPos) {
                                        if (pos) totalPnl += getPnl(pos);
                                        if (cPos) totalPnl += getPnl(cPos);
                                        hasPnl = true;
                                    }
                                    
                                    if (pos && parseFloat(pos.size) > 0) {
                                        const shares = parseFloat(pos.size).toLocaleString(undefined, {maximumFractionDigits: 0});
                                        const sideDisplay = side === 'Y' ? 'YES' : 'NO';
                                        
                                        const colorClass = side === 'Y' 
                                            ? 'text-emerald-400 border-emerald-400/30 bg-emerald-400/10' 
                                            : 'text-red-400 border-red-400/30 bg-red-400/10';
                                        
                                        badgesHtml += `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] font-bold mono border ${colorClass}">
                                                        ${sideDisplay} ${shares}
                                                       </span>`;
                                    }
                                });
                            }
                            
                            if (hasPnl) {
                                const pnlFormatted = (totalPnl >= 0 ? '+$' : '-$') + Math.abs(totalPnl).toFixed(2);
                                const pnlColorClass = totalPnl >= 0 
                                    ? 'text-emerald-400 border-emerald-400/30 bg-emerald-400/10' 
                                    : 'text-red-400 border-red-400/30 bg-red-400/10';
                                
                                badgesHtml += `<span class="inline-block px-1.5 py-0.5 rounded text-[10px] font-bold mono border ${pnlColorClass}">
                                                P/L: ${pnlFormatted}
                                           </span>`;
                            }
                            
                            const newTag = e.is_new ? `<div class="flex items-center gap-0.5 text-amber-400 text-[10px] mono font-bold tracking-widest uppercase">
                                    <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M13 10V3L4 14h7v7l9-11h-7z" />
                                    </svg>
                                    <span>NEW</span>
                                  </div>` : "";
                            
                            let oddsDisplay = '';
                            if (e.yes_odds !== null && e.yes_odds !== undefined) {
                                const oddsColor = getOddsColor(e.yes_odds);
                                oddsDisplay = `
                                <div class="flex flex-col items-end pl-2">
                                    <div class="flex items-center gap-1.5" style="color: ${oddsColor}">
                                        <div class="w-1.5 h-1.5 rounded-full" style="background-color: ${oddsColor}"></div>
                                        <span class="text-sm font-bold mono">${e.yes_odds}%</span>
                                    </div>
                                    <span class="text-[10px] text-slate-500 font-medium tracking-wide mt-0.5">beats</span>
                                </div>`;
                            }

                            const tickerLetters = e.ticker.substring(0, 2).toUpperCase();
                            let iconHtml = '';
                            if (e.icon) {
                                iconHtml = `<img src="${e.icon}" alt="${e.ticker}" class="w-8 h-8 rounded-full object-cover border border-white/5 shadow-sm bg-[#1e232b]" onerror="this.outerHTML='<div class=\\'w-8 h-8 rounded-full bg-[#1e232b] flex items-center justify-center text-xs font-bold text-slate-400 border border-white/5 shadow-sm shrink-0\\'>${tickerLetters}</div>'">`;
                            } else {
                                iconHtml = `<div class="w-8 h-8 rounded-full bg-[#1e232b] flex items-center justify-center text-xs font-bold text-slate-400 border border-white/5 shadow-sm shrink-0">
                                                ${tickerLetters}
                                            </div>`;
                            }

                            html += `<a href="${e.url}" target="_blank" class="flex justify-between items-center p-3 hover:bg-white/5 rounded-xl transition-all group mb-1">
                                <div class="flex items-center gap-3">
                                    ${iconHtml}
                                    <div class="flex flex-col justify-center">
                                        <div class="flex items-center gap-1.5">
                                            <span class="text-sm font-bold text-white group-hover:text-emerald-400 transition-colors">${e.ticker}</span>
                                            ${newTag}
                                        </div>
                                        <div class="flex items-center gap-1 mt-1">
                                            ${badgesHtml}
                                        </div>
                                    </div>
                                </div>
                                ${oddsDisplay}
                            </a>`;
                        });
                    }
                });
                
                if (dayEvs.length === 0) {
                     html += `<div class="flex flex-col items-center justify-center h-40 text-slate-600 opacity-50 mt-10">
                                <svg class="w-6 h-6 mb-2" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                                </svg>
                                <span class="text-xs font-medium">No earnings</span>
                              </div>`;
                }
                
                html += `</div></div>`;
            }
            
            html += `</div>`;
            cal.innerHTML = html;
        }

        function move(dir) {
            let newDate = new Date(curWeekStart);
            newDate.setDate(newDate.getDate() + (dir * 7));
            if (newDate >= minWeek && newDate <= maxWeek) {
                curWeekStart = newDate;
                renderCalendar();
            }
        }

        async function init() {
            preloadHardcodedAccounts();
            console.log("Initializing Earnings Terminal...");

            try {
                console.log("Fetching data from /api/data...");
                const res = await fetch('/api/data?flush=false');
                events = await res.json();
                console.log(`Loaded ${events.length} events.`);

                document.getElementById('loading').style.display = 'none';

                const calElem = document.getElementById('viewCalendar');
                calElem.classList.remove('hidden');
                calElem.classList.add('flex');

                const controls = document.getElementById('controlsContainer');
                controls.classList.remove('hidden');
                controls.classList.add('flex');

                const refreshBtn = document.getElementById('refreshBtn');
                refreshBtn.classList.remove('hidden');
                refreshBtn.classList.add('flex');

                if (!events.length) return;
                
                const dates = events.map(e => new Date(e.date + "T00:00:00"));
                minWeek = getMonday(new Date(Math.min(...dates)));
                maxWeek = getMonday(new Date(Math.max(...dates)));
                
                // Set to the current week
                curWeekStart = getMonday(new Date());
                
                renderCalendar();
            } catch(e) {}
        }
        
        async function syncPositions() {
            if(!currentAddress) return;
            const icon = document.querySelector('#profileDropdown #syncPosIcon');
            if(icon) icon.classList.add('sync-loading');
            
            try {
                const res = await fetch(`/api/positions?address=${currentAddress}`);
                const data = await res.json();
                positions = data.positions || [];
                closed_positions = data.closed_positions || [];
                
                if (!document.getElementById('viewCalendar').classList.contains('hidden')) {
                    renderCalendar();
                } else if (!document.getElementById('viewAnalytics').classList.contains('hidden')) {
                    triggerAnalyticsRender();
                }
            } catch(e) { }
            
            setTimeout(() => { 
                if(icon) icon.classList.remove('sync-loading'); 
                document.getElementById('profileDropdown').classList.remove('dropdown-open');
            }, 1000);
        }
        
        function logoutUser() {
            currentAddress = "";
            positions = [];
            closed_positions = [];
            
            document.getElementById('profileDropdown').classList.remove('dropdown-open');
            document.getElementById('mappingControls').classList.remove('hidden');
            document.getElementById('mappingControls').classList.add('flex');
            
            const profileDiv = document.getElementById('userProfile');
            profileDiv.classList.add('hidden');
            profileDiv.classList.remove('flex');
            
            switchView('calendar');
            renderCalendar();
        }

        function extractPnL(p) {
            let realized = parseFloat(p.realizedPnl || 0);
            let unrealized = 0;
            if (p.cashPnl !== undefined && p.cashPnl !== null) {
                unrealized = parseFloat(p.cashPnl);
            } else {
                const current = parseFloat(p.cashValue || p.currentValue || 0);
                const initial = parseFloat(p.initialValue || p.totalBought || 0);
                if (initial !== 0 || current !== 0) {
                    unrealized = current - initial;
                } else {
                    unrealized = parseFloat(p.unrealizedPnl || 0);
                }
            }
            return { realized, unrealized };
        }

        function getTradeDate(p) {
            if (p.endDate) return p.endDate.split("T")[0];
            if (p.timestamp) {
                let ts = parseInt(p.timestamp);
                if (ts < 10000000000) ts *= 1000; 
                return new Date(ts).toISOString().split('T')[0];
            }
            return "Unknown";
        }

        function triggerAnalyticsRender() {
            let totalRealized = 0;
            let totalUnrealized = 0;
            let trades = [];
            let closedCount = 0;
            let winCount = 0;

            closed_positions.forEach(p => {
                if (p.title && earningsRegex.test(p.title)) {
                    closedCount++;
                    let { realized } = extractPnL(p);
                    totalRealized += realized;
                    if (realized > 0) winCount++;
                    
                    let d = getTradeDate(p);
                    trades.push({ date: d, pnl: realized });
                }
            });

            positions.forEach(p => {
                if (p.title && earningsRegex.test(p.title)) {
                    let { realized, unrealized } = extractPnL(p);
                    totalRealized += realized;
                    totalUnrealized += unrealized;
                    
                    if (realized !== 0) {
                        let d = getTradeDate(p);
                        trades.push({ date: d, pnl: realized });
                    }
                }
            });

            let totalPnL = totalRealized + totalUnrealized;
            let hitRate = closedCount > 0 ? ((winCount / closedCount) * 100).toFixed(1) : "0.0";

            document.getElementById('statRealized').innerText = totalRealized >= 0 ? `+$${totalRealized.toFixed(2)}` : `-$${Math.abs(totalRealized).toFixed(2)}`;
            document.getElementById('statRealized').className = totalRealized >= 0 ? "text-xl font-black mono text-emerald-400" : "text-xl font-black mono text-red-400";
            
            document.getElementById('statUnrealized').innerText = totalUnrealized >= 0 ? `+$${totalUnrealized.toFixed(2)}` : `-$${Math.abs(totalUnrealized).toFixed(2)}`;
            document.getElementById('statUnrealized').className = totalUnrealized >= 0 ? "text-xl font-black mono text-emerald-400" : "text-xl font-black mono text-red-400";

            document.getElementById('statTotal').innerText = totalPnL >= 0 ? `+$${totalPnL.toFixed(2)}` : `-$${Math.abs(totalPnL).toFixed(2)}`;
            document.getElementById('statTotal').className = totalPnL >= 0 ? "text-xl font-black mono text-emerald-400" : "text-xl font-black mono text-red-400";

            document.getElementById('statTrades').innerText = closedCount;
            document.getElementById('statHitRate').innerText = `${hitRate}%`;

            let dailyPnl = {};
            trades.forEach(t => {
                if (t.date === "Unknown" || !t.date) return;
                dailyPnl[t.date] = (dailyPnl[t.date] || 0) + t.pnl;
            });

            let sortedDates = Object.keys(dailyPnl).sort();
            let datesOut = [];
            let pnlOut = [];
            let currentSum = 0;
            
            sortedDates.forEach(d => {
                currentSum += dailyPnl[d];
                datesOut.push(d);
                pnlOut.push(currentSum);
            });

            const trace = {
                x: datesOut,
                y: pnlOut,
                type: 'scatter',
                mode: 'lines+markers',
                line: { color: '#bef264', width: 3, shape: 'spline', smoothing: 1.3 },
                marker: { size: 8, color: '#111318', line: { color: '#bef264', width: 2 } },
                fill: 'tozeroy',
                fillcolor: 'rgba(190, 242, 100, 0.05)',
                hovertemplate: '<br><b>Date</b>: %{x}<br><b>Net Realized PnL</b>: $%{y:.2f}<extra></extra>'
            };
            
            const layout = {
                paper_bgcolor: 'transparent',
                plot_bgcolor: 'transparent',
                margin: { t: 20, r: 30, b: 50, l: 60 },
                xaxis: { 
                    showgrid: false, 
                    tickfont: { color: '#64748b', family: 'JetBrains Mono' }
                },
                yaxis: { 
                    showgrid: true, 
                    gridcolor: 'rgba(255,255,255,0.05)', 
                    tickfont: { color: '#64748b', family: 'JetBrains Mono' },
                    tickprefix: '$'
                },
                hovermode: 'closest'
            };
            
            Plotly.newPlot('plotlyChart', [trace], layout, {responsive: true, displayModeBar: false});
        }

        async function refreshMarkets() {
            const icon = document.getElementById('refreshIcon');
            icon.classList.add('sync-loading');
            try {
                const res = await fetch('/api/refresh_odds');
                const oddsMap = await res.json();
                
                let updated = false;
                events.forEach(e => {
                    if (oddsMap[e.slug] !== undefined) {
                        e.yes_odds = oddsMap[e.slug];
                        updated = true;
                    }
                });
                
                if (updated) renderCalendar();
            } catch(e) {}
            setTimeout(() => { 
                icon.classList.remove('sync-loading'); 
            }, 1000);
        }

        init();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    threading.Thread(target=init_cache, daemon=True).start()
    app.run(debug=True, use_reloader=False, port=5008)
