#!/usr/bin/env python3
"""
Hanoi House/Land/Apartment Prices (by district) -> Email
(runs on GitHub Actions, no local computer needed)

Same shape as the gold-price-emailer / 9gag-meme-emailer this is modeled on:
fetches price data, then emails an HTML digest via Gmail SMTP. Runs in two
phases so the workflow can persist dedup state *between* them (see the
accompanying GitHub Actions workflow):

    python hanoi_house_price_emailer.py generate
        -> scrapes the price data, writes the composed email
           (subject/html/text) under ./email/, and updates the
           "last sent price" state file

    python hanoi_house_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

SOURCES & AN IMPORTANT CAVEAT
------------------------------
Vietnamese gold prices have a clean daily aggregator (giavang.org) with one
simple table per seller. Housing prices don't have a real equivalent, and
Mogi.vn's main "Giá nhà đất" table (https://mogi.vn/gia-nha-dat) only gives
a BLENDED nha + dat (house + land) average per district - it does not split
out apartments.

HOWEVER: each district's own Mogi page (e.g.
https://mogi.vn/gia-nha-dat-quan-cau-giay-qd290) contains server-rendered
prose that DOES break the average down by property type, including a
sentence like:

    "Gia can ho tang 5,5%, len gan 35 trieu dong/m2."

That sentence is what this script now parses per district (see
CANHO_PRICE_RE / fetch_apartment_prices below) to build an apartment-only
table. It is still a best-effort scrape of prose text, not a structured
API, so:
  - it is heuristic and can miss a district if Mogi rephrases the sentence
  - the "city-wide average" this script emails is a simple mean of whatever
    per-district figures were successfully parsed, not an official
    aggregate figure
  - the original blended nha-dat table is kept in the email too (clearly
    labeled) since it's the most robust of the three numbers

If you find a cleaner, structured, officially-published apartment-only
source, that's a better fit than this regex-on-prose approach - swap it in
where noted below.

Because prices move slowly, running this every 30 minutes will very often
just re-send the same numbers. Consider SEND_ONLY_ON_CHANGE=true (see
below) if you'd rather only get an email when the numbers actually change.

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
     export SEND_ONLY_ON_CHANGE="false"       # optional, default false
     export TIMEZONE="Asia/Ho_Chi_Minh"       # optional, for the subject line
     export SOURCE_URL="https://mogi.vn/gia-nha-dat"  # optional, blended table
     export STATE_FILE="state/last_price.json"        # optional, dedup state file
     export ALLOW_INSECURE_SSL_FALLBACK="false"        # optional, last-resort TLS bypass
     export APARTMENT_REQUEST_DELAY="1.0"     # optional, seconds between the
                                               # ~27 per-district page fetches
     export SKIP_APARTMENT_SECTION="false"    # optional, set true to fall back
                                               # to the old blended-only email

NOTE ON SCRAPING
-----------------
Always worth checking the current robots.txt / terms of whatever site this
is pointed at before running it unattended long-term, e.g.:
    https://mogi.vn/robots.txt

The page markup (and the apartment sentence wording) can change at any
time. If `generate` reports 0 blended rows, or 0/few apartment rows, open
SOURCE_URL (and a couple of the per-district URLs in HANOI_APARTMENT_AREAS)
and update parse_hanoi_table() / CANHO_PRICE_RE below to match.
"""

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import time
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

SOURCE_URL = os.environ.get("SOURCE_URL", "https://mogi.vn/gia-nha-dat")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}
EMAIL_DIR = "email"
STATE_FILE = os.environ.get("STATE_FILE", "state/last_price.json")
SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "false").lower() == "true"
ALLOW_INSECURE_SSL_FALLBACK = os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true"
APARTMENT_REQUEST_DELAY = float(os.environ.get("APARTMENT_REQUEST_DELAY", "1.0"))
SKIP_APARTMENT_SECTION = os.environ.get("SKIP_APARTMENT_SECTION", "false").lower() == "true"

# Every district/huyen of Hanoi, as labeled on mogi.vn (prefix + name).
# Matched against link text, so this list is what determines which rows on
# the blended gia-nha-dat page belong to Hanoi (the same page also lists
# TPHCM districts).
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

# Each Hanoi district's OWN mogi.vn page, which (unlike the blended table)
# has a per-property-type breakdown in prose, including apartments. URLs
# copied directly from the district links on https://mogi.vn/gia-nha-dat
# as of Aug 2026 - Hanoi's district list changed after the 2025 admin
# merger, so this may need occasional updates independent of HANOI_AREAS
# above (which still reflects the older/blended table's area list).
HANOI_APARTMENT_AREAS = [
    ("Quận Ba Đình", "https://mogi.vn/gia-nha-dat-quan-ba-dinh-qd289"),
    ("Quận Cầu Giấy", "https://mogi.vn/gia-nha-dat-quan-cau-giay-qd290"),
    ("Quận Đống Đa", "https://mogi.vn/gia-nha-dat-quan-dong-da-qd291"),
    ("Quận Hai Bà Trưng", "https://mogi.vn/gia-nha-dat-quan-hai-ba-trung-qd292"),
    ("Quận Hoàn Kiếm", "https://mogi.vn/gia-nha-dat-quan-hoan-kiem-qd293"),
    ("Quận Hoàng Mai", "https://mogi.vn/gia-nha-dat-quan-hoang-mai-qd294"),
    ("Quận Long Biên", "https://mogi.vn/gia-nha-dat-quan-long-bien-qd295"),
    ("Quận Tây Hồ", "https://mogi.vn/gia-nha-dat-quan-tay-ho-qd296"),
    ("Quận Thanh Xuân", "https://mogi.vn/gia-nha-dat-quan-thanh-xuan-qd297"),
    ("Quận Hà Đông", "https://mogi.vn/gia-nha-dat-quan-ha-dong-qd745"),
    ("Quận Bắc Từ Liêm", "https://mogi.vn/gia-nha-dat-quan-bac-tu-liem-qd754"),
    ("Quận Nam Từ Liêm", "https://mogi.vn/gia-nha-dat-quan-nam-tu-liem-qd755"),
    ("Huyện Mê Linh", "https://mogi.vn/gia-nha-dat-huyen-me-linh-qd729"),
    ("Huyện Ba Vì", "https://mogi.vn/gia-nha-dat-huyen-ba-vi-qd298"),
    ("Huyện Chương Mỹ", "https://mogi.vn/gia-nha-dat-huyen-chuong-my-qd299"),
    ("Huyện Đan Phượng", "https://mogi.vn/gia-nha-dat-huyen-dan-phuong-qd300"),
    ("Huyện Hoài Đức", "https://mogi.vn/gia-nha-dat-huyen-hoai-duc-qd301"),
    ("Huyện Phúc Thọ", "https://mogi.vn/gia-nha-dat-huyen-phuc-tho-qd304"),
    ("Huyện Quốc Oai", "https://mogi.vn/gia-nha-dat-huyen-quoc-oai-qd305"),
    ("Huyện Thạch Thất", "https://mogi.vn/gia-nha-dat-huyen-thach-that-qd306"),
    ("Huyện Thanh Oai", "https://mogi.vn/gia-nha-dat-huyen-thanh-oai-qd307"),
    ("Huyện Thường Tín", "https://mogi.vn/gia-nha-dat-huyen-thuong-tin-qd308"),
    ("Thị Xã Sơn Tây", "https://mogi.vn/gia-nha-dat-thi-xa-son-tay-qd311"),
    ("Huyện Đông Anh", "https://mogi.vn/gia-nha-dat-huyen-dong-anh-qd284"),
    ("Huyện Gia Lâm", "https://mogi.vn/gia-nha-dat-huyen-gia-lam-qd285"),
    ("Huyện Sóc Sơn", "https://mogi.vn/gia-nha-dat-huyen-soc-son-qd286"),
    ("Huyện Thanh Trì", "https://mogi.vn/gia-nha-dat-huyen-thanh-tri-ha-noi-qd287"),
]

# Matches sentences like:
#   "Giá căn hộ tăng 5,5%, lên gần 35 triệu đồng/m2."
#   "Giá căn hộ giảm 9,7%, xuống còn trên 130 triệu đồng/m2."
#   "Giá căn hộ không đổi, giữ mức 50 triệu đồng/m2."
# Group 1: direction word ("tăng" / "giảm" / "không đổi")
# Group 2: percent change (may be empty, e.g. for "không đổi")
# Group 3: price in triệu đồng/m2
CANHO_PRICE_RE = re.compile(
    r"Giá\s+căn\s+hộ\s+"
    r"(tăng|giảm|không đổi)"
    r"(?:\s*(?:nhẹ|mạnh))?"
    r"\s*([\d.,]+)?%?,?"
    r"\s*(?:lên|xuống|giữ mức)?\s*(?:còn)?\s*(?:trên|gần|dưới)?\s*"
    r"([\d.,]+)\s*triệu(?:\s*đồng)?\s*/\s*m2",
    re.IGNORECASE,
)


def load_last_hash(path=STATE_FILE):
    """Return the previous run's price-data hash, or None if there isn't
    one (missing/corrupt state is treated as "first run", not fatal).
    """
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


def fetch_page(url):
    """GET a page, verifying TLS against certifi's CA bundle explicitly
    (see gold-price-emailer's fetch_page for why). ALLOW_INSECURE_SSL_FALLBACK
    is an explicit opt-in last resort if that still fails.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=certifi.where())
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.SSLError as e:
        print(f"  TLS verification failed with certifi's CA bundle: {e}", file=sys.stderr)
        if not ALLOW_INSECURE_SSL_FALLBACK:
            print(
                "  Set ALLOW_INSECURE_SSL_FALLBACK=true to retry without verification "
                "as a last resort.",
                file=sys.stderr,
            )
            raise
        print("  ALLOW_INSECURE_SSL_FALLBACK=true - retrying with TLS verification disabled.", file=sys.stderr)
        resp = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        resp.raise_for_status()
        return resp.text


def _norm(s):
    # Collapse NBSP/whitespace and normalize to NFC so diacritics compare
    # equal regardless of which composed/decomposed form the page sends.
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return unicodedata.normalize("NFC", s)


def parse_hanoi_table(html):
    """
    Parse the Hanoi section of mogi.vn/gia-nha-dat into a list of
    {area, price, change} rows. This is the original BLENDED (house + land)
    table - see module docstring for why apartments aren't split out here.

    The page isn't cleanly separated in the DOM in an obvious way we can
    rely on long-term, so rather than depend on exact table/row structure,
    this matches by *district name* (HANOI_AREAS) against the page's
    flattened text, then looks at the text immediately following each
    match for a "NN triệu/m2" price and an optional "N,N%" change figure.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [_norm(l) for l in text.split("\n") if _norm(l)]

    areas_norm = {_norm(a): a for a in HANOI_AREAS}
    rows = []
    seen = set()

    for i, line in enumerate(lines):
        area = areas_norm.get(line)
        if not area or area in seen:
            continue
        if i + 1 >= len(lines):
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


def fetch_hanoi_prices():
    html = fetch_page(SOURCE_URL)
    return parse_hanoi_table(html)


def parse_apartment_price(html):
    """Parse a single district page's prose for the 'Giá căn hộ ...'
    sentence. Returns {price, change, direction} or None if the sentence
    wasn't found (page rephrased, or this district has no apartment
    stock to report on).
    """
    soup = BeautifulSoup(html, "html.parser")
    text = _norm(soup.get_text(" "))
    m = CANHO_PRICE_RE.search(text)
    if not m:
        return None
    direction_word, change, price = m.groups()
    direction = None
    if direction_word == "tăng":
        direction = "up"
    elif direction_word == "giảm":
        direction = "down"
    return {"price": price, "change": change, "direction": direction}


def _to_float(s):
    if not s:
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


def fetch_apartment_prices():
    """Fetch each Hanoi district's own page and parse its apartment-price
    sentence. Politely delayed (APARTMENT_REQUEST_DELAY) between requests
    since this is ~27 requests instead of the blended table's 1.
    Failures on individual districts are logged and skipped, not fatal.
    """
    rows = []
    for i, (area, url) in enumerate(HANOI_APARTMENT_AREAS):
        if i > 0 and APARTMENT_REQUEST_DELAY > 0:
            time.sleep(APARTMENT_REQUEST_DELAY)
        try:
            html = fetch_page(url)
        except requests.RequestException as e:
            print(f"  [apartment] failed to fetch {area} ({url}): {e}", file=sys.stderr)
            continue
        parsed = parse_apartment_price(html)
        if not parsed:
            print(f"  [apartment] could not find apartment sentence for {area} - skipping", file=sys.stderr)
            continue
        rows.append({"area": area, **parsed})
    return rows


def _change_html(change, direction):
    if not change:
        return "<span style='color:#999'>—</span>"
    color = "#1a7f37" if direction == "up" else ("#cf222e" if direction == "down" else "#666")
    arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "")
    return f"<span style='color:{color}'>{escape(change)}% {arrow}</span>"


def build_price_table_html(rows, price_label="triệu/m²"):
    row_html = "\n".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['price'])} {price_label}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{_change_html(r['change'], r['direction'])}</td>"
        f"</tr>"
        for r in rows
    )
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
<thead>
<tr style="background:#f5f5f5;">
<th style="padding:8px 12px;text-align:left;">Quận / Huyện</th>
<th style="padding:8px 12px;text-align:right;">Giá trung bình</th>
<th style="padding:8px 12px;text-align:right;">So với kỳ trước</th>
</tr>
</thead>
<tbody>
{row_html}
</tbody>
</table>"""


def build_html(blended_rows, apartment_rows, apartment_avg, source_url, timestamp):
    sections = []

    if apartment_rows and not SKIP_APARTMENT_SECTION:
        sections.append(f"""
<h2 style="color:#1a5fb4;font-size:18px;margin-top:28px;">Giá căn hộ theo quận/huyện</h2>
<p style="color:#999;font-size:12px;margin-top:-8px;">
Lấy được dữ liệu {len(apartment_rows)}/{len(HANOI_APARTMENT_AREAS)} quận/huyện kỳ này.
</p>
{build_price_table_html(apartment_rows)}
""")
    elif not SKIP_APARTMENT_SECTION:
        sections.append("""
<h2 style="color:#1a5fb4;font-size:18px;margin-top:28px;">Giá căn hộ theo quận/huyện</h2>
<p>Không lấy được câu "Giá căn hộ ..." từ bất kỳ trang quận/huyện nào kỳ này -
có thể Mogi.vn đã đổi cách diễn đạt trên trang. Xem CANHO_PRICE_RE trong script.</p>
""")

    if blended_rows:
        sections.append(f"""
<h2 style="color:#1a5fb4;font-size:18px;margin-top:28px;">Giá nhà đất chung theo quận/huyện (nhà + đất, gộp)</h2>
<p style="color:#999;font-size:12px;margin-top:-4px;">
Đây KHÔNG phải giá căn hộ riêng - là giá bình quân gộp chung nhà và đất, giữ lại để tham khảo/so sánh.
</p>
{build_price_table_html(blended_rows)}
""")
    else:
        sections.append("""
<h2 style="color:#1a5fb4;font-size:18px;margin-top:28px;">Giá nhà đất chung (nhà + đất, gộp)</h2>
<p>Không lấy được bảng giá nhà đất chung kỳ này. Kiểm tra trực tiếp nguồn.</p>
""")

    body = "\n".join(sections)

    return f"""\
<html>
<body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
<h1 style="color:#1a5fb4;">Giá nhà đất & căn hộ Hà Nội theo quận/huyện</h1>
<p style="color:#555;">Cập nhật {escape(timestamp)}</p>
{body}
<p style="color:#999; font-size:12px; margin-top:24px;">
Nguồn: <a href="{escape(source_url)}">{escape(source_url)}</a> và từng trang quận/huyện tương ứng trên Mogi.vn ·
Đơn vị: triệu đồng/m² · Email tự động, chỉ mang tính tham khảo, không phải lời khuyên đầu tư.
</p>
</body>
</html>"""


def build_plain_text(blended_rows, apartment_rows, apartment_avg, source_url, timestamp):
    lines = [f"Gia nha dat & can ho Ha Noi theo quan/huyen - cap nhat {timestamp}", ""]

    if apartment_rows and not SKIP_APARTMENT_SECTION:
        lines.append(f"Gia can ho theo quan/huyen ({len(apartment_rows)}/{len(HANOI_APARTMENT_AREAS)} quan/huyen co du lieu ky nay):")
        for r in apartment_rows:
            change_str = f"{r['change']}%" if r["change"] else "—"
            lines.append(f"  {r['area']}: {r['price']} trieu/m2 ({change_str})")
        lines.append("")
    elif not SKIP_APARTMENT_SECTION:
        lines.append("Khong lay duoc gia can ho ky nay.")
        lines.append("")

    lines.append("Gia nha dat chung (nha + dat, gop) - KHONG phai gia can ho rieng:")
    if not blended_rows:
        lines.append("  Khong lay duoc bang gia nha dat chung ky nay.")
    else:
        for r in blended_rows:
            change_str = f"{r['change']}%" if r["change"] else "—"
            lines.append(f"  {r['area']}: {r['price']} trieu/m2 ({change_str})")

    lines.append("")
    lines.append(f"Nguon: {source_url}")
    return "\n".join(lines)


def resolve_timestamp():
    timezone_name = os.environ.get("TIMEZONE", "Asia/Ho_Chi_Minh")
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone_name))
    except Exception:
        now = datetime.now()
    return now, now.strftime("%H:%M %d/%m/%Y")


def cmd_generate():
    if os.path.exists(EMAIL_DIR):
        for f in os.listdir(EMAIL_DIR):
            os.remove(os.path.join(EMAIL_DIR, f))
    os.makedirs(EMAIL_DIR, exist_ok=True)

    print(f"Fetching blended table {SOURCE_URL} ...")
    try:
        blended_rows = fetch_hanoi_prices()
    except requests.RequestException as e:
        print(f"Failed to fetch blended price page: {e}", file=sys.stderr)
        blended_rows = []
    print(f"Parsed {len(blended_rows)} blended Hanoi district row(s).")

    apartment_rows = []
    if not SKIP_APARTMENT_SECTION:
        print(f"Fetching apartment prices for {len(HANOI_APARTMENT_AREAS)} district page(s) "
              f"(delay={APARTMENT_REQUEST_DELAY}s) ...")
        apartment_rows = fetch_apartment_prices()
        print(f"Parsed {len(apartment_rows)}/{len(HANOI_APARTMENT_AREAS)} apartment row(s).")

    if not blended_rows and not apartment_rows:
        print(
            "  0 rows parsed from either source - markup may have changed. "
            "Check SOURCE_URL and a sample district URL in HANOI_APARTMENT_AREAS.",
            file=sys.stderr,
        )

    apartment_prices_f = [_to_float(r["price"]) for r in apartment_rows]
    apartment_prices_f = [p for p in apartment_prices_f if p is not None]
    apartment_avg = sum(apartment_prices_f) / len(apartment_prices_f) if apartment_prices_f else None

    combined_for_hash = {"blended": blended_rows, "apartment": apartment_rows}
    price_hash = hash_data(combined_for_hash)
    last_hash = load_last_hash()

    if (blended_rows or apartment_rows) and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Gia nha dat & can ho Ha Noi - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(blended_rows, apartment_rows, apartment_avg, SOURCE_URL, timestamp)
    text_body = build_plain_text(blended_rows, apartment_rows, apartment_avg, SOURCE_URL, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"send": True, "blended_rows": len(blended_rows), "apartment_rows": len(apartment_rows)}, f)

    save_last_hash(price_hash)
    print(f"Generated email (blended={len(blended_rows)}, apartment={len(apartment_rows)}). Saved to ./{EMAIL_DIR}/")


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
