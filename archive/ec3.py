from flask import Flask, render_template_string, jsonify, request
import requests
import re
import time
import json
import pytz
import yfinance as yf
from datetime import datetime, timedelta

app = Flask(__name__)

# --- Config ---
EVENTS_URL = "https://gamma-api.polymarket.com/events"
DATA_API_POSITIONS = "https://data-api.polymarket.com/positions"
PROFILE_API = "https://gamma-api.polymarket.com/public-profile"
TAG_SLUG = "earnings"
MAX_REQUESTS = 10
DEFAULT_WALLET = "0x..." 

# Cache
cache = {"data": None, "last_fetch": 0}
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
            
            # Ensure the dataframe index is localized to ET
            if df.index.tzinfo is None:
                df.index = df.index.tz_localize('UTC').tz_convert(et_tz)
            else:
                df.index = df.index.tz_convert(et_tz)
            
            # Filter for future/current dates (we subtract 1 day to ensure we don't miss today's early earnings)
            future_dates = df[df.index >= now_et - timedelta(days=1)].index
            
            if not future_dates.empty:
                dt = future_dates.min()
                
                # Categorize based on Eastern Time
                if dt.hour == 0 and dt.minute == 0:
                    category = "unknown"
                    time_str = ""
                elif dt.hour < 12:  # Usually BMO (Before Market Open)
                    category = "pre"
                    time_str = dt.strftime("%I:%M %p ET")
                else:               # Usually AMC (After Market Close)
                    category = "post"
                    time_str = dt.strftime("%I:%M %p ET")
                    
                return {
                    "date": dt.strftime("%Y-%m-%d"),
                    "time": time_str,
                    "category": category,
                    "timestamp": dt.timestamp() # Unix timestamp for strict chronological sorting
                }
    except Exception:
        pass
    return None

def fetch_and_parse_events():
    headers = {"User-Agent": "PolymarketEarningsUI/4.0", "Accept": "application/json"}
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
    
    for e in events:
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
            
            # Extract live "Yes" odds
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
        event_info = fetch_live_earnings_date(ticker) if ticker else None
        
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
    headers = {"User-Agent": "PolymarketEarningsUI/4.0", "Accept": "application/json"}
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
                    <input id="addr" type="text" placeholder="WALLET ADDRESS (0x...)" class="bg-[#15181e] border border-white/10 text-[11px] mono rounded px-3 py-2 w-64 focus:outline-none focus:border-neon-lime/50 text-white placeholder-slate-600 shadow-inner">
                    <button onclick="mapPosition()" id="mapBtn" title="Map Wallet Positions" class="bg-[#bef264] text-black text-[10px] font-black mono px-3 py-2 rounded transition uppercase flex items-center justify-center gap-1.5 hover:opacity-90 active:scale-95 shadow-md shadow-lime-900/20">
                        <span id="mapIcon" class="flex items-center justify-center"></span>
                        <span id="mapText">Map Pos</span>
                    </button>
                </div>

                <div id="userProfile" class="hidden items-center gap-3 pl-3 border-l border-white/10">
                    <div class="flex items-center gap-2">
                        <div class="w-8 h-8 rounded-full bg-slate-800 border border-slate-600 flex items-center justify-center overflow-hidden shrink-0 shadow-inner">
                            <img id="userAvatar" src="" class="w-full h-full object-cover hidden">
                            <span id="userInitials" class="text-[10px] font-bold text-slate-400 hidden"></span>
                        </div>
                        <div class="flex flex-col">
                            <span class="text-[9px] text-slate-500 uppercase tracking-widest font-semibold leading-none">Account</span>
                            <span id="usernameVal" class="text-[11px] font-bold text-white truncate max-w-[120px] mt-0.5 leading-none"></span>
                        </div>
                    </div>
                    
                    <div class="h-6 w-px bg-white/10 mx-1"></div>
                    
                    <button onclick="syncPositions()" id="syncPosBtn" title="Sync PnL & Positions" class="bg-[#15181e] border border-white/10 text-emerald-400 hover:bg-white/5 text-[10px] font-bold mono px-2.5 py-1.5 rounded transition uppercase flex items-center gap-1.5 active:scale-95 shadow-sm">
                        <svg id="syncPosIcon" class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>
                        <span>Sync</span>
                    </button>
                    
                    <button onclick="changeAccount()" title="Change Wallet Account" class="bg-[#15181e] border border-white/10 text-slate-400 hover:bg-white/5 hover:text-white text-[10px] font-bold mono px-2.5 py-1.5 rounded transition uppercase flex items-center gap-1.5 active:scale-95 shadow-sm">
                        <svg class="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" /></svg>
                        <span>Switch</span>
                    </button>
                </div>
            </div>
        </header>

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
            
            <div class="grid grid-cols-5 divide-x divide-white/10 w-full h-[700px]">
                <script>
                    const counts = [4, 3, 5, 2, 3];
                    counts.forEach(count => {
                        let colHtml = `<div class="flex flex-col h-full"><div class="p-2 flex-1 overflow-hidden">
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

        <div id="calendar" class="hidden flex-col border border-white/10 rounded-xl bg-[#111318] w-full overflow-hidden shadow-2xl"></div>
    </div>

    <script>
        let events = [], positions = [], closed_positions = [], curWeekStart = null, minWeek, maxWeek;
        let currentAddress = "";
        const days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];

        async function init() {
            try {
                const res = await fetch('/api/data?flush=true');
                events = await res.json();
                document.getElementById('loading').style.display = 'none';
                
                const calElem = document.getElementById('calendar');
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
                render();
            } catch(e) {}
        }

        async function mapPosition() {
            const a = document.getElementById('addr').value;
            if(!a) return;
            currentAddress = a;
            
            const btn = document.getElementById('mapBtn'), icon = document.getElementById('mapIcon'), text = document.getElementById('mapText');
            icon.innerHTML = `<svg class="w-3 h-3 sync-loading" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" /></svg>`;
            text.innerText = "Mapping...";
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
                
                render();
            } catch(e) {}
            icon.innerHTML = ""; text.innerText = "Map Pos";
        }
        
        async function syncPositions() {
            if(!currentAddress) return;
            const icon = document.getElementById('syncPosIcon');
            icon.classList.add('sync-loading');
            try {
                const res = await fetch(`/api/positions?address=${currentAddress}`);
                const data = await res.json();
                positions = data.positions || [];
                closed_positions = data.closed_positions || [];
                render();
            } catch(e) { }
            setTimeout(() => { icon.classList.remove('sync-loading'); }, 1000);
        }
        
        function changeAccount() {
            currentAddress = "";
            positions = [];
            closed_positions = [];
            
            document.getElementById('mappingControls').classList.remove('hidden');
            document.getElementById('mappingControls').classList.add('flex');
            
            const profileDiv = document.getElementById('userProfile');
            profileDiv.classList.add('hidden');
            profileDiv.classList.remove('flex');
            
            document.getElementById('addr').value = '';
            render();
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
                
                if (updated) render();
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
            if (!p) return 0;
            if (p.cashPnl !== undefined && p.cashPnl !== null) return parseFloat(p.cashPnl);
            const current = parseFloat(p.cashValue || p.currentValue || 0);
            const initial = parseFloat(p.initialValue || p.totalBought || 0);
            if (initial !== 0 || current !== 0) return current - initial;
            const r = parseFloat(p.realizedPnl || 0);
            const u = parseFloat(p.unrealizedPnl || 0);
            return r + u;
        }

        function getOddsColor(odds) {
            if (odds === null || odds === undefined) return '#94a3b8';
            const hue = Math.floor((odds / 100) * 120);
            return `hsl(${hue}, 80%, 50%)`;
        }

        function render() {
            const monthYearOptions = { month: 'long', year: 'numeric' };
            const monthYearStr = curWeekStart.toLocaleDateString('en-US', monthYearOptions);

            const prevDisabled = curWeekStart <= minWeek ? 'disabled' : '';
            const nextDisabled = curWeekStart >= maxWeek ? 'disabled' : '';

            const cal = document.getElementById('calendar');
            
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
            
            <div class="grid grid-cols-5 divide-x divide-white/10 w-full h-[700px]">
            `;
            
            for (let i = 0; i < 5; i++) {
                const day = new Date(curWeekStart); day.setDate(day.getDate() + i);
                const dStr = formatLocalDate(day);
                const dayEvs = events.filter(e => e.date === dStr);
                
                html += `<div class="flex flex-col h-full"><div class="flex-1 p-2 pb-6 overflow-y-auto custom-scrollbar">`;
                    
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

                            // Dynamic Icon Rendering with robust fallback
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
        function move(dir) { curWeekStart.setDate(curWeekStart.getDate() + (dir * 7)); render(); }
        init();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5000)