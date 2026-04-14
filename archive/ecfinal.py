from flask import Flask, render_template_string, jsonify, request
import requests
import re
import time
import json
import pytz
import yfinance as yf
import concurrent.futures
from datetime import datetime, timedelta

app = Flask(__name__)

# --- Config ---
EVENTS_URL = "https://gamma-api.polymarket.com/events"
DATA_API_POSITIONS = "https://data-api.polymarket.com/positions"
PROFILE_API = "https://gamma-api.polymarket.com/public-profile"
TAG_SLUG = "earnings"
MAX_REQUESTS = 10
DEFAULT_WALLET = "0x..." 

# Caches
cache = {"data": None, "last_fetch": 0}
master_cache = {"data": {}, "last_fetch": 0}
CACHE_TTL = 3600  

def extract_ticker(title):
    match = re.search(r"\(([A-Z]{1,5})\)", title)
    return match.group(1) if match else None

# STRICTLY RESTORED FROM ORIGINAL ec.py
def fetch_live_earnings_date(ticker):
    try:
        stock = yf.Ticker(ticker)
        df = stock.get_earnings_dates(limit=10)
        if df is not None and not df.empty:
            # Force everything into US Eastern Time
            et_tz = pytz.timezone('America/New_York')
            now_et = datetime.now(et_tz)
            
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize('UTC').tz_convert(et_tz)
            else:
                df.index = df.index.tz_convert(et_tz)
            
            future_dates = df[df.index >= now_et - timedelta(days=1)].index
            
            if not future_dates.empty:
                dt = future_dates.min()
                
                if dt.hour == 0 and dt.minute == 0:
                    category = "unknown"
                    time_str = ""
                elif dt.hour < 12:  
                    category = "pre"
                    time_str = dt.strftime("%I:%M %p ET")
                else:               
                    category = "post"
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
    headers = {"User-Agent": "PolymarketEarningsUI/5.0", "Accept": "application/json"}
    events = []
    limit = 100
    offset = 0
    loop_count = 0

    while loop_count < MAX_REQUESTS:
        try:
            resp = requests.get(EVENTS_URL, params={"tag_slug": TAG_SLUG, "closed": "false", "limit": limit, "offset": offset}, headers=headers, timeout=15)
            if resp.status_code != 200: break
            data = resp.json()
            batch = data.get("events", []) if isinstance(data, dict) else data
            if not batch: break
            events.extend(batch)
            if len(batch) < limit: break
            offset += limit
            loop_count += 1
            time.sleep(0.5)
        except Exception:
            break

    parsed_events = []
    date_pattern = re.compile(r"release earnings on ([A-Za-z]+\s\d{1,2},?\s\d{4})")
    now_utc = datetime.now(pytz.utc)
    
    # Fetch all earnings dates concurrently (Massive Speedup)
    tickers = [extract_ticker(e.get("title") or e.get("question") or "No Title") for e in events]
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        earnings_results = list(executor.map(lambda t: fetch_live_earnings_date(t) if t else None, tickers))
 
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
                        "timestamp": 0
                    }
                except ValueError:
                    pass
                    
        if event_info:
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

    return parsed_events

# --- ENDPOINTS ---

@app.route('/api/data')
def get_data():
    flush = request.args.get('flush') == 'true'
    current_time = time.time()
    if flush or not cache["data"] or (current_time - cache["last_fetch"] > CACHE_TTL):
        cache["data"] = fetch_and_parse_events()
        cache["last_fetch"] = current_time
    return jsonify(cache["data"])

@app.route('/api/refresh_odds')
def refresh_odds():
    headers = {"User-Agent": "PolymarketEarningsUI/5.0", "Accept": "application/json"}
    events = []
    limit = 100
    offset = 0
    loop_count = 0

    while loop_count < MAX_REQUESTS:
        try:
            resp = requests.get(EVENTS_URL, params={"tag_slug": TAG_SLUG, "closed": "false", "limit": limit, "offset": offset}, headers=headers, timeout=15)
            if resp.status_code != 200: break
            data = resp.json()
            batch = data.get("events", []) if isinstance(data, dict) else data
            if not batch: break
            events.extend(batch)
            if len(batch) < limit: break
            offset += limit
            loop_count += 1
            time.sleep(0.5)
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
    if not addr or "0x" not in addr: return jsonify({"positions": [], "closed_positions": [], "username": "", "avatar": ""})
    output = {"positions": [], "closed_positions": [], "username": "", "avatar": ""}
    try:
        p_resp = requests.get(DATA_API_POSITIONS, params={"user": addr, "sizeThreshold": 0.1}, timeout=10)
        if p_resp.status_code == 200: output["positions"] = p_resp.json()
        
        c_resp = requests.get("https://data-api.polymarket.com/closed-positions", params={"user": addr}, timeout=10)
        if c_resp.status_code == 200: output["closed_positions"] = c_resp.json()
        
        u_resp = requests.get(PROFILE_API, params={"address": addr}, timeout=5)
        if u_resp.status_code == 200:
            u_data = u_resp.json()
            output["username"] = u_data.get("name") or u_data.get("pseudonym") or f"{addr[:6]}...{addr[-4:]}"
            
            av = u_data.get("avatar") or u_data.get("image") or u_data.get("profileImage") or ""
            if av and av.startswith("/"):
                av = "https://polymarket.com" + av
            output["avatar"] = av
            
    except: pass
    return jsonify(output)


# --- NEW: BLAZING FAST MASTER DICTIONARY (CACHED) ---
def fetch_polymarket_batch(offset, closed_status):
    try:
        resp = requests.get(EVENTS_URL, params={"tag_slug": TAG_SLUG, "closed": closed_status, "limit": 100, "offset": offset}, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except: pass
    return []

@app.route('/api/master_dict')
def get_master_dict():
    current_time = time.time()
    
    if master_cache["data"] and (current_time - master_cache["last_fetch"] < CACHE_TTL):
        return jsonify(master_cache["data"])
        
    master_dict = {}
    
    # Concurrently fetch Open + up to 3000 Closed historical events
    tasks = [("false", 0)] + [("true", offset) for offset in range(0, 3000, 100)]
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(fetch_polymarket_batch, t[1], t[0]) for t in tasks]
        for future in concurrent.futures.as_completed(futures):
            events = future.result()
            for e in events:
                end_date = e.get("endDate", "")
                d = end_date.split("T")[0] if end_date else "Unknown"
                for m in e.get("markets", []):
                    ids = m.get("clobTokenIds")
                    if isinstance(ids, str):
                        try: ids = json.loads(ids)
                        except: continue
                    if ids and len(ids) >= 2:
                        master_dict[ids[0]] = d
                        master_dict[ids[1]] = d
                        
    master_cache["data"] = master_dict
    master_cache["last_fetch"] = current_time
    return jsonify(master_dict)

@app.route('/api/debug_analytics')
def debug_analytics():
    addr = request.args.get("address")
    if not addr: return jsonify({"error": "Please provide an address: /api/debug_analytics?address=0x..."})
    
    try:
        p_resp = requests.get(DATA_API_POSITIONS, params={"user": addr, "limit": 1000}, timeout=10)
        positions = p_resp.json() if p_resp.status_code == 200 else []
        
        c_resp = requests.get("https://data-api.polymarket.com/closed-positions", params={"user": addr, "limit": 1000}, timeout=10)
        closed_positions = c_resp.json() if c_resp.status_code == 200 else []
    except Exception as e:
        return jsonify({"error": str(e)})

    master_dict = master_cache.get("data", {})
    if not master_dict:
        return jsonify({"error": "Master dictionary is empty. Please load the main Analytics UI first to populate the cache."})

    matched_closed = []
    unmatched_closed = []
    total_realized_closed = 0

    for p in closed_positions:
        asset = p.get("asset")
        realized = float(p.get("realizedPnl", 0))
        if asset in master_dict:
            total_realized_closed += realized
            matched_closed.append({
                "asset_hash": asset,
                "resolution_date": master_dict[asset],
                "realized_pnl": realized,
                "raw_data": p
            })
        else:
            unmatched_closed.append({"asset_hash": asset, "raw_data": p})

    matched_open = []
    total_unrealized_open = 0
    total_realized_open = 0
    
    for p in positions:
        asset = p.get("asset")
        if asset in master_dict:
            realized = float(p.get("realizedPnl", 0))
            unrealized = 0
            if p.get("cashPnl") is not None:
                unrealized = float(p.get("cashPnl"))
            else:
                current = float(p.get("cashValue") or p.get("currentValue") or 0)
                initial = float(p.get("initialValue") or p.get("totalBought") or 0)
                if initial != 0 or current != 0:
                    unrealized = current - initial
                else:
                    unrealized = float(p.get("unrealizedPnl", 0))
            
            total_realized_open += realized
            total_unrealized_open += unrealized
            
            matched_open.append({
                "asset_hash": asset,
                "resolution_date": master_dict[asset],
                "realized_pnl": realized,
                "unrealized_pnl": unrealized,
                "raw_data": p
            })

    return jsonify({
        "1_SUMMARY": {
            "wallet": addr,
            "total_open_positions_fetched_from_api": len(positions),
            "total_closed_positions_fetched_from_api": len(closed_positions),
            "master_earnings_dictionary_size": len(master_dict),
            "earnings_markets_matched_closed": len(matched_closed),
            "earnings_markets_matched_open": len(matched_open),
            "calculated_total_realized": total_realized_closed + total_realized_open,
            "calculated_total_unrealized": total_unrealized_open
        },
        "2_MATCHED_CLOSED_TRADES": matched_closed,
        "3_MATCHED_OPEN_TRADES": matched_open,
        "4_UNMATCHED_CLOSED_TRADES_SAMPLE": unmatched_closed[:10]
    })

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

# --- UI ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Earnings Terminal</title>
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
                    <span>Earnings <span class="neon-lime">Terminal</span></span>
                </h1>
                
                <button onclick="refreshMarkets()" id="refreshBtn" title="Update Live Odds" class="hidden bg-[#15181e] border border-white/10 text-slate-400 hover:text-white hover:border-neon-lime text-xs font-black mono p-1.5 rounded transition uppercase items-center justify-center active:scale-95 shadow-md">
                    <svg id="refreshIcon" class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                </button>
            </div>
            
            <div id="controlsContainer" class="hidden items-center gap-3">
                <div id="mappingControls" class="flex items-center gap-2">
                    <input id="addr" type="text" placeholder="WALLET ADDRESS (0x...)" class="bg-[#15181e] border border-white/10 text-[11px] mono rounded px-3 py-2 w-64 focus:outline-none focus:border-neon-lime/50 text-white placeholder-slate-600 shadow-inner">
                    <button onclick="loginUser()" id="mapBtn" class="bg-[#bef264] text-black text-[10px] font-black mono px-4 py-2 rounded transition uppercase flex items-center justify-center gap-1.5 hover:opacity-90 active:scale-95 shadow-md shadow-lime-900/20">
                        <span id="mapIcon" class="flex items-center justify-center"></span>
                        <span id="mapText">Login</span>
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
                <div class="grid grid-cols-2 gap-6 mb-6">
                    <div class="border border-white/10 rounded-xl bg-[#111318] p-6 shadow-xl relative overflow-hidden">
                        <div class="absolute -right-4 -top-4 w-16 h-16 bg-emerald-400/10 rounded-full blur-xl"></div>
                        <p class="text-xs font-bold text-slate-500 uppercase tracking-widest mb-1">Total Realized PnL</p>
                        <h2 id="statRealized" class="text-3xl font-black mono text-emerald-400">$--.--</h2>
                    </div>
                    <div class="border border-white/10 rounded-xl bg-[#111318] p-6 shadow-xl relative overflow-hidden">
                        <div class="absolute -right-4 -top-4 w-16 h-16 bg-blue-400/10 rounded-full blur-xl"></div>
                        <p class="text-xs font-bold text-slate-500 uppercase tracking-widest mb-1">Total Unrealized PnL</p>
                        <h2 id="statUnrealized" class="text-3xl font-black mono text-white">$--.--</h2>
                    </div>
                </div>
                
                <div class="border border-white/10 rounded-xl bg-[#111318] w-full h-[500px] shadow-2xl relative flex flex-col">
                    <div id="plotLoader" class="absolute inset-0 flex flex-col items-center justify-center bg-[#111318]/90 z-10 rounded-xl backdrop-blur-sm hidden">
                        <svg class="w-10 h-10 text-neon-lime sync-loading mb-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                        <span class="mono text-[11px] font-bold text-white tracking-widest uppercase">Fetching Master Dictionary...</span>
                    </div>
                
                    <div class="py-4 px-6 border-b border-white/10 flex justify-between items-center shrink-0">
                        <h3 class="text-sm font-bold text-white uppercase tracking-wider mono">Cumulative Profit by Resolution Date</h3>
                    </div>
                    <div id="plotlyChart" class="w-full flex-1"></div>
                </div>
            </div>

        </main>
    </div>

    <script>
        let events = [], positions = [], closed_positions = [], masterDict = {}, curWeekStart = null, minWeek, maxWeek;
        let currentAddress = "";
        const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];

        // Close Dropdown logic
        document.addEventListener('click', function(event) {
            const dropdown = document.getElementById('profileDropdown');
            const trigger = document.getElementById('profileTrigger');
            if (dropdown && trigger && !trigger.contains(event.target) && !dropdown.contains(event.target)) {
                dropdown.classList.remove('dropdown-open');
            }
        });

        // Dynamic Dropdown Rendering
        function updateDropdownMenu() {
            const isAnalytics = !document.getElementById('viewAnalytics').classList.contains('hidden');
            const dd = document.getElementById('profileDropdown');
            
            dd.innerHTML = isAnalytics ? `
                <button onclick="switchView('calendar')" class="text-left px-5 py-2.5 text-[11px] font-bold text-white hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg class="w-4 h-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"></path></svg>
                    Calendar
                </button>
                <button onclick="syncPositions()" class="text-left px-5 py-2.5 text-[11px] font-bold text-emerald-400 hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg id="syncPosIcon" class="w-4 h-4 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                    Reload Portfolio
                </button>
                <div class="h-px bg-white/10 my-1 mx-2"></div>
                <button onclick="logoutUser()" class="text-left px-5 py-2.5 text-[11px] font-bold text-red-400 hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg class="w-4 h-4 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" /></svg>
                    Logout
                </button>
            ` : `
                <button onclick="switchView('analytics')" class="text-left px-5 py-2.5 text-[11px] font-bold text-neon-lime hover:bg-white/5 mono uppercase tracking-wider flex items-center gap-2.5 transition w-full">
                    <svg class="w-4 h-4 text-neon-lime" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z"></path></svg>
                    Analytics
                </button>
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
            
            if (viewName === 'calendar') {
                vAna.classList.add('hidden');
                vAna.classList.remove('flex');
                vCal.classList.remove('hidden');
                vCal.classList.add('flex');
                rBtn.classList.remove('hidden');
                rBtn.classList.add('flex');
            } else if (viewName === 'analytics') {
                vCal.classList.add('hidden');
                vCal.classList.remove('flex');
                rBtn.classList.add('hidden');
                rBtn.classList.remove('flex');
                vAna.classList.remove('hidden');
                vAna.classList.add('flex');
                
                triggerAnalyticsRender();
            }
        }

        async function init() {
            try {
                const res = await fetch('/api/data?flush=true');
                events = await res.json();
                
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
                curWeekStart = new Date(minWeek);
                renderCalendar();
            } catch(e) {}
        }

        async function loginUser() {
            const a = document.getElementById('addr').value;
            if(!a) return;
            currentAddress = a;
            
            const icon = document.getElementById('mapIcon'), text = document.getElementById('mapText');
            icon.innerHTML = `<svg class="w-3 h-3 sync-loading" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>`;
            text.innerText = "Authenticating...";
            
            try {
                const res = await fetch(`/api/positions?address=${a}`);
                const data = await res.json();
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
                    document.getElementById('usernameVal').innerText = a.substring(0,6) + "..." + a.substring(a.length-4);
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
                    initialsDiv.innerText = (data.username ? data.username : a).substring(0, 2).toUpperCase();
                }
                
                renderCalendar();
            } catch(e) {}
            icon.innerHTML = ""; text.innerText = "Login";
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
                } else {
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
            document.getElementById('addr').value = '';
            
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

        async function triggerAnalyticsRender() {
            const loader = document.getElementById('plotLoader');
            
            // Fetch Master Dictionary if Empty
            if (Object.keys(masterDict).length === 0) {
                loader.classList.remove('hidden');
                try {
                    const res = await fetch('/api/master_dict');
                    masterDict = await res.json();
                } catch(e) {}
                loader.classList.add('hidden');
            }

            let totalRealized = 0;
            let totalUnrealized = 0;
            let trades = [];

            // Cross-Reference Closed Positions
            closed_positions.forEach(p => {
                let endDate = masterDict[p.asset];
                if (endDate) {
                    let { realized } = extractPnL(p);
                    totalRealized += realized;
                    trades.push({ date: endDate, pnl: realized });
                }
            });

            // Cross-Reference Active Positions (For Unrealized & Partial Sales)
            positions.forEach(p => {
                let endDate = masterDict[p.asset];
                if (endDate) {
                    let { realized, unrealized } = extractPnL(p);
                    totalRealized += realized;
                    totalUnrealized += unrealized;
                    if (realized !== 0) {
                        trades.push({ date: endDate, pnl: realized });
                    }
                }
            });

            console.log("=== ANALYTICS DEBUG ===");
            console.log("Total Realized:", totalRealized, "Total Unrealized:", totalUnrealized);
            console.table(trades);

            // Update Metric Cards
            document.getElementById('statRealized').innerText = totalRealized >= 0 ? `+$${totalRealized.toFixed(2)}` : `-$${Math.abs(totalRealized).toFixed(2)}`;
            document.getElementById('statRealized').className = totalRealized >= 0 ? "text-3xl font-black mono text-emerald-400" : "text-3xl font-black mono text-red-400";
            
            document.getElementById('statUnrealized').innerText = totalUnrealized >= 0 ? `+$${totalUnrealized.toFixed(2)}` : `-$${Math.abs(totalUnrealized).toFixed(2)}`;
            document.getElementById('statUnrealized').className = totalUnrealized >= 0 ? "text-3xl font-black mono text-emerald-400" : "text-3xl font-black mono text-red-400";

            // Group Realized PnL exactly by Resolution Date
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

            // Render Plotly
            const trace = {
                x: datesOut,
                y: pnlOut,
                type: 'scatter',
                mode: 'lines+markers',
                line: { color: '#bef264', width: 3, shape: 'spline', smoothing: 1.3 },
                marker: { size: 8, color: '#111318', line: { color: '#bef264', width: 2 } },
                fill: 'tozeroy',
                fillcolor: 'rgba(190, 242, 100, 0.05)',
                hovertemplate: '<br><b>Resolution Date</b>: %{x}<br><b>Net PnL</b>: $%{y:.2f}<extra></extra>'
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

        function getMonday(d) {
            d = new Date(d);
            let day = d.getDay(), diff = d.getDate() - day + (day == 0 ? -6: 1);
            return new Date(d.setDate(diff));
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
        function move(dir) { curWeekStart.setDate(curWeekStart.getDate() + (dir * 7)); renderCalendar(); }
        init();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5000)