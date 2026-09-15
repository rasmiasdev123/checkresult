"""
Odisha Postal Circle - Latest Updates monitor.
Watches ONLY the "Latest Updates - Odisha Postal Circle" box (div.updatesList)
at https://app.indiapost.gov.in/circleportal/odisha

- First run (empty state.json) -> sends the LATEST pdf as proof "Bot working!"
- Manual run with SEND_TEST=1/true -> re-sends latest pdf anytime (bypasses IST gate)
- Normal runs -> sends only truly NEW file IDs, then updates state.json

Env required:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional:
  SEND_TEST=1 (force latest-pdf test), DRY_RUN=1 (parse only, no telegram)
"""
import os
import re
import sys
import json
import html as htmlmod
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests

PAGE_URL = "https://app.indiapost.gov.in/circleportal/odisha"
BASE = "https://app.indiapost.gov.in"
STATE_FILE = Path(__file__).parent / "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}

IST = timezone(timedelta(hours=5, minutes=30))


def fetch_page() -> str:
    r = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def clean_text(raw_html: str) -> str:
    # strip tags, unescape entities, collapse whitespace
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = htmlmod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_notices(html_text: str) -> list:
    """Return ordered list (newest first) of dicts: id, url, title, date, size."""
    notices = []
    seen = set()

    def add(file_id, url, title, date="", size=""):
        file_id = file_id.strip()
        title = re.sub(r"\s+", " ", title).strip()
        # drop junk / footer links
        if not file_id or file_id in seen:
            return
        if len(title) < 10:  # footer / empty anchors
            return
        seen.add(file_id)
        notices.append({
            "id": file_id,
            "url": url if url.startswith("http") else BASE + url,
            "title": title[:500],
            "date": date,
            "size": size,
        })

    # --- Method A: visible anchors (newest-first order, top 5) ---
    # Scoped idea: updatesList anchors all point to /api/documents/file/
    # (footer PDF uses /documents/footer/ so it is auto-ignored)
    anchor_pat = re.compile(
        r'<a[^>]*href="(/circleportal/api/documents/file/([A-Za-z0-9]+))"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in anchor_pat.finditer(html_text):
        url, file_id, inner = m.group(1), m.group(2), m.group(3)
        title = clean_text(inner)
        # inner contains "Title PDF [3.3 MB] [English Version]" -> keep title part
        # cut off trailing "PDF [" metadata
        title = re.split(r"\bPDF\s*\[", title, maxsplit=1)[0].strip()
        add(file_id, url, title)

    # --- Method B: embedded Next.js Flight data (full ~26 incl. Read More) ---
    # {"title":"...","date":"...","size":"...","version":"...","type":"PDF","url":"/circleportal/api/documents/file/xxx"}
    flight_pat = re.compile(
        r'"title"\s*:\s*"((?:\\.|[^"\\])*)".{0,600}?"date"\s*:\s*"([^"]*)".{0,300}?"size"\s*:\s*"([^"]*)".{0,800}?"url"\s*:\s*"(/circleportal/api/documents/file/([A-Za-z0-9]+))"',
        re.DOTALL,
    )
    for m in flight_pat.finditer(html_text):
        raw_title, date, size, url, file_id = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        )
        try:
            title = json.loads(f'"{raw_title}"')
        except Exception:
            title = raw_title
        add(file_id, url, title, date=date, size=size)

    return notices


def load_state() -> list:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        ids = data.get("seen_ids", [])
        return [str(x) for x in ids if x]
    except Exception as e:
        print(f"WARN: could not read state.json: {e}", flush=True)
        return []


def save_state(ids: list):
    STATE_FILE.write_text(
        json.dumps({"seen_ids": ids}, indent=2), encoding="utf-8"
    )
    print(f"Saved {len(ids)} ids to state.json", flush=True)


def send_document(token: str, chat_id: str, pdf_bytes: bytes, filename: str, caption: str):
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    files = {"document": (filename, pdf_bytes, "application/pdf")}
    data = {"chat_id": chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
    r = requests.post(url, data=data, files=files, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"sendDocument failed {r.status_code}: {r.text[:500]}")
    return r.json()


def send_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(
        url,
        data={"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML",
              "disable_web_page_preview": False},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"sendMessage failed {r.status_code}: {r.text[:500]}")


def safe_filename(title: str, file_id: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", title).strip("_")[:60]
    return f"{base or file_id}.pdf"


def caption_for(notice: dict, prefix: str) -> str:
    meta = " ".join(x for x in [notice.get("date"), notice.get("size")] if x)
    meta_line = f"\n📅 {meta}" if meta else ""
    return (
        f"{prefix}\n\n<b>{htmlmod.escape(notice['title'])}</b>"
        f"{meta_line}\n🔗 {PAGE_URL}\n📎 {notice['url']}\n\n⬆️ PDF attached"
    )


def main() -> int:
    send_test = os.getenv("SEND_TEST", "").lower() in ("1", "true", "yes")
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # 24h mode: no IST gate, always check.
    now_ist = datetime.now(IST)

    print(f"Fetching {PAGE_URL} ... (IST {now_ist:%Y-%m-%d %H:%M})", flush=True)
    try:
        html_text = fetch_page()
    except Exception as e:
        print(f"ERROR fetching page: {e}", flush=True)
        return 1

    notices = parse_notices(html_text)
    print(f"Found {len(notices)} notices in Latest Updates box.", flush=True)
    for n in notices[:8]:
        print(f"  - {n['id'][:12]}... | {n['title'][:90]}", flush=True)

    if not notices:
        print("ERROR: parsed 0 notices — site layout may have changed. State NOT updated.", flush=True)
        return 1

    if dry_run:
        print("DRY_RUN=1 — not sending, not writing state.", flush=True)
        return 0

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env missing.", flush=True)
        return 1

    seen_ids = load_state()
    first_run = len(seen_ids) == 0
    current_ids = [n["id"] for n in notices]

    # --- 1) First run: send LATEST pdf as proof ---
    if first_run and not send_test:
        latest = notices[0]
        print(f"First run — sending latest as proof: {latest['title'][:80]}", flush=True)
        pdf = requests.get(latest["url"], headers=HEADERS, timeout=90).content
        send_document(token, chat_id, pdf,
                      safe_filename(latest["title"], latest["id"]),
                      caption_for(latest, "✅ <b>Bot working! Latest notice:</b>"))
        save_state(current_ids)
        return 0

    # --- 2) Manual test button: re-send latest anytime ---
    if send_test:
        latest = notices[0]
        print(f"SEND_TEST — sending latest: {latest['title'][:80]}", flush=True)
        pdf = requests.get(latest["url"], headers=HEADERS, timeout=90).content
        if len(pdf) > 45 * 1024 * 1024:
            send_message(token, chat_id,
                         f"✅ <b>Bot working! Latest notice (PDF too big, link only):</b>\n\n"
                         f"{htmlmod.escape(latest['title'])}\n{latest['url']}")
        else:
            send_document(token, chat_id, pdf,
                          safe_filename(latest["title"], latest["id"]),
                          caption_for(latest, "✅ <b>Test OK — latest notice:</b>"))
        save_state(current_ids)
        return 0

    # --- 3) Normal run: only NEW ids ---
    fresh = [n for n in notices if n["id"] not in set(seen_ids)]
    if not fresh:
        print("No new notices.", flush=True)
        if set(current_ids) != set(seen_ids):
            save_state(current_ids)
        return 0

    # send oldest-first so telegram order reads chronologically
    fresh.reverse()
    print(f"New notices: {len(fresh)}", flush=True)
    for n in fresh:
        try:
            print(f"Sending: {n['title'][:80]}", flush=True)
            resp = requests.get(n["url"], headers=HEADERS, timeout=90)
            resp.raise_for_status()
            pdf = resp.content
            if len(pdf) < 1024 or len(pdf) > 45 * 1024 * 1024:
                raise ValueError(f"Bad PDF size {len(pdf)} bytes, sending link only")
            send_document(token, chat_id, pdf,
                          safe_filename(n["title"], n["id"]),
                          caption_for(n, "🔔 <b>New Notice - Odisha Postal Circle</b>"))
        except Exception as e:
            print(f"Document upload failed ({e}), sending link fallback.", flush=True)
            send_message(token, chat_id,
                         f"🔔 <b>New Notice - Odisha Postal Circle</b>\n\n"
                         f"{htmlmod.escape(n['title'])}\n🔗 {n['url']}\n(PDF download failed: {htmlmod.escape(str(e)[:200])})")

    save_state(current_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
