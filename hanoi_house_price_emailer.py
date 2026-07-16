#!/usr/bin/env python3
"""
Hanoi House/Land Prices (multiple sources) -> Email
(runs on GitHub Actions, no local computer needed)

Same shape as the gold-price-emailer / 9gag-meme-emailer this is modeled on:
fetches price data, then emails an HTML digest via Gmail SMTP. Runs in two
phases so the workflow can persist dedup state *between* them:

    python hanoi_house_price_emailer.py generate
        -> scrapes each source, writes the composed email
           (subject/html/text) under ./email/, and updates the
           "last sent price" state file

    python hanoi_house_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

WHY MULTIPLE SOURCES
---------------------
Vietnamese gold prices have one clean daily aggregator (giavang.org) with a
simple table per seller. Hanoi housing prices don't have a real
equivalent - there's no single site with a clean, frequently-updated,
per-district table. So this script hedges by pulling from more than one
source and treating each independently: if one site blocks scrapers,
changes its markup, or goes down, the email still sends with whatever
other source(s) succeeded, each clearly labeled with where its numbers
came from (same spirit as the gold script's per-seller error handling -
one seller failing doesn't take down the whole email).

Current sources:
  1. Mogi.vn "Giá nhà đất" page - one blended average price/m2 per
     district (house + land together), with a month-over-month % change.
     https://mogi.vn/gia-nha-dat
  2. Batdongsan.com.vn's per-district "nhà mặt phố" (street-front house)
     pages - a min-max price/m2 range per district, one page per district
     (see DISTRICT_SLUGS).
     https://batdongsan.com.vn/ban-nha-mat-pho-{slug}

Neither of these is a clean "apartment-only" table - that split doesn't
appear to exist anywhere as clean scrapable public data (see README.md).
If a run reports 0 rows for a source, that source's fetch_page() call now
prints diagnostics (HTTP status, response length, and whether the page
looks like a JS/anti-bot challenge page rather than real content) to
stderr - if you hit this, paste that diagnostic output back so the parser
can be fixed for real rather than guessed at blind.

SETUP
-----
1. Install dependencies:
     pip install requests beautifulsoup4 certifi

2. Create a Gmail "App Password" (regular Gmail passwords won't work with SMTP):
     - Go to https://myaccount.google.com/apppasswords
     - You need 2-Step Verification turned on first.
     - Create an app password for "Mail" and copy the 16-character code.

3. Set these as environment variables (see README.md for GitHub Actions
   secrets instead, if running in the cloud):
     export GMAIL_ADDRESS="youraddress@gmail.com"
     export GMAIL_APP_PASSWORD="16-char-app-password"
     export HOUSE_RECIPIENT="where-to-send@example.com"
     export SEND_ONLY_ON_CHANGE="false"        # optional, default false
     export TIMEZONE="Asia/Ho_Chi_Minh"        # optional, for the subject line
     export STATE_FILE="state/last_price.json" # optional, dedup state file
     export ALLOW_INSECURE_SSL_FALLBACK="false" # optional, last-resort TLS bypass

NOTE ON SCRAPING
-----------------
Always worth checking the current robots.txt / terms of whatever sites
this is pointed at before running it unattended long-term, e.g.:
https://mogi.vn/robots.txt
https://batdongsan.com.vn/robots.txt
"""

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import unicodedata
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import certifi
import requests
import urllib3
from bs4 import BeautifulSoup

if os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true":
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}

EMAIL_DIR = "email"
STATE_FILE = os.environ.get("STATE_FILE", "state/last_price.json")
SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "false").lower() == "true"
ALLOW_INSECURE_SSL_FALLBACK = os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true"

# Text markers that suggest we got a JS/anti-bot challenge page instead of
# real content (Cloudflare and friends). Not exhaustive, just enough to
# turn "0 rows, no idea why" into an actionable diagnostic.
CHALLENGE_MARKERS = [
    "just a moment", "cf-browser-verification", "checking your browser",
    "enable javascript and cookies", "attention required", "cf-chl",
    "captcha",
]


def norm(s):
    """Collapse NBSP/whitespace and normalize to NFC so Vietnamese
    diacritics compare equal regardless of which composed/decomposed form
    a given page happens to send them in (a silent source of 0-match bugs
    otherwise).
    """
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return unicodedata.normalize("NFC", s)


def load_last_hash(path=STATE_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f).get("hash")
    except (json.JSONDecodeError, OSError) as e:
        print(f"  could not read {path} ({e}) - starting with empty dedup state", file=sys.stderr)
        return None


def save_last_hash(price_hash, path=STATE_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"hash": price_hash, "updated": datetime.utcnow().isoformat() + "Z"}, f)


def hash_data(data):
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fetch_page(url, label=""):
    """GET a page, verifying TLS against certifi's CA bundle explicitly,
    and print diagnostics that actually help next time instead of a bare
    "0 rows parsed": status code, response length, and whether the page
    text looks like a JS/anti-bot challenge rather than real content.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=certifi.where())
    except requests.exceptions.SSLError as e:
        print(f"  [{label}] TLS verification failed with certifi's CA bundle: {e}", file=sys.stderr)
        if not ALLOW_INSECURE_SSL_FALLBACK:
            print(
                f"  [{label}] Set ALLOW_INSECURE_SSL_FALLBACK=true to retry without "
                "verification as a last resort.",
                file=sys.stderr,
            )
            raise
        print(f"  [{label}] ALLOW_INSECURE_SSL_FALLBACK=true - retrying without TLS verification.", file=sys.stderr)
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)

    print(f"  [{label}] GET {url} -> HTTP {resp.status_code}, {len(resp.text)} bytes", file=sys.stderr)
    lowered = resp.text.lower()
    hit = next((m for m in CHALLENGE_MARKERS if m in lowered), None)
    if hit:
        print(
            f"  [{label}] Response contains '{hit}' - this looks like a JS/anti-bot "
            "challenge page, not real content. Plain HTTP scraping likely won't work "
            "for this source without changes (different headers/cookies/session, or "
            "a headless-browser fetch).",
            file=sys.stderr,
        )
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# Source 1: Mogi.vn - blended average price/m2 per district, with a
# month-over-month % change. One page covers every Hanoi district.
# ---------------------------------------------------------------------------

MOGI_URL = os.environ.get("MOGI_URL", "https://mogi.vn/gia-nha-dat")

HANOI_AREAS = [
    "Quận Ba Đình", "Quận Cầu Giấy", "Quận Đống Đa", "Quận Hai Bà Trưng",
    "Quận Hoàn Kiếm", "Quận Hoàng Mai", "Quận Long Biên", "Quận Tây Hồ",
    "Quận Thanh Xuân", "Quận Hà Đông", "Quận Bắc Từ Liêm", "Quận Nam Từ Liêm",
    "Huyện Mê Linh", "Huyện Ba Vì", "Huyện Chương Mỹ", "Huyện Đan Phượng",
    "Huyện Hoài Đức", "Huyện Phúc Thọ", "Huyện Quốc Oai", "Huyện Thạch Thất",
    "Huyện Thanh Oai", "Huyện Thường Tín", "Thị Xã Sơn Tây", "Huyện Đông Anh",
    "Huyện Gia Lâm", "Huyện Sóc Sơn", "Huyện Thanh Trì", "Huyện Mỹ Đức",
    "Huyện Phú Xuyên", "Huyện Ứng Hòa",
]

PRICE_RE = re.compile(r"([\d][\d.,]*)\s*triệu\s*/\s*m2", re.IGNORECASE)
PERCENT_RE = re.compile(r"([\d][\d.,]*)\s*%")


def parse_mogi(html):
    """Parse Mogi.vn's Hanoi section into [{area, price, change, direction}].

    Matches by district name against the page's flattened text (more
    resilient to markup changes than a strict DOM walk), then reads the
    price + % change off the single line immediately following the
    district name - that's where mogi.vn renders them together (e.g.
    "214 triệu/m2  4,9% ▲", or "207 triệu/m2  —" when unchanged). The
    up/down arrow can land in its own separate line (its own inline tag),
    so that's checked as a narrow special case, not folded into a
    multi-line lookahead - an earlier version of this scanned several
    lines ahead for the % figure, which let one district's change bleed
    into the row above it.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [norm(l) for l in text.split("\n") if norm(l)]
    areas_norm = {norm(a): a for a in HANOI_AREAS}

    rows = []
    seen = set()
    for i, line in enumerate(lines):
        area = areas_norm.get(line)
        if not area or area in seen or i + 1 >= len(lines):
            continue
        w = lines[i + 1]
        m = PRICE_RE.search(w)
        if not m:
            continue
        price = m.group(1)
        change = None
        direction = None
        pm = PERCENT_RE.search(w)
        if pm:
            change = pm.group(1)
            direction = "up" if "▲" in w else ("down" if "▼" in w else None)
            if direction is None and i + 2 < len(lines) and lines[i + 2] in ("▲", "▼"):
                direction = "up" if lines[i + 2] == "▲" else "down"
        seen.add(area)
        rows.append({"area": area, "price": price, "change": change, "direction": direction})
    return rows


def fetch_mogi():
    html = fetch_page(MOGI_URL, label="Mogi.vn")
    return parse_mogi(html)


def mogi_change_html(change, direction):
    if not change:
        return "<span style='color:#999'>—</span>"
    color = "#1a7f37" if direction == "up" else ("#cf222e" if direction == "down" else "#666")
    arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "")
    return f"<span style='color:{color}'>{escape(change)}% {arrow}</span>"


def mogi_html_block(rows):
    if not rows:
        return f"<p>Could not parse this source this run. Check <a href='{escape(MOGI_URL)}'>{escape(MOGI_URL)}</a> directly.</p>"
    row_html = "\n".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['price'])} triệu/m²</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{mogi_change_html(r['change'], r['direction'])}</td>"
        f"</tr>"
        for r in rows
    )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
    <thead>
        <tr style="background:#f5f5f5;">
        <th style="padding:8px 12px;text-align:left;">Quận / Huyện</th>
        <th style="padding:8px 12px;text-align:right;">Giá trung bình</th>
        <th style="padding:8px 12px;text-align:right;">So với tháng trước</th>
        </tr>
    </thead>
    <tbody>
        {row_html}
    </tbody>
    </table>"""


def mogi_text_block(rows):
    if not rows:
        return f"Could not parse this source this run. Check {MOGI_URL}"
    lines = []
    for r in rows:
        change_str = f"{r['change']}%" if r["change"] else "—"
        lines.append(f"{r['area']}: {r['price']} trieu/m2 ({change_str} so voi thang truoc)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source 2: Batdongsan.com.vn - one page per district, each giving a
# min-max price/m2 range for street-front houses ("nhà mặt phố"). Fetching
# per-district pages (rather than hoping for one summary page) mirrors the
# gold script's per-seller pattern: one district failing doesn't take
# down the others.
# ---------------------------------------------------------------------------

DISTRICT_SLUGS = [
    ("Ba Đình", "ba-dinh"),
    ("Hoàn Kiếm", "hoan-kiem"),
    ("Đống Đa", "dong-da"),
    ("Hai Bà Trưng", "hai-ba-trung"),
    ("Tây Hồ", "tay-ho"),
    ("Cầu Giấy", "cau-giay"),
    ("Thanh Xuân", "thanh-xuan"),
    ("Hoàng Mai", "hoang-mai"),
    ("Long Biên", "long-bien"),
    ("Hà Đông", "ha-dong"),
    ("Nam Từ Liêm", "nam-tu-liem"),
    ("Bắc Từ Liêm", "bac-tu-liem"),
]

BATDONGSAN_BASE = os.environ.get("BATDONGSAN_BASE", "https://batdongsan.com.vn/ban-nha-mat-pho-")

# e.g. "219 triệu/m² - 2653 tr/m² triệu/m²" (unit repeated after each number)
BDS_RANGE_RE = re.compile(
    r"([\d][\d.,]*)\s*(?:triệu|tr)(?:\s*đồng)?\s*/\s*m2?²?\s*-\s*([\d][\d.,]*)\s*(?:tr\s*/\s*m2?²?\s*)?(?:triệu|tr)(?:\s*đồng)?\s*/\s*m2?²?",
    re.IGNORECASE,
)
# e.g. "42 - 81 triệu đồng/m2" (unit stated once, shared, at the end of the range)
BDS_RANGE_SHARED_UNIT_RE = re.compile(
    r"([\d][\d.,]*)\s*-\s*([\d][\d.,]*)\s*(?:triệu|tr)(?:\s*đồng)?\s*/\s*m2?²?",
    re.IGNORECASE,
)


def parse_batdongsan_district(html):
    """Pull a min-max price/m2 range for street-front houses out of a
    batdongsan.com.vn district page. Returns (low, high) strings, or None
    if no range-shaped text was found. Tries the repeated-unit phrasing
    first (more specific, avoids false positives from unrelated number
    ranges on the page), then falls back to the shared-unit-at-the-end
    phrasing.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = norm(soup.get_text(" "))
    m = BDS_RANGE_RE.search(text) or BDS_RANGE_SHARED_UNIT_RE.search(text)
    if not m:
        return None
    return m.group(1), m.group(2)


def fetch_batdongsan():
    rows = []
    for name, slug in DISTRICT_SLUGS:
        url = f"{BATDONGSAN_BASE}{slug}"
        try:
            html = fetch_page(url, label=f"Batdongsan.com.vn/{slug}")
            result = parse_batdongsan_district(html)
        except requests.RequestException as e:
            print(f"  [Batdongsan.com.vn/{slug}] fetch failed: {e}", file=sys.stderr)
            rows.append({"area": name, "url": url, "error": str(e)})
            continue
        if result is None:
            rows.append({"area": name, "url": url, "error": "Could not parse a price range from this page."})
        else:
            rows.append({"area": name, "url": url, "low": result[0], "high": result[1]})
    return rows


def batdongsan_html_block(rows):
    if not rows:
        return "<p>Could not fetch this source this run.</p>"
    parts = []
    for r in rows:
        if "error" in r:
            parts.append(
                f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
                f"<td colspan='2' style='padding:6px 12px;border-bottom:1px solid #eee;color:#a33;font-size:13px'>"
                f"Không lấy được dữ liệu ({escape(r['error'])}). "
                f"<a href='{escape(r['url'])}'>Xem trực tiếp</a></td></tr>"
            )
        else:
            parts.append(
                f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
                f"<td colspan='2' style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>"
                f"{escape(r['low'])} - {escape(r['high'])} triệu/m²</td></tr>"
            )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
    <thead>
        <tr style="background:#f5f5f5;">
        <th style="padding:8px 12px;text-align:left;">Quận</th>
        <th colspan="2" style="padding:8px 12px;text-align:right;">Khoảng giá nhà mặt phố</th>
        </tr>
    </thead>
    <tbody>
        {"".join(parts)}
    </tbody>
    </table>"""


def batdongsan_text_block(rows):
    if not rows:
        return "Could not fetch this source this run."
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"{r['area']}: khong lay duoc du lieu ({r['error']}). Xem tai {r['url']}")
        else:
            lines.append(f"{r['area']}: {r['low']} - {r['high']} trieu/m2")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email assembly
# ---------------------------------------------------------------------------

def resolve_timestamp():
    timezone_name = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    return now, now.strftime("%H:%M %d/%m/%Y")


def build_html(mogi_rows, mogi_ok, bds_rows, bds_ok, timestamp):
    mogi_section = mogi_html_block(mogi_rows) if mogi_ok else (
        f"<p>Nguồn này lỗi lần này. Xem <a href='{escape(MOGI_URL)}'>{escape(MOGI_URL)}</a> trực tiếp.</p>"
    )
    bds_section = batdongsan_html_block(bds_rows) if bds_ok else (
        "<p>Nguồn này lỗi lần này.</p>"
    )
    return f"""\
<html>
<body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
  <h1 style="color:#1a5fb4;">Giá nhà đất Hà Nội theo quận/huyện</h1>
  <p style="color:#555;">Cập nhật {escape(timestamp)}</p>

  <h2 style="color:#333;font-size:16px;border-bottom:2px solid #1a5fb4;padding-bottom:4px;">
    Nguồn 1: Mogi.vn - giá nhà đất bình quân (nhà + đất, gộp)
  </h2>
  {mogi_section}

  <h2 style="color:#333;font-size:16px;border-bottom:2px solid #888;padding-bottom:4px;margin-top:24px;">
    Nguồn 2: Batdongsan.com.vn - khoảng giá nhà mặt phố
  </h2>
  {bds_section}

  <p style="color:#999; font-size:12px; margin-top:20px;">
    Nguồn: <a href="{escape(MOGI_URL)}">{escape(MOGI_URL)}</a> ·
    <a href="{escape(BATDONGSAN_BASE)}...">batdongsan.com.vn</a> ·
    Chưa có nguồn công khai, cập nhật thường xuyên, tách riêng theo chung cư -
    xem README nếu bạn biết một nguồn như vậy ·
    Đơn vị: triệu đồng/m² · Email tự động, chỉ mang tính tham khảo, không phải
    lời khuyên đầu tư.
  </p>
</body>
</html>"""


def build_plain_text(mogi_rows, mogi_ok, bds_rows, bds_ok, timestamp):
    lines = [f"Gia nha dat Ha Noi theo quan/huyen - cap nhat {timestamp}", ""]
    lines.append("== NGUON 1: MOGI.VN (gia nha dat binh quan) ==")
    lines.append(mogi_text_block(mogi_rows) if mogi_ok else f"Nguon nay loi lan nay. Xem {MOGI_URL}")
    lines.append("")
    lines.append("== NGUON 2: BATDONGSAN.COM.VN (khoang gia nha mat pho) ==")
    lines.append(batdongsan_text_block(bds_rows) if bds_ok else "Nguon nay loi lan nay.")
    return "\n".join(lines)


def cmd_generate():
    if os.path.exists(EMAIL_DIR):
        for f in os.listdir(EMAIL_DIR):
            os.remove(os.path.join(EMAIL_DIR, f))
    os.makedirs(EMAIL_DIR, exist_ok=True)

    print(f"Fetching source 1: {MOGI_URL} ...")
    mogi_rows, mogi_ok = [], True
    try:
        mogi_rows = fetch_mogi()
        print(f"  Mogi.vn: parsed {len(mogi_rows)} row(s).")
        if not mogi_rows:
            print("  0 rows - see diagnostics above. Page markup may have changed, or Mogi may be blocking this request.", file=sys.stderr)
    except requests.RequestException as e:
        print(f"  Mogi.vn: fetch failed entirely: {e}", file=sys.stderr)
        mogi_ok = False

    print(f"Fetching source 2: {BATDONGSAN_BASE}{{district}} x{len(DISTRICT_SLUGS)} ...")
    bds_rows, bds_ok = [], True
    try:
        bds_rows = fetch_batdongsan()
        ok_count = sum(1 for r in bds_rows if "error" not in r)
        print(f"  Batdongsan.com.vn: {ok_count}/{len(bds_rows)} district(s) OK.")
    except Exception as e:
        print(f"  Batdongsan.com.vn: failed entirely: {e}", file=sys.stderr)
        bds_ok = False

    combined = {"mogi": mogi_rows, "bds": bds_rows}
    price_hash = hash_data(combined)
    last_hash = load_last_hash()

    any_rows = bool(mogi_rows) or any("error" not in r for r in bds_rows)
    if any_rows and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Gia nha dat Ha Noi - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(mogi_rows, mogi_ok, bds_rows, bds_ok, timestamp)
    text_body = build_plain_text(mogi_rows, mogi_ok, bds_rows, bds_ok, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"send": True, "mogi_rows": len(mogi_rows), "bds_rows": len(bds_rows)}, f)

    save_last_hash(price_hash)
    print(f"Generated email (Mogi: {len(mogi_rows)} rows, Batdongsan: {len(bds_rows)} rows). Saved to ./{EMAIL_DIR}/")


def cmd_send():
    sender = os.environ.get("GMAIL_ADDRESS")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("HOUSE_RECIPIENT")

    missing = [name for name, val in [
        ("GMAIL_ADDRESS", sender),
        ("GMAIL_APP_PASSWORD", app_password),
        ("HOUSE_RECIPIENT", recipient),
    ] if not val]
    if missing:
        print(f"Missing required environment variables: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    meta_path = os.path.join(EMAIL_DIR, "meta.json")
    if not os.path.exists(meta_path):
        print("No meta.json found - run 'generate' first.", file=sys.stderr)
        sys.exit(1)
    with open(meta_path) as f:
        meta = json.load(f)
    if not meta.get("send", False):
        print("Nothing to send this run (unchanged prices, or generate found no rows).")
        return

    with open(os.path.join(EMAIL_DIR, "subject.txt")) as f:
        subject = f.read()
    with open(os.path.join(EMAIL_DIR, "body.html")) as f:
        html_body = f.read()
    with open(os.path.join(EMAIL_DIR, "body.txt")) as f:
        text_body = f.read()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, app_password)
        server.send_message(msg)
    print(f"Sent to {recipient}!")


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("generate", "send"):
        print("Usage: python hanoi_house_price_emailer.py [generate|send]", file=sys.stderr)
        sys.exit(1)
    if sys.argv[1] == "generate":
        cmd_generate()
    else:
        cmd_send()


if __name__ == "__main__":
    main()
