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
this fetches a single page per category (whatever Mogi returns on the
first page of results, ~15 listings/category) - no pagination, by design.

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
import unicodedata
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

# Category URL SUFFIXES - combined with each district slug below to build
# per-district listing URLs, e.g. https://mogi.vn/ha-noi/quan-cau-giay/mua-can-ho-chung-cu
# Fetching per-district pages (confirmed working URLs - see comment below)
# instead of paginating one citywide URL, since the citywide page's ?cp=N
# pagination turned out to not reliably return new results (see git
# history). Each district page returns its own first ~15 listings with no
# pagination needed, so combining a handful of districts easily clears 50.
CATEGORY_URL_SUFFIX = {
    "Căn hộ / Chung cư": "mua-can-ho-chung-cu",
    "Nhà": "mua-nha",
}

# Hanoi district slugs, confirmed against real mogi.vn URLs for at least
# quan-cau-giay, quan-hai-ba-trung, quan-ha-dong, and quan-dong-da (spot
# checked directly) - the rest follow the same "quan-x"/"huyen-x" naming
# convention used consistently across mogi.vn's district pages elsewhere
# on the site, but weren't each individually re-verified.
HANOI_DISTRICT_SLUGS = [
    ("Quận Ba Đình", "quan-ba-dinh"),
    ("Quận Cầu Giấy", "quan-cau-giay"),
    ("Quận Đống Đa", "quan-dong-da"),
    ("Quận Hai Bà Trưng", "quan-hai-ba-trung"),
    ("Quận Hoàn Kiếm", "quan-hoan-kiem"),
    ("Quận Hoàng Mai", "quan-hoang-mai"),
    ("Quận Long Biên", "quan-long-bien"),
    ("Quận Tây Hồ", "quan-tay-ho"),
    ("Quận Thanh Xuân", "quan-thanh-xuan"),
    ("Quận Hà Đông", "quan-ha-dong"),
    ("Quận Bắc Từ Liêm", "quan-bac-tu-liem"),
    ("Quận Nam Từ Liêm", "quan-nam-tu-liem"),
    ("Huyện Mê Linh", "huyen-me-linh"),
    ("Huyện Đông Anh", "huyen-dong-anh"),
    ("Huyện Gia Lâm", "huyen-gia-lam"),
    ("Huyện Sóc Sơn", "huyen-soc-son"),
    ("Huyện Thanh Trì", "huyen-thanh-tri"),
    ("Huyện Hoài Đức", "huyen-hoai-duc"),
    ("Huyện Chương Mỹ", "huyen-chuong-my"),
    ("Huyện Đan Phượng", "huyen-dan-phuong"),
    ("Huyện Thanh Oai", "huyen-thanh-oai"),
    ("Huyện Thường Tín", "huyen-thuong-tin"),
    ("Huyện Ba Vì", "huyen-ba-vi"),
    ("Huyện Phúc Thọ", "huyen-phuc-tho"),
    ("Huyện Quốc Oai", "huyen-quoc-oai"),
    ("Huyện Thạch Thất", "huyen-thach-that"),
    ("Thị Xã Sơn Tây", "thi-xa-son-tay"),
]

CATEGORIES = [(cat, None) for cat in CATEGORY_URL_SUFFIX]

# Every listing's detail-page URL ends in -idNNNNNNN - that ID is used both
# to find where each listing "starts" in the raw HTML (slicing between
# consecutive matches) and to dedupe/hash listings run-to-run.
LISTING_HREF_RE = re.compile(r'href="([^"]*-id(\d+)/?)"')
AREA_RE = re.compile(r"([\d][\d.,]*)\s*m\s*2")
PN_RE = re.compile(r"(\d+)\s*PN")
WC_RE = re.compile(r"(\d+)\s*WC")
DISTRICT_RE = re.compile(r"((?:Quận|Huyện|Thị Xã)(?:\s+\S+){1,3}),\s*Hà Nội")
OTHER_CITY_RE = re.compile(
    r"TPHCM|TP\.?\s*HCM|Tp\.?\s*Hồ Chí Minh|Thành phố Hồ Chí Minh|"
    r"Đà Nẵng|Hải Phòng|Cần Thơ|Bình Dương|Đồng Nai|Bà Rịa|Nha Trang|Khánh Hòa",
    re.IGNORECASE,
)
# Hard filter: known-certain phrasings for "mini apartment" listings (a
# distinct, informally-regulated housing type in Vietnam). Word order and
# spacing vary a lot in real listings, so this covers several orderings/
# abbreviations rather than one fixed phrase. \s* also matches zero spaces,
# so "chungcumini" (no spaces) still matches.
MINI_APARTMENT_RE = re.compile(
    r"chung\s*c[uư]\s*mini|"
    r"mini\s*chung\s*c[uư]|"
    r"c[aă]n\s*h[ôo]\s*mini|"
    r"mini\s*c[aă]n\s*h[ôo]|"
    r"\bCCMN\b|"
    r"\bCC\s*mini\b|"
    r"\bCH\s*mini\b",
    re.IGNORECASE,
)
# Soft heuristic: wording commonly seen in mini-apartment ads (individual
# "sổ hồng riêng từng phòng" - separate title deed per room/unit - and
# round-the-clock security marketed per-room, both signal a subdivided
# building rather than a normal apartment) combined with an unusually
# small area. Doesn't exclude the listing - just adds a review flag,
# since small legitimate studios do exist and keyword matching alone
# proved unreliable (see conversation - "CC Mini" was missed once already).
MINI_APARTMENT_HINT_RE = re.compile(
    r"sổ hồng riêng từng phòng|an ninh 24/24|khép kín riêng biệt", re.IGNORECASE
)
MINI_APARTMENT_HINT_MAX_AREA = 30  # m2
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
MAX_LISTING_AGE_DAYS = int(os.environ.get("MAX_LISTING_AGE_DAYS", "365"))

# Max chunk size (chars of raw HTML) to look at per listing, used as a
# fallback when a listing has no "next listing ID" to bound it against
# (typically the last listing found on a page) - in that case there's
# nothing to stop the chunk at, so it's capped here instead. Too large and
# unrelated page content (sidebar/footer/related-listings widgets) can leak
# into the chunk and get mistaken for this listing's price/area - this bit
# us once already (a real "6,7 tỷ" listing showed an unrelated "4 tỷ 200
# triệu" instead). Too small and a genuinely verbose card gets truncated
# before reaching its own price. 1500 is a guess without visibility into
# the real markup size - watch for listings missing area/price/date after
# changing this and adjust up if fields start going missing, or down if
# implausible cross-contaminated values (like the one above) keep appearing.
MAX_CHUNK_CHARS = 1500


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


# Sanity-check thresholds for flag_suspicious() below - a last line of
# defense that runs on the final built listing, independent of whatever
# specific bug pattern the earlier parsing filters were written against.
# This exists because every bug found in this script so far (a title/price
# belonging to the wrong listing, an unrelated city's listing slipping
# through, a chung-cư-mini variant not yet in MINI_APARTMENT_RE) was only
# caught by a human looking at the actual sent email - this catches the
# same *shape* of problem (an implausible number, an internal
# inconsistency) without needing to know the exact new wording in advance.
MIN_PRICE_PER_M2_TRIEU = 15   # Hanoi apartments/houses are essentially
MAX_PRICE_PER_M2_TRIEU = 500  # never priced outside this per-m2 range


def flag_suspicious(l):
    """Returns a list of short warning strings for anything about this
    listing that looks off, or an empty list if it looks fine. Does not
    remove the listing - just marks it for a human to double-check."""
    warnings = []
    price = l["price_trieu"]

    if isinstance(price, (int, float)) and l["area"]:
        try:
            area = float(l["area"].replace(",", "."))
            if area > 0:
                per_m2 = price / area
                if per_m2 < MIN_PRICE_PER_M2_TRIEU or per_m2 > MAX_PRICE_PER_M2_TRIEU:
                    warnings.append(f"giá/m² bất thường ({per_m2:.0f} triệu/m²)")
        except ValueError:
            pass

    if isinstance(price, (int, float)) and l["title"]:
        title_price = parse_price_trieu(l["title"])
        if isinstance(title_price, (int, float)) and abs(title_price - price) > max(50, price * 0.05):
            warnings.append(f"giá trong tiêu đề ({format_price(title_price)}) khác giá hiển thị")

    if l.get("has_mini_hint"):
        area_val = None
        if l["area"]:
            try:
                area_val = float(l["area"].replace(",", "."))
            except ValueError:
                pass
        if area_val is None or area_val <= MINI_APARTMENT_HINT_MAX_AREA:
            warnings.append("có thể là chung cư mini (từ ngữ + diện tích nhỏ)")

    return warnings


def split_listing_chunks(html):
    """Slice the raw page HTML into one chunk per listing, using each
    listing's -idNNNN URL as the boundary (dedupes the image-link +
    title-link pair that both point at the same listing).

    Each chunk starts at the enclosing tag's opening '<', not at the
    href="..." match position itself - starting mid-tag (right at the
    href attribute) produces malformed HTML once sliced, which made
    BeautifulSoup mis-parse the fragment and occasionally return the
    wrong <a> tag entirely (a real bug: one listing's title/price ended up
    completely mismatched with its own - correct - URL). Rewinding to the
    preceding '<' keeps the opening <a ...> tag intact.
    """
    matches = list(LISTING_HREF_RE.finditer(html))
    seen = {}
    order = []
    for m in matches:
        href, listing_id = m.group(1), m.group(2)
        if listing_id not in seen:
            tag_start = html.rfind("<", 0, m.start())
            if tag_start == -1:
                tag_start = m.start()
            seen[listing_id] = tag_start
            order.append((listing_id, href, tag_start))

    chunks = []
    for i, (lid, href, start) in enumerate(order):
        next_start = order[i + 1][2] if i + 1 < len(order) else len(html)
        end = min(next_start, start + MAX_CHUNK_CHARS)
        chunks.append((lid, href, html[start:end]))
    return chunks


def parse_listing_chunk(lid, href, chunk_html, category):
    soup = BeautifulSoup(chunk_html, "html.parser")
    # Normalize to NFC (composed accents) before any regex matching below -
    # all the Vietnamese regex literals in this file are written in NFC,
    # but scraped HTML can use NFD (decomposed accents, e.g. "tỷ" as
    # separate base-letter + combining-mark codepoints) which looks
    # identical when displayed but silently fails to match an NFC regex.
    # This was a real, previously-unfixed gap (an earlier version of this
    # script normalized text; this rewrite didn't carry that over) and is
    # the likely explanation for at least one observed title/price
    # mismatch bug that a same-looking test string couldn't reproduce.
    text = unicodedata.normalize("NFC", re.sub(r"\s+", " ", soup.get_text(" ")).strip())

    # District-scoped Hanoi URLs turned out to occasionally include
    # cross-city content anyway (spotted a real Quận Bình Thạnh, TPHCM
    # listing coming through a /ha-noi/... page - likely a
    # suggested/sponsored listing bleeding in). Guard against it two ways:
    # require a confirmed "..., Hà Nội" district match, AND explicitly
    # reject if another major city is mentioned at all.
    if OTHER_CITY_RE.search(text):
        return None
    if MINI_APARTMENT_RE.search(text):
        return None

    title_tag = soup.find("a")
    title = title_tag.get_text(" ", strip=True) if title_tag else None
    # image alt text is often the real title when the <a> wraps only an <img>,
    # but Mogi prefixes alt text with "Hình ảnh " (lit. "Image") - strip that
    # off since it's not part of the actual listing title.
    if (not title or len(title) < 3) and soup.find("img", alt=True):
        title = soup.find("img", alt=True)["alt"]
        title = re.sub(r"^Hình ảnh\s+", "", title).strip()
    if title:
        title = unicodedata.normalize("NFC", title)

    area_m = AREA_RE.search(text)
    pn_m = PN_RE.search(text)
    wc_m = WC_RE.search(text)
    district_m = DISTRICT_RE.search(text)
    if not district_m:
        return None
    # A price mentioned directly in the title (e.g. "...Giá 6,7 tỷ") is
    # unambiguously this listing's price - prefer it over a price found
    # anywhere else in the chunk, since the wider chunk can occasionally
    # include unrelated content (see MAX_CHUNK_CHARS comment above).
    price = (parse_price_trieu(title) if title else None) or parse_price_trieu(text)
    posted_label, posted_date = parse_posted_date(text)
    # Soft hint (not a hard exclusion): wording common in mini-apartment ads,
    # computed from the full chunk text since these phrases rarely appear
    # in the title alone - see MINI_APARTMENT_HINT_RE comment above.
    has_mini_hint = bool(MINI_APARTMENT_HINT_RE.search(text))
    # Full-ish text snippet for AI review context - lets the AI judge price
    # plausibility against the actual description (floors, elevator,
    # frontage, etc.), not just a fixed price/m2 threshold. Capped to keep
    # the AI prompt a reasonable size across 50+ listings.
    raw_snippet = text[:400]

    # Thumbnail image - try src first, then common lazy-load attribute
    # names (real markup unverified, so this is a best-effort guess at
    # what attribute actually holds the real URL vs. a blank placeholder).
    image_url = None
    img_tag = soup.find("img")
    if img_tag:
        for attr in ("src", "data-src", "data-original", "data-lazy-src", "data-lazy"):
            val = img_tag.get(attr)
            if val and not val.startswith("data:"):  # skip inline base64 placeholder pixels
                image_url = val
                break
    if image_url and not image_url.startswith("http"):
        image_url = f"https://mogi.vn{image_url}" if image_url.startswith("/") else None

    url = href if href.startswith("http") else f"https://mogi.vn{href}"

    return {
        "id": lid,
        "category": category,
        "title": title or "(không có tiêu đề)",
        "url": url,
        "image_url": image_url,
        "district": district_m.group(1).strip() if district_m else None,
        "area": area_m.group(1) if area_m else None,
        "bedrooms": pn_m.group(1) if pn_m else None,
        "bathrooms": wc_m.group(1) if wc_m else None,
        "price_trieu": price,
        "posted_label": posted_label,
        "posted_date": posted_date.isoformat() if posted_date else None,
        "has_mini_hint": has_mini_hint,
        "raw_snippet": raw_snippet,
    }


def passes_filters(l, age_cutoff):
    price = l["price_trieu"]
    if isinstance(price, (int, float)) and price < MIN_PLAUSIBLE_PRICE_TRIEU:
        return False
    if age_cutoff is not None and l["posted_date"] and date.fromisoformat(l["posted_date"]) < age_cutoff:
        return False
    if MAX_PRICE_TRIEU > 0:
        if not (isinstance(price, (int, float)) and price <= MAX_PRICE_TRIEU):
            return False
    return True


# Hardcoded target - keep fetching one district at a time per category
# until this many listings SURVIVE filtering (price plausibility/age/
# budget), or all districts in HANOI_DISTRICT_SLUGS have been checked
# (a natural cap - no separate safety limit needed since that list is
# finite). No env vars needed - the number is fixed here.
MIN_LISTINGS_PER_CATEGORY = 50
PAGE_REQUEST_DELAY_SECONDS = 1.0


MAX_PAGES_PER_DISTRICT = 5  # try ?cp=2..5 within each district page too


def fetch_category_listings(category, age_cutoff=None):
    """Fetches up to MAX_PAGES_PER_DISTRICT pages per Hanoi district for the
    given category, moving to the next district once a district's own
    pagination stops returning new listings (or the per-district page cap
    is hit), continuing across districts until MIN_LISTINGS_PER_CATEGORY
    listings have survived filtering, or districts run out.
    """
    suffix = CATEGORY_URL_SUFFIX[category]
    listings = []
    seen_ids = set()
    valid_count = 0

    for district_name, slug in HANOI_DISTRICT_SLUGS:
        if valid_count >= MIN_LISTINGS_PER_CATEGORY:
            break

        base_url = f"https://mogi.vn/ha-noi/{slug}/{suffix}"
        for district_page in range(1, MAX_PAGES_PER_DISTRICT + 1):
            if valid_count >= MIN_LISTINGS_PER_CATEGORY:
                break

            url = base_url if district_page == 1 else f"{base_url}?cp={district_page}"
            if listings:  # be polite between requests, skip delay before the very first
                time.sleep(PAGE_REQUEST_DELAY_SECONDS)
            try:
                html = fetch_page(url)
            except requests.RequestException as e:
                print(f"  [{category}] failed to fetch {district_name} p{district_page} ({url}): {e}",
                      file=sys.stderr)
                break

            chunks = split_listing_chunks(html)
            if not chunks:
                if district_page == 1:
                    print(f"  [{category}] {district_name}: 0 listing IDs found (page structure "
                          f"may have changed, or genuinely no listings) - skipping district", file=sys.stderr)
                break

            new_here = 0
            dropped_excluded = 0
            for lid, href, chunk_html in chunks:
                if lid in seen_ids:
                    continue
                seen_ids.add(lid)
                new_here += 1  # counts as "new" for pagination-progress purposes even if dropped below
                parsed = parse_listing_chunk(lid, href, chunk_html, category)
                if parsed is None:
                    dropped_excluded += 1
                    continue
                listings.append(parsed)
                if passes_filters(parsed, age_cutoff):
                    valid_count += 1

            status = f", {dropped_excluded} dropped (non-Hanoi/mini-apartment)" if dropped_excluded else ""
            print(f"  [{category}] {district_name} p{district_page}: {new_here} new listing(s) found"
                  f"{status} (total kept so far: {len(listings)}, {valid_count} passing filters)")

            if new_here == 0:
                # this district's own pagination isn't returning anything new either -
                # move on to the next district rather than wasting more requests here
                break

    if valid_count < MIN_LISTINGS_PER_CATEGORY:
        print(f"  [{category}] WARNING: only {valid_count}/{MIN_LISTINGS_PER_CATEGORY} listings "
              f"passed filters after checking all {len(HANOI_DISTRICT_SLUGS)} district(s) "
              f"(up to {MAX_PAGES_PER_DISTRICT} page(s) each).", file=sys.stderr)

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
    age_cutoff = None
    if MAX_LISTING_AGE_DAYS > 0:
        age_cutoff = datetime.now().date() - timedelta(days=MAX_LISTING_AGE_DAYS)

    all_listings = []
    for category, _ in CATEGORIES:
        print(f"Fetching {category} - targeting {MIN_LISTINGS_PER_CATEGORY} listings after "
              f"filters, checking up to {len(HANOI_DISTRICT_SLUGS)} district page(s) ...")
        all_listings.extend(fetch_category_listings(category, age_cutoff=age_cutoff))
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
    district_str = escape(l["district"]) if l["district"] else "Không rõ quận/huyện"
    posted_str = escape(l["posted_label"]) if l["posted_label"] else "?"

    warnings = flag_suspicious(l)
    warning_html = ""
    if warnings:
        warning_text = escape("⚠️ Cần kiểm tra: " + "; ".join(warnings))
        warning_html = f'<br><span style="color:#b45309;font-size:12px;font-weight:bold;">{warning_text}</span>'

    if l.get("image_url"):
        thumb_html = (
            f'<img src="{escape(l["image_url"])}" alt="" width="64" height="64" '
            f'style="width:64px;height:64px;object-fit:cover;border-radius:4px;display:block;">'
        )
    else:
        thumb_html = '<div style="width:64px;height:64px;background:#eee;border-radius:4px;"></div>'

    return f"""
<tr>
<td style="padding:8px 4px 8px 12px;border-bottom:1px solid #eee;width:64px;">
  <a href="{escape(l['url'])}">{thumb_html}</a>
</td>
<td style="padding:8px 12px;border-bottom:1px solid #eee;">
  <a href="{escape(l['url'])}" style="color:#1a5fb4;text-decoration:none;font-weight:bold;">{escape(l['title'])}</a><br>
  <span style="color:#666;font-size:12px;">{district_str} · {detail_str} · đăng {posted_str}</span>{warning_html}
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
            rows = "\n".join(build_listing_row_html(l) for l in cat_listings)
            sections.append(f"""
<h2 style="color:#1a5fb4;font-size:18px;margin-top:28px;">{escape(category)} ({len(cat_listings)} tin) - sắp xếp theo giá tăng dần</h2>
<table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:640px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
<tbody>
{rows}
</tbody>
</table>""")
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
        lines.append(f"=== {category} ({len(cat_listings)} tin) - sap xep theo gia tang dan ===")
        for l in cat_listings:
            details = []
            if l["area"]:
                details.append(f"{l['area']} m2")
            if l["bedrooms"]:
                details.append(f"{l['bedrooms']} PN")
            if l["bathrooms"]:
                details.append(f"{l['bathrooms']} WC")
            detail_str = ", ".join(details) if details else "—"
            district_str = l["district"] or "Khong ro quan/huyen"
            posted_str = l["posted_label"] or "?"
            lines.append(f"  {l['title']}")
            lines.append(f"    {district_str} | {detail_str} | {format_price(l['price_trieu'])} | dang {posted_str}")
            lines.append(f"    {l['url']}")
            warnings = flag_suspicious(l)
            if warnings:
                lines.append(f"    !! CAN KIEM TRA: {'; '.join(warnings)}")
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


LISTINGS_JSON_PATH = os.path.join(EMAIL_DIR, "listings.json")
AI_PROMPT_PATH = os.path.join(EMAIL_DIR, "ai_prompt.txt")
AI_RESPONSE_PATH = os.path.join(EMAIL_DIR, "ai_response.json")


def build_ai_prompt(listings):
    """Builds the prompt file for the AI review step (run as a separate
    GitHub Actions step calling Copilot CLI directly - see workflow).
    Asks for a strict JSON verdict per listing so apply_ai_verdicts() can
    parse it without needing to understand free-form text.
    """
    compact = [
        {
            "id": l["id"],
            "title": l["title"],
            "district": l["district"],
            "area_m2": l["area"],
            "price": format_price(l["price_trieu"]),
            "listing_text": l.get("raw_snippet", ""),
        }
        for l in listings
    ]
    prompt = f"""You are reviewing a list of Hanoi real-estate listings scraped from a
property site. For EACH listing, decide if it is a genuine, normal house
or apartment FOR SALE in Hanoi at a plausible price, or if it should be
rejected because it is:
- a "chung cư mini" / mini-apartment / subdivided-room listing (any wording,
  not just the literal words "mini" or "CCMN" - use judgment on the title)
- located outside Hanoi
- not actually a real-estate sale listing (spam, ad, duplicate, nonsense title)
- priced in a way that is clearly inconsistent with the listing's own
  description - e.g. a multi-story house with an elevator, a large frontage,
  or a large area priced at only a few hundred triệu đồng is almost always a
  data-entry error (the "price" field probably picked up the wrong number
  during scraping, or the seller mistyped it), not a real bargain. Use your
  knowledge of realistic Hanoi property prices and the "listing_text" field
  (the actual scraped description) to judge this, not just the area/price
  ratio alone - a small area with a normal price is fine, but a price wildly
  below what the description implies (floors, elevator, frontage, location)
  should be rejected even if you can't independently know the "correct" price.

Respond with ONLY a JSON array, no markdown formatting, no code fences, no
explanation before or after. Each element must be exactly:
{{"id": "<id>", "verdict": "ok" or "reject", "reason": "<short reason, max 8 words>"}}

Include every id from the input exactly once. Here is the input:

{json.dumps(compact, ensure_ascii=False)}
"""
    with open(AI_PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(prompt)


def apply_ai_verdicts(listings):
    """Reads the AI review response (if present) and drops any listing the
    AI marked "reject". Designed to fail OPEN, not closed: if the response
    file is missing, unparseable, or incomplete, this falls back to keeping
    everything as-is (the existing rule-based filters and flag_suspicious()
    remain the safety net either way) rather than blocking the whole email
    over a malformed AI response - this step's output was never verified
    against a real run, so it needs to degrade gracefully.
    """
    if not os.path.exists(AI_RESPONSE_PATH):
        print("  No AI response file found - skipping AI review step (rule-based "
              "filters and flag_suspicious() still apply).", file=sys.stderr)
        return listings

    with open(AI_RESPONSE_PATH, encoding="utf-8") as f:
        raw = f.read().strip()

    if not raw:
        print("  AI response file is empty - skipping AI review step for this run.", file=sys.stderr)
        return listings

    # Strip markdown code fences if the model wrapped its JSON in them
    # despite being asked not to - common enough to guard against.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())

    verdicts = None
    try:
        verdicts = json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        pass

    if verdicts is None:
        # Calling Copilot CLI directly (not through a wrapper action) means
        # the raw output can have banner/log text before or after the
        # actual JSON - try pulling out the first [...] array found
        # anywhere in the text as a fallback before giving up entirely.
        m = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if m:
            try:
                verdicts = json.loads(m.group(0))
            except (json.JSONDecodeError, TypeError):
                pass

    if verdicts is None:
        print(f"  WARNING: could not parse AI response as JSON - skipping AI "
              f"review step for this run. Raw response (first 300 chars): {raw[:300]!r}",
              file=sys.stderr)
        return listings

    try:
        verdict_by_id = {v["id"]: v for v in verdicts if isinstance(v, dict) and "id" in v}
    except (TypeError, KeyError):
        print(f"  WARNING: AI response parsed as JSON but wasn't the expected list-of-objects "
              f"shape - skipping AI review step for this run.", file=sys.stderr)
        return listings

    rejected = []
    kept = []
    for l in listings:
        v = verdict_by_id.get(l["id"])
        if v and v.get("verdict") == "reject":
            rejected.append((l, v.get("reason", "")))
        else:
            kept.append(l)

    if rejected:
        print(f"  AI review rejected {len(rejected)} listing(s):", file=sys.stderr)
        for l, reason in rejected[:10]:
            print(f"    [{l['category']}] {format_price(l['price_trieu'])} - {l['title'][:50]} - {reason}",
                  file=sys.stderr)
        if len(rejected) > 10:
            print(f"    ... and {len(rejected) - 10} more", file=sys.stderr)

    unmatched = len(listings) - len(verdict_by_id.keys() & {l["id"] for l in listings})
    if unmatched:
        print(f"  Note: AI response didn't cover {unmatched} listing(s) - those were kept "
              f"(fails open).", file=sys.stderr)

    return kept


def cmd_prepare():
    """Phase 1: fetch listings, apply the existing rule-based filters, and
    write listings.json + the AI review prompt. Run cmd_build after an
    external AI review step has (optionally) written ai_response.json.
    """
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
    print(f"Total parsed after rule-based filters: {len(listings)} listing(s).")

    priced = [l for l in listings if l["price_trieu"] is not None]
    if listings and len(priced) / len(listings) < 0.5:
        print(
            f"  WARNING: only {len(priced)}/{len(listings)} listings had a parseable price - "
            "Mogi may have changed its price text format. Check parse_price_trieu().",
            file=sys.stderr,
        )

    with open(LISTINGS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(listings, f, ensure_ascii=False)
    build_ai_prompt(listings)
    print(f"Wrote {len(listings)} listing(s) to {LISTINGS_JSON_PATH} and the AI review "
          f"prompt to {AI_PROMPT_PATH}. Run the AI review step (see README), then run "
          f"'build'.")


def cmd_build():
    """Phase 2: read listings.json, apply the AI review verdict (if
    ai_response.json is present - written by the AI review workflow step),
    apply flag_suspicious() to whatever remains, and write the final email.
    """
    if not os.path.exists(LISTINGS_JSON_PATH):
        print(f"{LISTINGS_JSON_PATH} not found - run 'prepare' first.", file=sys.stderr)
        sys.exit(1)
    with open(LISTINGS_JSON_PATH, encoding="utf-8") as f:
        listings = json.load(f)

    listings = apply_ai_verdicts(listings)
    print(f"Total after AI review: {len(listings)} listing(s).")

    flagged = [(l, flag_suspicious(l)) for l in listings]
    flagged = [(l, w) for l, w in flagged if w]
    if flagged:
        print(f"  {len(flagged)} listing(s) flagged for review in the email itself (marked with ⚠️):",
              file=sys.stderr)
        for l, warnings in flagged[:10]:
            print(f"    [{l['category']}] {format_price(l['price_trieu'])} - {l['title'][:50]} - "
                  f"{'; '.join(warnings)}", file=sys.stderr)
        if len(flagged) > 10:
            print(f"    ... and {len(flagged) - 10} more", file=sys.stderr)

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
    print(f"Built email ({len(listings)} listings). Saved to ./{EMAIL_DIR}/")


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
    if len(sys.argv) != 2 or sys.argv[1] not in ("prepare", "build", "send"):
        print("Usage: python hanoi_house_price_emailer.py [prepare|build|send]", file=sys.stderr)
        print("  prepare - fetch listings, apply rule-based filters, write listings.json + AI prompt")
        print("  build   - apply AI review verdict (if present) and build the email")
        print("  send    - send the built email via Gmail SMTP")
        sys.exit(1)
    if sys.argv[1] == "prepare":
        cmd_prepare()
    elif sys.argv[1] == "build":
        cmd_build()
    else:
        cmd_send()


if __name__ == "__main__":
    main()
