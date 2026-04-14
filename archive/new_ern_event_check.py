import requests
import json
import random
from email.mime.text import MIMEText
import smtplib
import os
import logging
import time

logging.basicConfig(
    filename="/home/devanshjain293/polymarket/notifier.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# --- Config ---
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
EVENT_TAG = "earnings"
QUERY = " "
EMAIL_ADDRESS = "devanshjain293@gmail.com"
EMAIL_PASSWORD = "pzcj luil koda ftcb"
TO_EMAIL = "devanshjain293@gmail.com,jmr18122001@gmail.com"
SEEN_EVENTS_FILE = "/home/devanshjain293/polymarket/seen_events.json"


def load_seen_events():
    if os.path.exists(SEEN_EVENTS_FILE):
        with open(SEEN_EVENTS_FILE, "r") as f:
            return set(json.load(f))
    else:
        # Create an empty file on first run
        with open(SEEN_EVENTS_FILE, "w") as f:
            json.dump([], f)
        return set()


def save_seen_events(seen_event_ids):
    with open(SEEN_EVENTS_FILE, "w") as f:
        json.dump(list(seen_event_ids), f)


def send_email(subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = TO_EMAIL
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)
    logging.info(f"Email sent: {subject}")


def fetch_earnings_events():
    """Fetch all active earnings events with pagination"""
    headers = {
        "User-Agent": f"PolymarketNotifier/1.0-{random.randint(1000,9999)}",
        "Accept": "application/json",
    }
    events = []
    page = 1

    while True:
        params = {
            "q": QUERY,
            "type": "event",
            "events_tag": EVENT_TAG,
            "event_status": "active",
            "limit": 100,
            "page": page,
        }
        resp = requests.get(SEARCH_URL, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        batch = data.get("events") or []
        logging.info(f"Page {page}: got {len(batch)} events")
        events.extend(batch)

        pagination = data.get("pagination", {})
        if not pagination.get("hasMore"):
            break

        page += 1
        time.sleep(0.4)  # polite delay

    return events


def main():
    seen_event_ids = load_seen_events()
    try:
        events = fetch_earnings_events()
        new_events = []
        for e in events:
            eid = e.get("id")
            title = e.get("title") or e.get("question") or "No Title"
            if eid not in seen_event_ids:
                seen_event_ids.add(eid)
                new_events.append((title, eid, e.get("slug")))
        if new_events:
            for title, eid, slug in new_events:
                body = (
                    f"New Earnings Event Detected:\n\n"
                    f"Title: {title}\n"
                    f"ID: {eid}\n"
                    f"URL: https://polymarket.com/event/{slug}"
                )
                logging.info(f"New event found: {title} (ID: {eid})")
                send_email(f"New Earnings Event: {title}", body)
            save_seen_events(seen_event_ids)
        else:
            logging.info("No new events found.")
    except Exception as ex:
        logging.error(f"Error: {ex}")


if __name__ == "__main__":
    logging.info("----- Script started -----")
    main()
    logging.info("----- Script finished -----\n")