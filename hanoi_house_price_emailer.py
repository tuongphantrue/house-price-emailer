#!/usr/bin/env python3
"""
Hanoi Apartment & House Listings -> Email
(runs on GitHub Actions, no local computer needed)

Sends the EXACT asking price of individual apartment/house listings
currently posted on Mogi.vn - not district averages. Runs in two phases
so the workflow can persist dedup state *between* them:

    python hanoi_house_price_emailer.py generate
        -> scrapes listing pages, writes the composed email
           (subject/html/text) under ./email/, and updates the
           "last sent" state file

    python hanoi_house_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

SOURCE & SCOPE
--------------
  https://mogi.vn/ha-noi/mua-can-ho-chung-cu   (apartments for sale, Hanoi)
  https://mogi.vn/ha-noi/mua-nha               (houses for sale, Hanoi)

Both are large, paginated lists (Hanoi apartments alone run ~7,300+
listings, 15/page) sorted newest-first. Pulling literally "all of them"
every run means hundreds of pages and tens of thousands of rows - not
realistic for a single email or for staying polite to the site. Instead
this fetches the newest MAX_PAGES_PER_CATEGORY pages of each category
(default 10 pages = up to ~150 listings per category, ~300 total) with a
delay between page requests. Raise MAX_PAGES_PER_CATEGORY if you want more.

HOW LISTINGS ARE PARSED
------------------------
Rather than depending on exact CSS classes/DOM structure (which broke
things once already in this project - see git history), each listing is
found by its URL, which always ends in "-idNNNNNNN" - that ID is stable
regardless of how the surrounding markup changes. The raw HTML is sliced
between consecutive listing IDs, and each slice is parsed for title,
district, area (m2), bedrooms/bathrooms, and price. If Mogi changes the
page structure enough that the ID pattern itself changes, or moves *where*
the price/area text sits in a way that breaks the regexes below, this
will simply report a lower match count (see the min-parsed-ratio warning
in cmd_generate) rather than emailing garbage.

SETUP
-----
1. Install dependencies:
     pip install requests beautifulsoup4 certifi

2. Create a Gmail "App Password":
     - https://myaccount.google.com/apppasswords (needs 2-Step Verification on)

3. Environment variables:
     export GMAIL_ADDRESS="youraddress@gmail.com"
     export GMAIL_APP_PASSWORD="16-char-app-password"
     export HOUSE_RECIPIENT="where-to-send@example.com"
     export SEND_ONLY_ON_CHANGE="false"        # optional, default false
     export TIMEZONE="Asia/Ho_Chi_Minh"        # optional, for the subject line
     export STATE_FILE="state/last_price.json" # optional, dedup state file
     export ALLOW_INSECURE_SSL_FALLBACK="false"
     export MAX_PAGES_PER_CATEGORY="10"        # optional, ~15 listings/page
     export PAGE_REQUEST_DELAY="1.0"           # optional, seconds between page fetches
     export APARTMENT_URL="https://mogi.vn/ha-noi/mua-can-ho-chung-cu"
     export HOUSE_URL="https://mogi.vn/ha-noi/mua-nha"

NOTE ON SCRAPING
-----------------
Worth checking https://mogi.vn/robots.txt before running this unattended
long-term. If `generate` reports a low parsed ratio, open one of the
listing URLs above and check whether the "-idNNNN" URL pattern and the
"X tỷ Y triệu" / "N m2" / "N PN" / "N WC" text tokens near each listing
still look the way LISTING_HREF_RE / parse_listing_chunk expect below.
"""

import hashlib
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta, date
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
}
EMAIL_DIR = "email"
STATE_FILE = os.environ.get("STATE_FILE", "state/last_price.json")
SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "false").lower() == "true"
ALLOW_INSECURE_SSL_FALLBACK = os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true"
MAX_PAGES_PER_CATEGORY = int(os.environ.get("MAX_PAGES_PER_CATEGORY", "10"))
PAGE_REQUEST_DELAY = float(os.environ.get("PAGE_REQUEST_DELAY", "1.0"))
# Real Hanoi apartments/houses for SALE are never priced this low in triệu
# đồng - a listing showing e.g. "12 triệu" (~$460) for a 95m2 apartment is
# almost always a seller data-entry error on Mogi itself (typed "triệu"
# meaning "tỷ", or a leftover placeholder), not a scraping bug - confirmed
# by spot-checking the live page directly. Filtered out rather than shown
# as fact. Lower this if you genuinely want to see sub-threshold listings
# (e.g. very small/rural huyện listings can legitimately be cheaper).
MIN_PLAUSIBLE_PRICE_TRIEU = float(os.environ.get("MIN_PLAUSIBLE_PRICE_TRIEU", "300"))
# Price ceiling in triệu đồng (1 tỷ = 1000 triệu) - hardcoded to 5 tỷ VND.
# Listings with no confirmed price (unparsed, or "Thỏa thuận"/negotiable)
# are excluded too, since they can't be confirmed to meet the ceiling.
# Set to 0 to disable and show listings at any price.
MAX_PRICE_TRIEU = 5000.0

CATEGORIES = [
    ("Căn hộ / Chung cư", os.environ.get("APARTMENT_URL", "https://mogi.vn/ha-noi/mua-can-ho-chung-cu")),
    ("Nhà", os.environ.get("HOUSE_URL", "https://mogi.vn/ha-noi/mua-nha")),
]

# Every listing's detail-page URL ends in -idNNNNNNN - that ID is used both
# to find where each listing "starts" in the raw HTML (slicing between
# consecutive matches) and to dedupe/hash listings run-to-run.
LISTING_HREF_RE = re.compile(r'href="([^"]*-id(\d+)/?)"')
AREA_RE = re.compile(r"([\d][\d.,]*)\s*m\s*2")
PN_RE = re.compile(r"(\d+)\s*PN")
WC_RE = re.compile(r"(\d+)\s*WC")
DISTRICT_RE = re.compile(r"((?:Quận|Huyện|Thị Xã)(?:\s+\S+){1,3}),\s*Hà Nội")
TY_TRIEU_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*tỷ(?:\s*(\d+(?:[.,]\d+)?)\s*triệu)?")
TRIEU_ONLY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*triệu")

# Posting-date text seen on listing cards: "Hôm nay", "Hôm qua", "N ngày
# trước", "N tuần trước", "N tháng trước", or an explicit "dd/mm/yyyy".
# Listings without a matched date are treated as unknown age, not dropped.
POSTED_RELATIVE_RE = re.compile(
    r"\b(Hôm nay|Hôm qua|(\d+)\s*(ngày|tuần|tháng|năm)\s*trước)\b"
)
POSTED_EXPLICIT_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

# Listings aren't shown if their post is older than this many days - an old
# post's asking price may no longer reflect the current market (or the unit
# may already be sold). Set to 0 to disable age filtering entirely.
MAX_LISTING_AGE_DAYS = int(os.environ.get("MAX_LISTING_AGE_DAYS", "90"))

# Max chunk size (chars of raw HTML) to look at per listing, in case two
# consecutive listing IDs are unexpectedly far apart in the markup.
MAX_CHUNK_CHARS = 4000


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


def fetch_page(url):
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


def parse_posted_date(text, today=None):
    """Returns (label, date) for a listing's posting date, or (None, None)
    if no recognizable date text was found. `today` should be a date
    object (defaults to the real current date) - passed explicitly so this
    stays testable/deterministic.
    """
    if today is None:
        today = datetime.now().date()

    m = POSTED_RELATIVE_RE.search(text)
    if m:
        label = m.group(1)
        if label == "Hôm nay":
            return "Hôm nay", today
        if label == "Hôm qua":
            return "Hôm qua", today - timedelta(days=1)
        n, unit = m.group(2), m.group(3)
        n = int(n)
        days = {"ngày": 1, "tuần": 7, "tháng": 30, "năm": 365}[unit]
        return f"{n} {unit} trước", today - timedelta(days=n * days)

    m2 = POSTED_EXPLICIT_DATE_RE.search(text)
    if m2:
        d, mo, y = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
        try:
            dt = date(y, mo, d)
            return dt.strftime("%d/%m/%Y"), dt
        except ValueError:
            return None, None

    return None, None


def parse_price_trieu(text):
    """Returns price in triệu đồng (float), the string 'Thỏa thuận' for
    negotiable listings, or None if no price could be found at all."""
    lower = text.lower()
    if "thỏa thuận" in lower or "thoả thuận" in lower:
        return "Thỏa thuận"
    m = TY_TRIEU_RE.search(text)
    if m:
        ty = float(m.group(1).replace(",", "."))
        trieu = float(m.group(2).replace(",", ".")) if m.group(2) else 0.0
        return ty * 1000 + trieu
    m2 = TRIEU_ONLY_RE.search(text)
    if m2:
        return float(m2.group(1).replace(",", "."))
    return None


def format_price(price_trieu):
    if price_trieu is None:
        return "—"
    if isinstance(price_trieu, str):
        return price_trieu
    if price_trieu >= 1000:
        ty = int(price_trieu // 1000)
        trieu = price_trieu - ty * 1000
        return f"{ty} tỷ {trieu:.0f} triệu" if trieu else f"{ty} tỷ"
    return f"{price_trieu:.0f} triệu"


def split_listing_chunks(html):
    """Slice the raw page HTML into one chunk per listing, using each
    listing's -idNNNN URL as the boundary (dedupes the image-link +
    title-link pair that both point at the same listing).
    """
    matches = list(LISTING_HREF_RE.finditer(html))
    seen = {}
    order = []
    for m in matches:
        href, listing_id = m.group(1), m.group(2)
        if listing_id not in seen:
            seen[listing_id] = m.start()
            order.append((listing_id, href, m.start()))

    chunks = []
    for i, (lid, href, start) in enumerate(order):
        next_start = order[i + 1][2] if i + 1 < len(order) else len(html)
        end = min(next_start, start + MAX_CHUNK_CHARS)
        chunks.append((lid, href, html[start:end]))
    return chunks


def parse_listing_chunk(lid, href, chunk_html, category):
    soup = BeautifulSoup(chunk_html, "html.parser")
    text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()

    title_tag = soup.find("a")
    title = title_tag.get_text(" ", strip=True) if title_tag else None
    # image alt text is often the real title when the <a> wraps only an <img>
    if (not title or len(title) < 3) and soup.find("img", alt=True):
        title = soup.find("img", alt=True)["alt"]

    area_m = AREA_RE.search(text)
    pn_m = PN_RE.search(text)
    wc_m = WC_RE.search(text)
    district_m = DISTRICT_RE.search(text)
    price = parse_price_trieu(text)
    posted_label, posted_date = parse_posted_date(text)

    url = href if href.startswith("http") else f"https://mogi.vn{href}"

    return {
        "id": lid,
        "category": category,
        "title": title or "(không có tiêu đề)",
        "url": url,
        "district": district_m.group(1).strip() if district_m else None,
        "area": area_m.group(1) if area_m else None,
        "bedrooms": pn_m.group(1) if pn_m else None,
        "bathrooms": wc_m.group(1) if wc_m else None,
        "price_trieu": price,
        "posted_label": posted_label,
        "posted_date": posted_date.isoformat() if posted_date else None,
    }


def fetch_category_listings(category, base_url):
    listings = []
    seen_ids = set()
    for page in range(1, MAX_PAGES_PER_CATEGORY + 1):
        url = base_url if page == 1 else f"{base_url}?cp={page}"
        if page > 1 and PAGE_REQUEST_DELAY > 0:
            time.sleep(PAGE_REQUEST_DELAY)
        try:
            html = fetch_page(url)
        except requests.RequestException as e:
            print(f"  [{category}] failed to fetch page {page} ({url}): {e}", file=sys.stderr)
            break

        chunks = split_listing_chunks(html)
        if not chunks:
            print(f"  [{category}] page {page}: 0 listing IDs found - stopping "
                  f"(either last page or markup changed)", file=sys.stderr)
            break

        new_this_page = 0
        for lid, href, chunk_html in chunks:
            if lid in seen_ids:
                continue
            seen_ids.add(lid)
            listings.append(parse_listing_chunk(lid, href, chunk_html, category))
            new_this_page += 1

        print(f"  [{category}] page {page}: {new_this_page} new listing(s) parsed "
              f"(total so far: {len(listings)})")

        if new_this_page == 0:
            break

    return listings


def _price_sort_key(l):
    p = l["price_trieu"]
    if isinstance(p, (int, float)):
        return (0, p)
    # unpriced ("Thỏa thuận" or unparsed) sorts after all priced listings
    return (1, 0)


def sort_listings_by_price(listings):
    # sort within each category (cheapest first), keeping categories grouped
    # in the same order as CATEGORIES
    by_category = {cat: [] for cat, _ in CATEGORIES}
    for l in listings:
        by_category.setdefault(l["category"], []).append(l)
    sorted_listings = []
    for cat, _ in CATEGORIES:
        sorted_listings.extend(sorted(by_category.get(cat, []), key=_price_sort_key))
    return sorted_listings


def group_by_district(listings):
    """Groups an already price-sorted list of listings by district,
    preserving price order within each district. Unknown districts are
    grouped together under None and always sorted last.
    """
    groups = {}
    for l in listings:
        groups.setdefault(l["district"], []).append(l)

    def sort_key(district):
        return (district is None, district or "")

    return [(d, groups[d]) for d in sorted(groups, key=sort_key)]


def fetch_all_listings():
    all_listings = []
    for category, base_url in CATEGORIES:
        print(f"Fetching {category} ({base_url}) - up to {MAX_PAGES_PER_CATEGORY} page(s) ...")
        all_listings.extend(fetch_category_listings(category, base_url))
    return all_listings


def build_listing_row_html(l):
    price_str = escape(format_price(l["price_trieu"]))
    details = []
    if l["area"]:
        details.append(f"{escape(l['area'])} m²")
    if l["bedrooms"]:
        details.append(f"{escape(l['bedrooms'])} PN")
    if l["bathrooms"]:
        details.append(f"{escape(l['bathrooms'])} WC")
    detail_str = " · ".join(details) if details else "—"
    posted_str = escape(l["posted_label"]) if l["posted_label"] else "?"

    return f"""
<tr>
<td style="padding:8px 12px;border-bottom:1px solid #eee;">
  <a href="{escape(l['url'])}" style="color:#1a5fb4;text-decoration:none;font-weight:bold;">{escape(l['title'])}</a><br>
  <span style="color:#666;font-size:12px;">{detail_str} · đăng {posted_str}</span>
</td>
<td style="padding:8px 12px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap;font-weight:bold;">{price_str}</td>
</tr>"""


def build_district_section_html(district, district_listings):
    district_label = escape(district) if district else "Không rõ quận/huyện"
    rows = "\n".join(build_listing_row_html(l) for l in district_listings)
    return f"""
<h3 style="color:#333;font-size:15px;margin-top:18px;margin-bottom:4px;">{district_label} ({len(district_listings)} tin)</h3>
<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:640px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
<tbody>
{rows}
</tbody>
</table>"""


def build_html(listings, timestamp):
    if not listings:
        body = "<p>Không lấy được tin đăng nào kỳ này. Kiểm tra trực tiếp nguồn hoặc xem log để biết chi tiết.</p>"
    else:
        sections = []
        for category, _ in CATEGORIES:
            cat_listings = [l for l in listings if l["category"] == category]
            if not cat_listings:
                continue
            district_sections = "\n".join(
                build_district_section_html(district, district_listings)
                for district, district_listings in group_by_district(cat_listings)
            )
            sections.append(f"""
<h2 style="color:#1a5fb4;font-size:18px;margin-top:28px;">{escape(category)} ({len(cat_listings)} tin)</h2>
{district_sections}""")
        body = "\n".join(sections)

    return f"""\
<html>
<body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
<h1 style="color:#1a5fb4;">Tin đăng bán nhà & căn hộ Hà Nội</h1>
<p style="color:#555;">Cập nhật {escape(timestamp)} · {len(listings)} tin đăng</p>
{body}
<p style="color:#999; font-size:12px; margin-top:24px;">
Nguồn: từng tin đăng thực tế trên Mogi.vn (không phải giá trung bình) ·
Email tự động, chỉ mang tính tham khảo, không phải lời khuyên đầu tư.
</p>
</body>
</html>"""


def build_plain_text(listings, timestamp):
    lines = [f"Tin dang ban nha & can ho Ha Noi - cap nhat {timestamp}", f"{len(listings)} tin dang", ""]
    for category, _ in CATEGORIES:
        cat_listings = [l for l in listings if l["category"] == category]
        if not cat_listings:
            continue
        lines.append(f"=== {category} ({len(cat_listings)} tin) ===")
        for district, district_listings in group_by_district(cat_listings):
            district_label = district or "Khong ro quan/huyen"
            lines.append(f"  --- {district_label} ({len(district_listings)} tin) ---")
            for l in district_listings:
                details = []
                if l["area"]:
                    details.append(f"{l['area']} m2")
                if l["bedrooms"]:
                    details.append(f"{l['bedrooms']} PN")
                if l["bathrooms"]:
                    details.append(f"{l['bathrooms']} WC")
                detail_str = ", ".join(details) if details else "—"
                posted_str = l["posted_label"] or "?"
                lines.append(f"    {l['title']}")
                lines.append(f"      {detail_str} | {format_price(l['price_trieu'])} | dang {posted_str}")
                lines.append(f"      {l['url']}")
        lines.append("")
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

    listings = fetch_all_listings()

    implausible = [
        l for l in listings
        if isinstance(l["price_trieu"], (int, float)) and l["price_trieu"] < MIN_PLAUSIBLE_PRICE_TRIEU
    ]
    if implausible:
        print(f"  Dropping {len(implausible)} listing(s) with implausible price "
              f"(< {MIN_PLAUSIBLE_PRICE_TRIEU} triệu - likely seller data-entry error, "
              f"not a real sale price):", file=sys.stderr)
        for l in implausible[:10]:
            print(f"    [{l['category']}] {format_price(l['price_trieu'])} - {l['title'][:60]}", file=sys.stderr)
        if len(implausible) > 10:
            print(f"    ... and {len(implausible) - 10} more", file=sys.stderr)
    listings = [l for l in listings if l not in implausible]

    if MAX_LISTING_AGE_DAYS > 0:
        today = datetime.now().date()
        cutoff = today - timedelta(days=MAX_LISTING_AGE_DAYS)
        too_old = [
            l for l in listings
            if l["posted_date"] and date.fromisoformat(l["posted_date"]) < cutoff
        ]
        if too_old:
            print(f"  Dropping {len(too_old)} listing(s) posted more than "
                  f"{MAX_LISTING_AGE_DAYS} day(s) ago (price may be stale):", file=sys.stderr)
            for l in too_old[:10]:
                print(f"    [{l['category']}] posted {l['posted_label']} - {l['title'][:60]}", file=sys.stderr)
            if len(too_old) > 10:
                print(f"    ... and {len(too_old) - 10} more", file=sys.stderr)
        listings = [l for l in listings if l not in too_old]

    if MAX_PRICE_TRIEU > 0:
        over_budget = [
            l for l in listings
            if not (isinstance(l["price_trieu"], (int, float)) and l["price_trieu"] <= MAX_PRICE_TRIEU)
        ]
        if over_budget:
            print(f"  Dropping {len(over_budget)} listing(s) over budget or with unconfirmed price "
                  f"(ceiling: {format_price(MAX_PRICE_TRIEU)}):", file=sys.stderr)
            for l in over_budget[:10]:
                print(f"    [{l['category']}] {format_price(l['price_trieu'])} - {l['title'][:60]}", file=sys.stderr)
            if len(over_budget) > 10:
                print(f"    ... and {len(over_budget) - 10} more", file=sys.stderr)
        listings = [l for l in listings if l not in over_budget]

    listings = sort_listings_by_price(listings)
    print(f"Total parsed: {len(listings)} listing(s).")

    priced = [l for l in listings if l["price_trieu"] is not None]
    if listings and len(priced) / len(listings) < 0.5:
        print(
            f"  WARNING: only {len(priced)}/{len(listings)} listings had a parseable price - "
            "Mogi may have changed its price text format. Check parse_price_trieu().",
            file=sys.stderr,
        )

    hash_input = [{"id": l["id"], "price_trieu": l["price_trieu"]} for l in listings]
    price_hash = hash_data(hash_input)
    last_hash = load_last_hash()

    if listings and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Listings unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Tin dang nha & can ho Ha Noi - {now.strftime('%d/%m/%Y %H:%M')} ({len(listings)} tin)"
    html_body = build_html(listings, timestamp)
    text_body = build_plain_text(listings, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"send": True, "listing_count": len(listings)}, f)

    save_last_hash(price_hash)
    print(f"Generated email ({len(listings)} listings). Saved to ./{EMAIL_DIR}/")


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
        print("Nothing to send this run (unchanged listings, or generate found none).")
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
