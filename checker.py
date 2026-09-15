"""
Odisha Postal Circle - Latest Updates monitor.
Watches ONLY the "Latest Updates - Odisha Postal Circle" box (div.updatesList)
at https://app.indiapost.gov.in/circleportal/odisha

- First run (empty state.json) -> sends the LATEST pdf as proof "Bot working!"
- Manual run with SEND_TEST=1/true -> re-sends latest pdf anytime (bypasses IST gate)
- Normal runs -> compares every notice by name+link hash, not counts:
  unseen name/link -> "🔔 New" alert, same link with a changed name
  -> "♻️ Updated" alert. Vanished items are kept silently so a flaky
  parse can never re-alert. (Hash covers file link + normalized name only,
  so it is stable across fetch routes and metadata formats.)
- Optional result focus: KEYWORDS mark exam-result notices with ⭐;
  RESULT_ONLY=1 sends only keyword matches.

Env required:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional:
  SEND_TEST=1 (force latest-pdf test), DRY_RUN=1 (classify only, no send/state write),
  BASELINE=1 (one-shot: record all live notices, send nothing — use after upgrades),
  KEYWORDS="result,ldce,merit,..." (default targets exam results),
  RESULT_ONLY=1 (send only keyword matches),
  RUN_LOOP=1, LOOP_MINUTES=5 (continuous mode for systemd)
"""
import hashlib
import os
import re
import sys
import json
import html as htmlmod
from pathlib import Path
from datetime import datetime, timedelta, timezone

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PAGE_URL = "https://app.indiapost.gov.in/circleportal/odisha"
BASE = "https://app.indiapost.gov.in"
STATE_FILE = Path(__file__).parent / "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

IST = timezone(timedelta(hours=5, minutes=30))


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, status=3,
        backoff_factor=5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


def _page_candidates():
    from urllib.parse import quote
    enc = quote(PAGE_URL, safe="")
    return [
        ("direct", PAGE_URL),
        ("allorigins", "https://api.allorigins.win/raw?url=" + enc),
        ("codetabs", "https://api.codetabs.com/v1/proxy?quest=" + enc),
        ("corsproxy", "https://corsproxy.io/?url=" + enc),
        ("jina", "https://localhost:3000/" + PAGE_URL),
    ]


def _looks_like_page(text: str) -> bool:
    return ("updatesList" in text) or ("/api/documents/file/" in text)


def fetch_page() -> str:
    last_err = None
    for name, url in _page_candidates():
        for attempt in range(1, 3):
            try:
                print(f"Fetch via {name} attempt {attempt}/2 ...", flush=True)
                r = _session().get(url, headers=HEADERS, timeout=(30, 120))
                r.raise_for_status()
                text = r.text
                if _looks_like_page(text):
                    print(f"OK via {name} ({len(text)} chars)", flush=True)
                    return text
                print(f"{name} returned {len(text)} chars but no notices, retrying...", flush=True)
            except Exception as e:
                last_err = e
                print(f"{name} attempt {attempt} failed: {str(e)[:200]}", flush=True)
                time.sleep(5 * attempt)
    raise last_err or RuntimeError("all fetch routes failed")


def download_pdf(pdf_url: str) -> bytes:
    from urllib.parse import quote
    cands = [
        ("direct", pdf_url),
        ("allorigins", "https://api.allorigins.win/raw?url=" + quote(pdf_url, safe="")),
        ("codetabs", "https://api.codetabs.com/v1/proxy?quest=" + quote(pdf_url, safe="")),
        ("corsproxy", "https://corsproxy.io/?url=" + quote(pdf_url, safe="")),
    ]
    last_err = None
    for name, url in cands:
        try:
            print(f"PDF via {name} ...", flush=True)
            resp = _session().get(url, headers=HEADERS, timeout=(30, 180))
            resp.raise_for_status()
            data = resp.content
            if data[:5] == b"%PDF-":
                print(f"PDF OK via {name} ({len(data)} bytes)", flush=True)
                return data
            print(f"{name} did not return PDF (got {len(data)} bytes), trying next...", flush=True)
        except Exception as e:
            last_err = e
            print(f"PDF via {name} failed: {str(e)[:200]}", flush=True)
    raise last_err or RuntimeError("all PDF routes failed")


def clean_text(raw_html: str) -> str:
    # strip tags, unescape entities, collapse whitespace
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = htmlmod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def norm_name(t: str) -> str:
    """Normalized notice name: lowercase, punctuation dropped, spaces collapsed."""
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def notice_hash(file_id: str, title: str) -> str:
    # file link + normalized name only: stable across fetch routes and
    # metadata formats (this CMS mints a new file id per upload, so any
    # new/revised PDF is a new link -> NEW alert; a retitle -> UPDATED).
    base = f"{file_id}|{norm_name(title)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def is_result(title: str, keywords) -> bool:
    name = norm_name(title)
    return any(k in name for k in keywords)


def find_stored(state: dict, n: dict):
    """Match a live notice to its stored record by key, stable CMS id, or file id."""
    if n["key"] in state:
        return n["key"], state[n["key"]]
    if n.get("uid"):
        for k, v in state.items():
            if v.get("uid") == n["uid"]:
                return k, v
    for k, v in state.items():
        if v.get("file_id") == n["id"]:
            return k, v
    return None, None


def classify(notices: list, state: dict):
    """Split live notices into (fresh, updated). Updated = same notice, changed name/meta."""
    fresh, updated = [], []
    for n in notices:
        _, stored = find_stored(state, n)
        if stored is None:
            fresh.append(n)
        elif stored.get("hash") and stored["hash"] != n["hash"]:
            updated.append((stored, n))
    return fresh, updated


def snapshot(notices: list, state: dict) -> dict:
    """Full state to persist. Vanished entries are kept so flaky parses never re-alert."""
    snap = {}
    for n in notices:
        snap[n["key"]] = {"file_id": n["id"], "uid": n.get("uid", ""),
                          "slug": n.get("slug", ""), "title": n["title"],
                          "date": n.get("date", ""), "size": n.get("size", ""),
                          "url": n["url"], "hash": n["hash"]}
    for k, v in state.items():
        snap.setdefault(k, v)
    return snap


def parse_notices(html_text: str) -> list:
    """Return ordered list (newest first) of dicts: key/id/uid/slug/url/title/date/size/hash."""
    notices = []
    by_file = {}

    def add(file_id, url, title, date="", size=""):
        file_id = file_id.strip()
        title = re.sub(r"\s+", " ", title).strip()
        # drop junk / footer links
        if not file_id or len(title) < 10:  # footer / empty anchors
            return
        if file_id in by_file:  # same file seen twice: fill in missing meta
            cur = by_file[file_id]
            if not cur.get("date") and date:
                cur["date"] = date
            if not cur.get("size") and size:
                cur["size"] = size
            if len(title) > len(cur["title"]):
                cur["title"] = title[:500]
            return
        entry = {
            "key": f"file:{file_id}",
            "id": file_id,
            "uid": "",
            "slug": "",
            "url": url if url.startswith("http") else BASE + url,
            "title": title[:500],
            "date": date,
            "size": size,
        }
        by_file[file_id] = entry
        notices.append(entry)

    # --- Method A1: full items (date badge + anchor + size, newest first) ---
    # Each box item = day/month badge, then the PDF anchor with title + [size].
    item_pat = re.compile(
        r'<p[^>]*>(\d{1,2})</p>\s*<p[^>]*>(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)</p>'
        r'.*?<a[^>]*href="(/circleportal/api/documents/file/([A-Za-z0-9]+))"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in item_pat.finditer(html_text):
        day, mon, url, file_id, inner = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        title = clean_text(inner)
        title = re.split(r"\bPDF\s*\[", title, maxsplit=1)[0].strip()
        size_m = re.search(r"\[([\d.]+\s*(?:KB|MB))\]", inner, re.IGNORECASE)
        add(file_id, url, title, date=f"{day} {mon}",
            size=size_m.group(1) if size_m else "")

    # --- Method A2: bare anchors fallback (same links, no badge) ---
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
    # Flight payload is double-escaped in the HTML (\"...\"), so unescape first.
    flat = html_text.replace('\\"', '"').replace('\\/', '/')
    # {"title":"...","date":"...","size":"...","version":"...","type":"PDF","url":"/circleportal/api/documents/file/xxx"}
    flight_pat = re.compile(
        r'"title"\s*:\s*"((?:\\.|[^"\\])*)".{0,600}?"date"\s*:\s*"([^"]*)".{0,300}?"size"\s*:\s*"([^"]*)".{0,800}?"url"\s*:\s*"(/circleportal/api/documents/file/([A-Za-z0-9]+))"',
        re.DOTALL,
    )
    for m in flight_pat.finditer(flat):
        raw_title, date, size, url, file_id = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        )
        try:
            title = json.loads(f'"{raw_title}"')
        except Exception:
            title = raw_title
        add(file_id, url, title, date=date, size=size)

    # --- Method C: bare URLs (proxy/markdown fallback, e.g. Jina reader) ---
    # Matches markdown links [title](.../file/xxx) or raw .../file/xxx occurrences.
    if not notices:
        md_pat = re.compile(
            r'\[([^\]]{10,300})\]\((?:https?://app\.indiapost\.gov\.in)?(/circleportal/api/documents/file/([A-Za-z0-9]+))\)'
        )
        for m in md_pat.finditer(html_text):
            title, url, file_id = m.group(1), m.group(2), m.group(3)
            add(file_id, url, clean_text(title))
    if not notices:
        for m in re.finditer(r'/circleportal/api/documents/file/([A-Za-z0-9]+)', html_text):
            file_id = m.group(1)
            # grab ~120 chars before as pseudo-title
            start = max(0, m.start() - 200)
            snippet = clean_text(html_text[start:m.start()])[-150:]
            add(file_id, "/circleportal/api/documents/file/" + file_id,
                snippet or f"Notice {file_id[:8]}")

    # --- Attach stable CMS ids (uid/slug) when Flight data is present ---
    # Lets us recognize "same notice, re-uploaded PDF" as an update, not a new notice.
    uid_pat = re.compile(
        r'"id"\s*:\s*"([0-9a-fA-F-]{8,})".{0,400}?"slug"\s*:\s*"([^"]{3,200})".{0,1500}?/circleportal/api/documents/file/([A-Za-z0-9]+)',
        re.DOTALL,
    )
    uid_by_file = {}
    for m in uid_pat.finditer(flat):
        uid_by_file[m.group(3)] = (m.group(1), m.group(2))
    for n in notices:
        # key stays file-based so alerts never duplicate when fetch routes
        # return different metadata shapes; uid is a cross-match attribute.
        if n["id"] in uid_by_file:
            n["uid"], n["slug"] = uid_by_file[n["id"]]
        n["hash"] = notice_hash(n["id"], n["title"])

    return notices


def load_state() -> dict:
    """Return {key: record}. Auto-migrates legacy {"seen_ids": [...]} format."""
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARN: could not read state.json: {e}", flush=True)
        return {}
    if isinstance(data, dict) and isinstance(data.get("notices"), dict):
        return data["notices"]
    ids = data.get("seen_ids", []) if isinstance(data, dict) else []
    migrated = {f"file:{x}": {"file_id": str(x)} for x in ids if x}
    if migrated:
        print(f"Migrating legacy state: {len(migrated)} names -> record format", flush=True)
    return migrated


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps({"notices": state}, indent=2), encoding="utf-8"
    )
    print(f"Saved {len(state)} notices to state.json", flush=True)


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


def caption_for(notice: dict, prefix: str, old=None) -> str:
    meta = " ".join(x for x in [notice.get("date"), notice.get("size")] if x)
    meta_line = f"\n📅 {meta}" if meta else ""
    changed = ""
    if old and old.get("title") and norm_name(old["title"]) != norm_name(notice["title"]):
        changed = f"\n<i>Was: {htmlmod.escape(old['title'][:200])}</i>"
    return (
        f"{prefix}\n\n<b>{htmlmod.escape(notice['title'])}</b>"
        f"{changed}{meta_line}\n🔗 {PAGE_URL}\n📎 {notice['url']}\n\n⬆️ PDF attached"
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    except Exception:
        pass
    send_test = os.getenv("SEND_TEST", "").lower() in ("1", "true", "yes")
    dry_run = os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    keywords = [norm_name(k) for k in os.getenv(
        "KEYWORDS", "result,ldce,merit,select list,supplementary,provisional").split(",") if k.strip()]
    result_only = os.getenv("RESULT_ONLY", "").lower() in ("1", "true", "yes")

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
        star = "⭐" if is_result(n["title"], keywords) else " "
        print(f"  {star} {n['id'][:12]}... | {n['title'][:90]}", flush=True)

    if not notices:
        print("ERROR: parsed 0 notices — site layout may have changed. State NOT updated.", flush=True)
        return 1

    state = load_state()
    first_run = len(state) == 0

    if dry_run:
        if first_run:
            print(f"DRY_RUN — first run: would send latest as proof: {notices[0]['title'][:80]}", flush=True)
        else:
            fresh, updated = classify(notices, state)
            print(f"DRY_RUN — new: {len(fresh)}, updated: {len(updated)}", flush=True)
            for n in fresh:
                tag = "⭐RESULT" if is_result(n["title"], keywords) else "notice"
                print(f"  [NEW {tag}] {n['title'][:90]}", flush=True)
            for old, n in updated:
                print(f"  [UPDATED] {n['title'][:90]}", flush=True)
                if old.get("title") and norm_name(old["title"]) != norm_name(n["title"]):
                    print(f"      was: {old['title'][:90]}", flush=True)
        print("DRY_RUN=1 — not sending, not writing state.", flush=True)
        return 0

    if os.getenv("BASELINE", "").lower() in ("1", "true", "yes"):
        save_state(snapshot(notices, state))
        print(f"BASELINE=1 — recorded {len(notices)} live notices, sent nothing.", flush=True)
        return 0

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env missing.", flush=True)
        return 1

    # --- 1) First run: send LATEST pdf as proof ---
    if first_run and not send_test:
        latest = notices[0]
        print(f"First run — sending latest as proof: {latest['title'][:80]}", flush=True)
        pdf = download_pdf(latest["url"])
        send_document(token, chat_id, pdf,
                      safe_filename(latest["title"], latest["id"]),
                      caption_for(latest, "✅ <b>Bot working! Latest notice:</b>"))
        save_state(snapshot(notices, state))
        return 0

    # --- 2) Manual test: re-send latest anytime ---
    if send_test:
        latest = notices[0]
        print(f"SEND_TEST — sending latest: {latest['title'][:80]}", flush=True)
        pdf = download_pdf(latest["url"])
        if len(pdf) > 45 * 1024 * 1024:
            send_message(token, chat_id,
                         f"✅ <b>Bot working! Latest notice (PDF too big, link only):</b>\n\n"
                         f"{htmlmod.escape(latest['title'])}\n{latest['url']}")
        else:
            send_document(token, chat_id, pdf,
                          safe_filename(latest["title"], latest["id"]),
                          caption_for(latest, "✅ <b>Test OK — latest notice:</b>"))
        save_state(snapshot(notices, state))
        return 0

    # --- 3) Normal run: NEW names/links + UPDATED (retitled/revised) notices ---
    fresh, updated = classify(notices, state)
    vanished = set(state) - {n["key"] for n in notices}
    if vanished:
        print(f"{len(vanished)} stored notice(s) not currently visible — kept silently.", flush=True)
    if not fresh and not updated:
        print("No new or updated notices.", flush=True)
        save_state(snapshot(notices, state))
        return 0

    jobs = [("new", None, n) for n in reversed(fresh)]
    jobs += [("updated", old, n) for old, n in reversed(updated)]
    print(f"New: {len(fresh)}, updated: {len(updated)}", flush=True)
    for kind, old, n in jobs:
        res = is_result(n["title"], keywords)
        if result_only and not res:
            print(f"Skipped (non-result, RESULT_ONLY=1): {n['title'][:80]}", flush=True)
            continue
        if kind == "new":
            prefix = ("⭐ <b>RESULT — Odisha Postal Circle</b>" if res
                      else "🔔 <b>New Notice - Odisha Postal Circle</b>")
        else:
            prefix = ("⭐ <b>RESULT updated — Odisha Postal Circle</b>" if res
                      else "♻️ <b>Updated notice - Odisha Postal Circle</b>")
        try:
            print(f"Sending ({kind}): {n['title'][:80]}", flush=True)
            pdf = download_pdf(n["url"])
            if len(pdf) < 1024 or len(pdf) > 45 * 1024 * 1024:
                raise ValueError(f"Bad PDF size {len(pdf)} bytes, sending link only")
            send_document(token, chat_id, pdf,
                          safe_filename(n["title"], n["id"]),
                          caption_for(n, prefix, old))
        except Exception as e:
            print(f"Document upload failed ({e}), sending link fallback.", flush=True)
            send_message(token, chat_id,
                         f"{prefix}\n\n"
                         f"{htmlmod.escape(n['title'])}\n🔗 {n['url']}\n(PDF download failed: {htmlmod.escape(str(e)[:200])})")

    save_state(snapshot(notices, state))
    return 0


def _load_dotenv():
    """Load KEY=VALUE from .env next to this script (EC2 service mode)."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


if __name__ == "__main__":
    _load_dotenv()
    if os.getenv("RUN_LOOP", "").lower() in ("1", "true", "yes"):
        mins = float(os.getenv("LOOP_MINUTES", "5"))
        print(f"RUN_LOOP=1 — checking every {mins} min.", flush=True)
        while True:
            try:
                main()
            except Exception as e:
                print(f"Loop iteration failed: {e}", flush=True)
            time.sleep(mins * 60)
    else:
        sys.exit(main())
