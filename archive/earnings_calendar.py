import streamlit as st
import requests
import re
import time
from datetime import datetime, timedelta, date

# --- Config ---
EVENTS_URL = "https://gamma-api.polymarket.com/events"
TAG_SLUG = "earnings"
MAX_REQUESTS = 10  # Failsafe limit for pagination

st.set_page_config(layout="wide", page_title="Polymarket Earnings Calendar")

@st.cache_data(ttl=3600)  # Cache the data for an hour
def fetch_and_parse_events():
    headers = {
        "User-Agent": "PolymarketEarningsUI/1.1",
        "Accept": "application/json",
    }
    events = []
    limit = 100
    offset = 0
    loop_count = 0

    # 1. Fetch active earnings events
    while loop_count < MAX_REQUESTS:
        params = {
            "tag_slug": TAG_SLUG,
            "closed": "false",  # Only open/active events
            "limit": limit,
            "offset": offset,
        }
        
        try:
            resp = requests.get(EVENTS_URL, params=params, headers=headers, timeout=15)
            if resp.status_code != 200:
                break
                
            batch = resp.json()
            
            # Extract events list
            if isinstance(batch, dict) and "events" in batch:
                batch = batch["events"]
                
            if not batch:
                break
                
            events.extend(batch)
            
            # Break if we've reached the end of the results
            if len(batch) < limit:
                break

            offset += limit
            loop_count += 1
            time.sleep(0.5)

        except Exception as e:
            st.error(f"Error fetching data: {e}")
            break

    # 2. Parse descriptions for dates
    parsed_events = []
    date_pattern = re.compile(r"release earnings on ([A-Za-z]+\s\d{1,2},?\s\d{4})")
    
    for e in events:
        desc = e.get("description", "")
        title = e.get("title") or e.get("question") or "No Title"
        slug = e.get("slug", "")
        
        match = date_pattern.search(desc)
        if match:
            date_str = match.group(1).replace(",", "")
            try:
                event_date = datetime.strptime(date_str, "%B %d %Y").date()
                parsed_events.append({
                    "title": title,
                    "date": event_date,
                    "url": f"https://polymarket.com/event/{slug}"
                })
            except ValueError:
                pass

    return parsed_events

def main():
    st.title("📊 Polymarket Earnings Calendar")

    # Initialize session state for week navigation
    if 'week_offset' not in st.session_state:
        st.session_state.week_offset = 0

    # Fetch data (cached after the first load)
    with st.spinner("Fetching active earnings events..."):
        events = fetch_and_parse_events()

    # Layout for navigation controls
    col_prev, col_current, col_next = st.columns([1, 4, 1])
    
    with col_prev:
        if st.button("⬅️ Previous Week", use_container_width=True):
            st.session_state.week_offset -= 1
            st.rerun()
            
    with col_next:
        if st.button("Next Week ➡️", use_container_width=True):
            st.session_state.week_offset += 1
            st.rerun()

    # Calculate the start (Monday) and end (Sunday) of the currently selected week
    today = date.today()
    start_of_week = today - timedelta(days=today.weekday()) + timedelta(weeks=st.session_state.week_offset)
    end_of_week = start_of_week + timedelta(days=6)

    with col_current:
        st.markdown(
            f"<h3 style='text-align: center;'>Week of {start_of_week.strftime('%B %d, %Y')} - {end_of_week.strftime('%B %d, %Y')}</h3>", 
            unsafe_allow_html=True
        )

    st.divider()

    # Create 5 columns for the standard trading week (Monday - Friday)
    days_of_week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    cols = st.columns(5)

    for i, col in enumerate(cols):
        current_date = start_of_week + timedelta(days=i)
        
        with col:
            # Column headers
            st.markdown(f"**{days_of_week[i]}**")
            st.caption(current_date.strftime('%b %d, %Y'))
            
            # Filter events for this specific day
            day_events = [e for e in events if e["date"] == current_date]
            
            if not day_events:
                st.info("No scheduled events")
            else:
                for event in day_events:
                    with st.container(border=True):
                        st.markdown(f"[{event['title']}]({event['url']})")

if __name__ == "__main__":
    main()