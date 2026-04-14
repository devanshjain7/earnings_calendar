from flask import Flask, render_template_string, jsonify
import requests
import re
import time
import pytz
import yfinance as yf
from datetime import datetime, timedelta

app = Flask(__name__)

# --- Config ---
EVENTS_URL = "https://gamma-api.polymarket.com/events"
TAG_SLUG = "earnings"
MAX_REQUESTS = 10

# Cache
cache = {"data": None, "last_fetch": 0}
CACHE_TTL = 3600  

def extract_ticker(title):
    match = re.search(r"\(([A-Z]{1,5})\)", title)
    return match.group(1) if match else None

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
    headers = {"User-Agent": "PolymarketEarningsUI/3.0", "Accept": "application/json"}
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
    
    for e in events:
        desc = e.get("description", "")
        title = e.get("title") or e.get("question") or "No Title"
        slug = e.get("slug", "")
        
        ticker = extract_ticker(title)
        event_info = fetch_live_earnings_date(ticker) if ticker else None
        
        # Fallback to description
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
                        "timestamp": 0 # Unknown time gets sorted to top of its section
                    }
                except ValueError:
                    pass
                    
        if event_info:
            parsed_events.append({
                "ticker": ticker if ticker else title[:10] + "...",
                "date": event_info["date"],
                "time": event_info["time"],
                "category": event_info["category"],
                "timestamp": event_info["timestamp"],
                "url": f"https://polymarket.com/event/{slug}"
            })

    return parsed_events

@app.route('/api/data')
def get_data():
    current_time = time.time()
    if not cache["data"] or (current_time - cache["last_fetch"] > CACHE_TTL):
        cache["data"] = fetch_and_parse_events()
        cache["last_fetch"] = current_time
    return jsonify(cache["data"])

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

# --- HTML / JS / Tailwind CSS Frontend ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket Earnings Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; background-color: #0f172a; color: #f8fafc; }
        .glass-card {
            background: rgba(30, 41, 59, 0.7);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.05);
        }
        .ticker-card:hover { transform: translateY(-3px); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.5); border-color: rgba(52, 211, 153, 0.4); }
    </style>
</head>
<body class="min-h-screen p-8">

    <div class="max-w-7xl mx-auto">
        <header class="flex justify-between items-center mb-10 border-b border-slate-800 pb-6">
            <div>
                <h1 class="text-4xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-blue-400 to-emerald-400">
                    Earnings Calendar
                </h1>
                <p class="text-slate-400 mt-2 text-sm font-medium">All times displayed in US Eastern Time (ET)</p>
            </div>
            
            <div class="flex items-center space-x-4 bg-slate-800 p-2 rounded-xl shadow-inner border border-slate-700">
                <button id="prevBtn" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg disabled:opacity-30 disabled:cursor-not-allowed transition font-semibold" onclick="changeWeek(-1)">
                    &#8592; Prev
                </button>
                <span id="weekLabel" class="font-bold min-w-[200px] text-center text-emerald-300 tracking-wide">
                    Loading...
                </span>
                <button id="nextBtn" class="px-4 py-2 bg-slate-700 hover:bg-slate-600 rounded-lg disabled:opacity-30 disabled:cursor-not-allowed transition font-semibold" onclick="changeWeek(1)">
                    Next &#8594;
                </button>
            </div>
        </header>

        <div id="loading" class="text-center py-20">
            <div class="inline-block animate-spin w-12 h-12 border-4 border-emerald-500 border-t-transparent rounded-full mb-4"></div>
            <p class="text-slate-400 animate-pulse font-semibold tracking-wide">Aggregating live exchange data...</p>
        </div>

        <div id="calendar" class="hidden grid grid-cols-5 gap-6">
            <!-- Columns injected by JS -->
        </div>
    </div>

    <script>
        let events = [];
        let currentWeekStart = null;
        let minWeekStart = null;
        let maxWeekStart = null;

        const daysOfWeek = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];
        
        // Define our daily sections
        const sections = [
            { key: 'pre', label: '🌅 Pre-Market' },
            { key: 'post', label: '🌙 Post-Market' },
            { key: 'unknown', label: '⏱️ Time Unknown' }
        ];

        function getMonday(d) {
            d = new Date(d);
            var day = d.getDay(), diff = d.getDate() - day + (day == 0 ? -6: 1);
            return new Date(d.setDate(diff));
        }

        function formatDateLabel(d) {
            return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
        }

        // Custom local date formatter to prevent JS timezone shifts
        function formatLocalDate(d) {
            const y = d.getFullYear();
            const m = String(d.getMonth() + 1).padStart(2, '0');
            const day = String(d.getDate()).padStart(2, '0');
            return `${y}-${m}-${day}`;
        }

        async function init() {
            const res = await fetch('/api/data');
            events = await res.json();
            
            document.getElementById('loading').classList.add('hidden');
            document.getElementById('calendar').classList.remove('hidden');

            if (events.length === 0) {
                document.getElementById('calendar').innerHTML = "<p class='col-span-5 text-center text-slate-500 font-medium'>No events found.</p>";
                return;
            }

            const dates = events.map(e => new Date(e.date + "T00:00:00"));
            minWeekStart = getMonday(new Date(Math.min(...dates)));
            maxWeekStart = getMonday(new Date(Math.max(...dates)));
            currentWeekStart = new Date(minWeekStart);
            renderCalendar();
        }

        function changeWeek(offset) {
            currentWeekStart.setDate(currentWeekStart.getDate() + (offset * 7));
            renderCalendar();
        }

        function renderCalendar() {
            document.getElementById('prevBtn').disabled = currentWeekStart <= minWeekStart;
            document.getElementById('nextBtn').disabled = currentWeekStart >= maxWeekStart;

            const weekEnd = new Date(currentWeekStart);
            weekEnd.setDate(weekEnd.getDate() + 4);
            document.getElementById('weekLabel').innerText = `${formatDateLabel(currentWeekStart)} - ${formatDateLabel(weekEnd)}`;

            const calendarDiv = document.getElementById('calendar');
            calendarDiv.innerHTML = '';

            for (let i = 0; i < 5; i++) {
                const currentDay = new Date(currentWeekStart);
                currentDay.setDate(currentDay.getDate() + i);
                const dateString = formatLocalDate(currentDay); // Protect against local timezone shift

                const dayEvents = events.filter(e => e.date === dateString);

                let dayHtml = `
                    <div class="flex flex-col bg-slate-800/30 rounded-2xl p-4 border border-slate-700/50">
                        <div class="pb-3 border-b border-slate-700/80 mb-2">
                            <h3 class="text-xl font-bold text-slate-100">${daysOfWeek[i]}</h3>
                            <p class="text-sm text-slate-400 font-medium">${formatDateLabel(currentDay)}</p>
                        </div>
                `;

                if (dayEvents.length === 0) {
                    dayHtml += `<div class="text-sm text-slate-500 italic mt-2 text-center py-4">No scheduled events</div>`;
                } else {
                    sections.forEach(sec => {
                        // Filter by section and sort chronologically by timestamp
                        const secEvents = dayEvents.filter(e => e.category === sec.key).sort((a,b) => a.timestamp - b.timestamp);
                        
                        if (secEvents.length > 0) {
                            dayHtml += `<div class="mt-4 mb-2 text-[11px] font-bold text-slate-400 uppercase tracking-widest pl-1">${sec.label}</div>`;
                            
                            secEvents.forEach(e => {
                                // Add a badge if there is a specific time
                                const timeBadge = e.time ? `<div class="text-[10px] bg-slate-800 text-slate-300 px-2 py-0.5 rounded-md border border-slate-600 shadow-sm">${e.time}</div>` : '';
                                
                                dayHtml += `
                                    <a href="${e.url}" target="_blank" class="block ticker-card glass-card rounded-xl p-3 mb-3 transition-all duration-200 group">
                                        <div class="flex justify-between items-center">
                                            <div class="text-xl font-black text-white group-hover:text-emerald-400 transition-colors">
                                                ${e.ticker}
                                            </div>
                                            ${timeBadge}
                                        </div>
                                    </a>
                                `;
                            });
                        }
                    });
                }
                
                dayHtml += `</div>`;
                calendarDiv.innerHTML += dayHtml;
            }
        }

        init();
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=5001)