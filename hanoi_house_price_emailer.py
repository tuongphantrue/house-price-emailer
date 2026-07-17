#!/usr/bin/env python3
"""
Hanoi House/Land Prices (many sources, silently skips whatever fails) -> Email
(runs on GitHub Actions, no local computer needed)

Same shape as the gold-price-emailer / 9gag-meme-emailer this is modeled on:
fetches price data, then emails an HTML digest via Gmail SMTP. Runs in two
phases so the workflow can persist dedup state *between* them:

    python hanoi_house_price_emailer.py generate
        -> tries every source in SOURCES, keeps whichever ones actually
           returned data, writes the composed email under ./email/, and
           updates the "last sent price" state file

    python hanoi_house_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

WHY MANY SOURCES, AND WHY THEY'RE SILENTLY SKIPPED
-----------------------------------------------------
Vietnamese gold prices have one clean daily aggregator (giavang.org) with a
simple table per seller. Hanoi housing prices don't have a real
equivalent, and several real estate sites front their pages with
Cloudflare-style bot protection that blocklists cloud/CI IP ranges
(GitHub Actions runners got a flat 403 from more than one site in
testing, regardless of headers - that's an IP-range block, not a markup
problem). So this script:

  1. Tries a public reader proxy (r.jina.ai) before a direct fetch, since
     that fetches from different infrastructure than GitHub's runners.
  2. Treats every source as fully independent and expendable: if a
     source errors, gets blocked, or its page structure doesn't match
     what the parser expects, that source is just left out of the email
     entirely - no error placeholder, no partial-failure noise. The
     footer lists which sources actually made it into that run.
  3. Keeps SOURCES as a plain list so adding, removing, or fixing one
     source doesn't touch the others.

Current sources (see SOURCES near the bottom):
  1. Mogi.vn - blended average price/m2 per district (house + land
     together), with a month-over-month % change.
  2-5. Batdongsan.com.vn, one per property type - Nhà mặt phố
     (street-front houses), Chung cư (apartments), Nhà riêng (regular
     houses), Đất nền (land) - each a min/max price/m2 range per
     district. Confirmed working via the reader proxy.
  6. Nhatot.com - city-wide average price/m2 by property type (Nhà
     riêng, Nhà phố, Nhà mặt tiền, Chung cư). Genuinely publishes
     computed averages, unlike most listing sites.

Several other well-known sites were checked and deliberately left out,
rather than added as speculative entries that would just come back
empty every run:
  - alonhadat.com.vn, cafeland.vn, dothi.net, homedy.com - only publish
    individual listing prices, no computed district/city average. There's
    nothing for a parser to reliably extract from these.
  - guland.vn - does publish per-district averages, but its robots.txt
    disallows automated access, so it's excluded on principle regardless
    of technical feasibility.

If you find a genuinely reliable apartment-only, house-only, or
per-district aggregator for Hanoi beyond what's listed above, adding it
as another SOURCES entry is the way to go - see fetch_nhatot() or
fetch_batdongsan_category() for examples of how a source is wired in.

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
     export USE_READER_PROXY="true"            # optional, default true
     export ALLOW_INSECURE_SSL_FALLBACK="false" # optional, last-resort TLS bypass
"""

import hashlib
import json
import os
import random
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.google.com/",
}

EMAIL_DIR = "email"
STATE_FILE = os.environ.get("STATE_FILE", "state/last_price.json")
SEND_ONLY_ON_CHANGE = os.environ.get("SEND_ONLY_ON_CHANGE", "false").lower() == "true"
ALLOW_INSECURE_SSL_FALLBACK = os.environ.get("ALLOW_INSECURE_SSL_FALLBACK", "false").lower() == "true"

# See the module docstring: a flat 403 regardless of headers is an
# IP-range block, not fixable by header tweaks - so try fetching from
# different infrastructure (a public reader proxy) first.
USE_READER_PROXY = os.environ.get("USE_READER_PROXY", "true").lower() == "true"
READER_PROXY_PREFIX = os.environ.get("READER_PROXY_PREFIX", "https://r.jina.ai/")

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


def _flatten_to_lines(content):
    """Turn either raw HTML (direct fetch) or Markdown (reader-proxy
    fetch) into a flat list of normalized, non-empty lines. Markdown
    renders links as "[text](url)" rather than plain text, so those are
    unwrapped to just their text before normalizing - otherwise
    line-based matching (e.g. against a district name) would never hit.
    """
    if "<html" in content.lower() or "<body" in content.lower() or "<table" in content.lower():
        soup = BeautifulSoup(content, "html.parser")
        text = soup.get_text("\n")
    else:
        text = content

    lines = []
    for line in text.split("\n"):
        m = re.match(r"^\[(.+?)\]\(.*\)$", line.strip())
        if m:
            line = m.group(1)
        line = norm(line)
        if line:
            lines.append(line)
    return lines


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


def _looks_like_challenge(text):
    lowered = text.lower()
    return next((m for m in CHALLENGE_MARKERS if m in lowered), None)


def _direct_fetch(url, label):
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

    print(f"  [{label}] direct GET {url} -> HTTP {resp.status_code}, {len(resp.text)} bytes", file=sys.stderr)
    hit = _looks_like_challenge(resp.text)
    if hit:
        print(f"  [{label}] Response contains '{hit}' - looks like a JS/anti-bot challenge page.", file=sys.stderr)
    resp.raise_for_status()
    return resp.text


RATE_LIMIT_MAX_RETRIES = int(os.environ.get("READER_PROXY_MAX_RETRIES", "3"))
RATE_LIMIT_BACKOFF_SECONDS = float(os.environ.get("READER_PROXY_BACKOFF_SECONDS", "4"))
READER_PROXY_MIN_BYTES = int(os.environ.get("READER_PROXY_MIN_BYTES", "10000"))
READER_PROXY_PACING_SECONDS = float(os.environ.get("READER_PROXY_PACING_SECONDS", "1.5"))


def fetch_page(url, label=""):
    """GET a page's content, printing diagnostics (status code, response
    length, anti-bot-challenge detection) instead of a bare failure.
    Tries a public reader proxy first (different infrastructure than
    GitHub's runners - relevant since a flat 403 regardless of headers is
    an IP-range block, not a markup problem), falling back to a direct
    fetch.

    The reader proxy is a shared free service with its own request-rate
    limit - running many requests back-to-back (e.g. one per district per
    property type) can trip a 429 on it well before any target site is
    even involved. A 429 here is retried with backoff rather than treated
    as a dead end, since immediately falling back to a direct fetch just
    trades one block for another (the site's own anti-bot page). A short
    pacing delay between calls also helps avoid tripping the limit in the
    first place.

    A "successful" (200, non-challenge) response that's suspiciously
    short is also not trusted at face value - real district/category
    pages have run tens of KB; a response of only a few KB is more likely
    a stub, redirect notice, or truncated render than the actual table,
    so that case falls through to a direct fetch too rather than being
    silently accepted as empty-but-valid.
    """
    if READER_PROXY_PACING_SECONDS > 0:
        time.sleep(READER_PROXY_PACING_SECONDS)

    if USE_READER_PROXY:
        proxied_url = READER_PROXY_PREFIX + url
        attempt = 0
        while attempt <= RATE_LIMIT_MAX_RETRIES:
            attempt += 1
            try:
                resp = requests.get(proxied_url, headers={"Accept": "text/plain"}, timeout=25)
            except requests.RequestException as e:
                print(f"  [{label}] reader proxy fetch failed ({e}) - falling back to a direct fetch.", file=sys.stderr)
                break

            print(f"  [{label}] reader-proxy GET {proxied_url} -> HTTP {resp.status_code}, {len(resp.text)} bytes", file=sys.stderr)

            if resp.status_code == 429:
                if attempt <= RATE_LIMIT_MAX_RETRIES:
                    wait = RATE_LIMIT_BACKOFF_SECONDS * attempt
                    print(f"  [{label}] reader proxy rate-limited (429) - retrying in {wait:.0f}s (attempt {attempt}/{RATE_LIMIT_MAX_RETRIES}).", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"  [{label}] reader proxy still rate-limited after {RATE_LIMIT_MAX_RETRIES} retries - falling back to a direct fetch.", file=sys.stderr)
                break

            if resp.status_code == 200 and not _looks_like_challenge(resp.text):
                if len(resp.text) < READER_PROXY_MIN_BYTES:
                    print(
                        f"  [{label}] reader proxy returned only {len(resp.text)} bytes (< {READER_PROXY_MIN_BYTES}) - "
                        f"too short to trust as the real page, snippet: {resp.text[:300]!r}",
                        file=sys.stderr,
                    )
                    break
                return resp.text

            print(f"  [{label}] reader proxy didn't return usable content - falling back to a direct fetch.", file=sys.stderr)
            break

    return _direct_fetch(url, label)


# ---------------------------------------------------------------------------
# Shared district list + generic price patterns, reused across sources
# ---------------------------------------------------------------------------

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

DISTRICT_SLUGS = [
    ("Ba Đình", "ba-dinh"), ("Hoàn Kiếm", "hoan-kiem"), ("Đống Đa", "dong-da"),
    ("Hai Bà Trưng", "hai-ba-trung"), ("Tây Hồ", "tay-ho"), ("Cầu Giấy", "cau-giay"),
    ("Thanh Xuân", "thanh-xuan"), ("Hoàng Mai", "hoang-mai"), ("Long Biên", "long-bien"),
    ("Hà Đông", "ha-dong"), ("Nam Từ Liêm", "nam-tu-liem"), ("Bắc Từ Liêm", "bac-tu-liem"),
]

PRICE_RE = re.compile(r"([\d][\d.,]*)\s*triệu\s*/\s*m2", re.IGNORECASE)
PERCENT_RE = re.compile(r"([\d][\d.,]*)\s*%")

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


# ---------------------------------------------------------------------------
# Source: Mogi.vn - one page, every Hanoi district, blended avg price/m2
# + month-over-month % change.
# ---------------------------------------------------------------------------

MOGI_URL = os.environ.get("MOGI_URL", "https://mogi.vn/gia-nha-dat")


def fetch_mogi():
    """Parse Mogi.vn's Hanoi section into [{area, price, change, direction}].

    Scans forward from each known district name for the first line with a
    price, bounded by either a max lookahead or the next known district
    name (whichever comes first) - not a fixed "next line" offset. Mogi's
    page has changed its exact markup/spacing between the name and price
    more than once during development of this script (sometimes 1 line
    apart, sometimes more), so a fixed offset breaks intermittently; this
    bounded scan tolerates that drift without reintroducing the earlier
    bug where an unbounded lookahead let one row's % change bleed into
    the row above it - bounding at the next district name (or a small
    max) prevents that regardless of how many lines apart things end up
    being.
    """
    html = fetch_page(MOGI_URL, label="Mogi.vn")
    lines = _flatten_to_lines(html)
    areas_norm = {norm(a): a for a in HANOI_AREAS}
    area_indices = [i for i, line in enumerate(lines) if line in areas_norm]

    rows = []
    seen = set()
    max_lookahead = 6
    for idx, i in enumerate(area_indices):
        area = areas_norm[lines[i]]
        if area in seen:
            continue
        next_area_i = area_indices[idx + 1] if idx + 1 < len(area_indices) else len(lines)
        window_end = min(i + 1 + max_lookahead, next_area_i, len(lines))

        price = None
        price_line_idx = None
        for j in range(i + 1, window_end):
            m = PRICE_RE.search(lines[j])
            if m:
                price = m.group(1)
                price_line_idx = j
                break
        if price is None:
            continue

        change = None
        direction = None
        for j in range(price_line_idx, min(price_line_idx + 2, window_end)):
            w = lines[j]
            pm = PERCENT_RE.search(w)
            if pm and change is None:
                change = pm.group(1)
            if "▲" in w:
                direction = "up"
            elif "▼" in w:
                direction = "down"
            if change is not None and direction is not None:
                break

        seen.add(area)
        rows.append({"area": area, "price": price, "change": change, "direction": direction})

    if not rows:
        found_areas = [a for a in HANOI_AREAS if norm(a) in lines]
        print(
            f"  [Mogi.vn] fetched {len(html)} bytes, {len(lines)} lines, but found 0 matching "
            f"district+price pairs. District names found in the text: {found_areas or '(none)'}. "
            f"First 300 chars: {html[:300]!r}",
            file=sys.stderr,
        )
    return rows


def render_change_table(rows):
    def change_html(change, direction):
        if not change:
            return "<span style='color:#999'>—</span>"
        color = "#1a7f37" if direction == "up" else ("#cf222e" if direction == "down" else "#666")
        arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "")
        return f"<span style='color:{color}'>{escape(change)}% {arrow}</span>"

    row_html = "\n".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['price'])} triệu/m²</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{change_html(r['change'], r['direction'])}</td></tr>"
        for r in rows
    )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
    <thead><tr style="background:#f5f5f5;"><th style="padding:8px 12px;text-align:left;">Quận / Huyện</th><th style="padding:8px 12px;text-align:right;">Giá trung bình</th><th style="padding:8px 12px;text-align:right;">So với tháng trước</th></tr></thead>
    <tbody>{row_html}</tbody>
    </table>"""


def render_change_text(rows):
    lines = []
    for r in rows:
        change_str = f"{r['change']}%" if r["change"] else "—"
        lines.append(f"{r['area']}: {r['price']} trieu/m2 ({change_str} so voi thang truoc)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Source: Batdongsan.com.vn, one entry per property type - each fetched
# per district (mirrors the gold script's per-seller pattern: one
# district failing doesn't take down the others). Confirmed working via
# the reader proxy.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sample listings - real individual properties (not aggregate stats),
# pulled from the same batdongsan.com.vn category/district pages already
# being fetched above for price-range data. No extra requests: each
# listing page's content already contains individual listing cards
# alongside the price summary, so this just extracts a few of them as a
# by-product of the existing fetch.
#
# Listing cards only parse out of the reader-proxy's Markdown rendering
# (they appear as "[Ảnh đại diện ...](url "title")" links) - a raw HTML
# direct-fetch fallback won't match this regex, so districts that fell
# back to a direct fetch just won't contribute a sample, which is fine
# given this is illustrative, not the main price data.
# ---------------------------------------------------------------------------

SAMPLE_LISTINGS = []  # reset per run in cmd_generate(); populated by fetch_batdongsan_category

LISTING_LINK_RE = re.compile(r'\[(Ảnh đại diện[^\]]{0,400})\]\((https://batdongsan\.com\.vn/[^\s\)]+)(?:\s+"([^"]*)")?\)')
# Price and area are anchored together via "·" in the card format
# ("1.250 tỷ ·476,4 m²") - matching them as one pair rather than
# separately avoids false-matching an unrelated price mentioned earlier
# in the description (e.g. "HĐ thuê 36 tỷ/năm" - a rental-income figure -
# appearing before the actual sale price in some listing titles).
LISTING_PRICE_AREA_RE = re.compile(
    r'([\d][\d.,]*\s*tỷ(?:/năm)?|[Gg]iá thỏa thuận|[Tt]hỏa thuận)\s*·\s*([\d][\d.,]*)\s*m²'
)
LISTING_WARD_RE = re.compile(r'P\.\s*([^(]+?)\s*\(')


def extract_sample_listings(content, category_label, district_label, max_samples=2):
    """Pull up to max_samples individual listing cards out of a
    batdongsan.com.vn category/district page's Markdown content. Best
    effort - a card missing a clear price+area pair is skipped rather
    than guessed at, since this is illustrative content, not data to be
    relied on.
    """
    out = []
    for match in LISTING_LINK_RE.finditer(content):
        if len(out) >= max_samples:
            break
        link_text, url, title_attr = match.groups()
        pa_m = LISTING_PRICE_AREA_RE.search(link_text)
        if not pa_m:
            continue
        ward_m = LISTING_WARD_RE.search(link_text)
        title = (title_attr or "").strip() or link_text[:80].strip()
        out.append({
            "category": category_label,
            "district": district_label,
            "title": title,
            "price": pa_m.group(1).strip(),
            "area": pa_m.group(2).strip(),
            "ward": ward_m.group(1).strip() if ward_m else district_label,
            "url": url,
        })
    return out


def render_sample_listings_html(listings):
    if not listings:
        return ""
    cards = "\n".join(
        f"""<div style="border:1px solid #eee;border-radius:8px;padding:10px 14px;margin-bottom:8px;max-width:600px;">
    <div style="font-size:13px;color:#1a5fb4;font-weight:bold;">{escape(l['category'])}</div>
    <div style="font-size:14px;margin:2px 0;"><a href="{escape(l['url'])}" style="color:#111;text-decoration:none;">{escape(l['title'])}</a></div>
    <div style="font-size:13px;color:#555;">{escape(l['price'])} · {escape(l['area'])} m² · {escape(l['ward'])}</div>
    </div>"""
        for l in listings
    )
    return f"""
  <h2 style="color:#333;font-size:16px;border-bottom:2px solid #1a5fb4;padding-bottom:4px;margin-top:24px;">
    Nhà mẫu tham khảo (tin đăng thực tế)
  </h2>
  <p style="color:#666;font-size:13px;">
    Một vài tin đăng thực tế lấy từ Batdongsan.com.vn, chỉ mang tính minh họa - không phải danh sách đầy đủ hay gợi ý mua.
  </p>
  {cards}"""


def render_sample_listings_text(listings):
    if not listings:
        return ""
    lines = ["== NHA MAU THAM KHAO (tin dang thuc te) =="]
    for l in listings:
        lines.append(f"[{l['category']}] {l['title']} - {l['price']} - {l['area']} m2 - {l['ward']}")
        lines.append(f"  {l['url']}")
    return "\n".join(lines)


def fetch_batdongsan_category(url_prefix, label):
    rows = []
    for name, slug in DISTRICT_SLUGS:
        url = f"https://batdongsan.com.vn/{url_prefix}-{slug}"
        try:
            html = fetch_page(url, label=f"{label}/{slug}")
        except requests.RequestException as e:
            print(f"  [{label}/{slug}] fetch failed: {e}", file=sys.stderr)
            continue
        SAMPLE_LISTINGS.extend(extract_sample_listings(html, category_label=label, district_label=name))
        text = norm(" ".join(_flatten_to_lines(html)))
        m = BDS_RANGE_RE.search(text) or BDS_RANGE_SHARED_UNIT_RE.search(text)
        if not m:
            print(f"  [{label}/{slug}] no price range found in response - skipping this district.", file=sys.stderr)
            continue
        rows.append({"area": name, "low": m.group(1), "high": m.group(2)})
    return rows


def render_range_table(rows):
    row_html = "\n".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
        f"<td colspan='2' style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['low'])} - {escape(r['high'])} triệu/m²</td></tr>"
        for r in rows
    )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
    <thead><tr style="background:#f5f5f5;"><th style="padding:8px 12px;text-align:left;">Quận</th><th colspan="2" style="padding:8px 12px;text-align:right;">Khoảng giá</th></tr></thead>
    <tbody>{row_html}</tbody>
    </table>"""


def render_range_text(rows):
    return "\n".join(f"{r['area']}: {r['low']} - {r['high']} trieu/m2" for r in rows)


# ---------------------------------------------------------------------------
# Source: Nhatot.com (Chợ Tốt Nhà) - publishes actual computed category
# averages city-wide (unlike most listing sites, which only show
# individual asking prices with no aggregate). Confirmed via manual
# research; the site also has bot detection, so this relies on the
# reader-proxy path like the others.
# ---------------------------------------------------------------------------

NHATOT_URL = os.environ.get("NHATOT_URL", "https://www.nhatot.com/mua-ban-nha-dat-ha-noi")

# e.g. "Nhà mặt tiền Hà Nội: 200 triệu – 1 tỷ/m²" or "Nhà phố Hà Nội: 120 – 350 triệu/m²"
NHATOT_CATEGORY_RE = re.compile(
    r"(Nhà riêng, nhà nguyên căn|Nhà phố|Nhà mặt tiền|Chung cư)\s+Hà Nội:\s*"
    r"([\d][\d.,]*)\s*(triệu|tỷ)?\s*[–\-]\s*([\d][\d.,]*)\s*(triệu|tỷ)\s*/\s*m",
    re.IGNORECASE,
)


def _to_trieu(num_str, unit):
    val = _vn_to_float(num_str)
    return val * 1000 if unit == "tỷ" else val


def fetch_nhatot():
    html = fetch_page(NHATOT_URL, label="Nhatot.com")
    text = norm(" ".join(_flatten_to_lines(html)))
    rows = []
    for m in NHATOT_CATEGORY_RE.finditer(text):
        label, num1, unit1, num2, unit2 = m.groups()
        unit1 = unit1 or unit2  # first number often shares the second's unit
        low = _to_trieu(num1, unit1)
        high = _to_trieu(num2, unit2)
        rows.append({"area": label.strip(), "low": f"{low:.0f}", "high": f"{high:.0f}"})
    return rows


# ---------------------------------------------------------------------------
# Source roster - each is fully independent; a failure here just means
# that entry gets left out of the email (see cmd_generate).
#
# Only sources with verified, real aggregate data are listed here. Several
# other well-known sites were checked and dropped: alonhadat.com.vn,
# cafeland.vn, dothi.net, and homedy.com only publish individual listing
# prices with no computed average - there's nothing for a parser to
# reliably extract. guland.vn does publish per-district averages, but its
# robots.txt disallows automated access, so it's excluded on principle
# regardless of technical feasibility.
# ---------------------------------------------------------------------------

SOURCES = [
    {
        "name": "Mogi.vn - giá nhà đất bình quân (nhà + đất, gộp)",
        "fetch": fetch_mogi,
        "render_html": render_change_table,
        "render_text": render_change_text,
    },
    {
        "name": "Batdongsan.com.vn - Nhà mặt phố",
        "fetch": lambda: fetch_batdongsan_category("ban-nha-mat-pho", "Batdongsan/NhaMatPho"),
        "render_html": render_range_table,
        "render_text": render_range_text,
    },
    {
        "name": "Batdongsan.com.vn - Chung cư",
        "fetch": lambda: fetch_batdongsan_category("ban-can-ho-chung-cu", "Batdongsan/ChungCu"),
        "render_html": render_range_table,
        "render_text": render_range_text,
    },
    {
        "name": "Batdongsan.com.vn - Nhà riêng",
        "fetch": lambda: fetch_batdongsan_category("ban-nha-rieng", "Batdongsan/NhaRieng"),
        "render_html": render_range_table,
        "render_text": render_range_text,
    },
    {
        "name": "Batdongsan.com.vn - Đất nền",
        "fetch": lambda: fetch_batdongsan_category("ban-dat", "Batdongsan/DatNen"),
        "render_html": render_range_table,
        "render_text": render_range_text,
    },
    {
        "name": "Nhatot.com - giá theo loại hình (toàn Hà Nội)",
        "fetch": fetch_nhatot,
        "render_html": render_range_table,
        "render_text": render_range_text,
    },
]

TYPICAL_SIZES_M2 = [30, 50, 70, 100]

# Maps a SOURCES entry's name to how it should appear in this summary, and
# how to read its rows (range = has low/high; point = has a single price).
TYPICAL_PRICE_CATEGORIES = {
    "Mogi.vn - giá nhà đất bình quân (nhà + đất, gộp)": ("Nhà đất bình quân (nhà + đất)", "point"),
    "Batdongsan.com.vn - Nhà mặt phố": ("Nhà mặt phố", "range"),
    "Batdongsan.com.vn - Chung cư": ("Chung cư (căn hộ)", "range"),
    "Batdongsan.com.vn - Nhà riêng": ("Nhà riêng", "range"),
    "Batdongsan.com.vn - Đất nền": ("Đất nền (đất)", "range"),
}


def _vn_to_float(s):
    # Values seen so far are plain integers or a decimal comma (e.g.
    # "3,5"), never a thousands separator - triệu/m2 magnitudes stay well
    # under that range. If a source ever sends "1.234,5" style numbers
    # this will need a thousands-separator-aware version.
    return float(s.replace(",", "."))


def _format_money_trieu(trieu):
    if trieu >= 1000:
        ty = f"{trieu / 1000:.2f}".rstrip("0").rstrip(".")
        return f"{ty} tỷ"
    return f"{trieu:.0f} triệu"


def compute_typical_prices(results):
    """Return [{"label": ..., "kind": "range"|"point", "per_m2": (low, high) or price, "sizes": {size: text}}]
    for whichever categories in TYPICAL_PRICE_CATEGORIES actually have
    data this run. City-wide per-m2 figures are the average across
    whatever districts that source returned (not every district
    necessarily reported, if some were skipped) - so treat this as a
    rough illustration, not a precise citywide average.
    """
    out = []
    for r in results:
        mapping = TYPICAL_PRICE_CATEGORIES.get(r["name"])
        if not mapping:
            continue
        label, kind = mapping
        rows = r["rows"]
        if kind == "range":
            lows = [_vn_to_float(x["low"]) for x in rows if "low" in x]
            highs = [_vn_to_float(x["high"]) for x in rows if "high" in x]
            if not lows or not highs:
                continue
            avg_low, avg_high = sum(lows) / len(lows), sum(highs) / len(highs)
            sizes = {
                size: f"{_format_money_trieu(avg_low * size)} - {_format_money_trieu(avg_high * size)}"
                for size in TYPICAL_SIZES_M2
            }
            out.append({"label": label, "kind": kind, "per_m2": (avg_low, avg_high), "sizes": sizes})
        else:
            prices = [_vn_to_float(x["price"]) for x in rows if "price" in x]
            if not prices:
                continue
            avg_price = sum(prices) / len(prices)
            sizes = {size: _format_money_trieu(avg_price * size) for size in TYPICAL_SIZES_M2}
            out.append({"label": label, "kind": kind, "per_m2": avg_price, "sizes": sizes})
    return out


def render_typical_price_html(typical):
    if not typical:
        return ""
    header_cells = "".join(f"<th style='padding:8px 12px;text-align:right;'>{size} m²</th>" for size in TYPICAL_SIZES_M2)
    body_rows = "\n".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(t['label'])}</strong></td>"
        + "".join(f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(t['sizes'][size])}</td>" for size in TYPICAL_SIZES_M2)
        + "</tr>"
        for t in typical
    )
    return f"""
  <h2 style="color:#333;font-size:16px;border-bottom:2px solid #1a5fb4;padding-bottom:4px;margin-top:24px;">
    Giá nhà điển hình tại Hà Nội (ước tính)
  </h2>
  <p style="color:#666;font-size:13px;">
    Tính từ giá trung bình/m² ở trên nhân với diện tích tham khảo - chỉ mang tính minh họa, không phải giá thực tế của một căn nhà cụ thể.
  </p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
  <thead><tr style="background:#f5f5f5;"><th style="padding:8px 12px;text-align:left;">Loại hình</th>{header_cells}</tr></thead>
  <tbody>{body_rows}</tbody>
  </table>"""


def render_typical_price_text(typical):
    if not typical:
        return ""
    lines = ["== GIA NHA DIEN HINH TAI HA NOI (uoc tinh) =="]
    for t in typical:
        parts = ", ".join(f"{size}m2: {t['sizes'][size]}" for size in TYPICAL_SIZES_M2)
        lines.append(f"{t['label']}: {parts}")
    lines.append("(Tinh tu gia trung binh/m2 nhan voi dien tich tham khao - chi mang tinh minh hoa.)")
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


def run_all_sources():
    """Try every source; return only the ones that actually produced
    rows, each as {name, rows, render_html, render_text}. Anything that
    errors or comes back empty is logged to stderr and left out - no
    error placeholder in the email itself.
    """
    results = []
    for src in SOURCES:
        print(f"Fetching: {src['name']} ...")
        try:
            rows = src["fetch"]()
        except Exception as e:
            print(f"  {src['name']}: failed entirely ({e}) - skipping.", file=sys.stderr)
            continue
        if not rows:
            print(f"  {src['name']}: 0 rows - skipping (see diagnostics above).", file=sys.stderr)
            continue
        print(f"  {src['name']}: {len(rows)} row(s).")
        results.append({"name": src["name"], "rows": rows, "render_html": src["render_html"], "render_text": src["render_text"]})
    return results


def build_html(results, listings, timestamp):
    if not results:
        sections = "<p>No source returned data this run. Check the workflow logs.</p>"
    else:
        sections = "\n".join(
            f"""
  <h2 style="color:#333;font-size:16px;border-bottom:2px solid #1a5fb4;padding-bottom:4px;margin-top:24px;">
    {escape(r['name'])}
  </h2>
  {r['render_html'](r['rows'])}"""
            for r in results
        )
    typical_section = render_typical_price_html(compute_typical_prices(results))
    listings_section = render_sample_listings_html(listings)
    return f"""\
<html>
<body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
  <h1 style="color:#1a5fb4;">Giá nhà đất Hà Nội theo quận/huyện</h1>
  <p style="color:#555;">Cập nhật {escape(timestamp)}</p>
  {sections}
  {typical_section}
  {listings_section}
  <p style="color:#999; font-size:12px; margin-top:20px;">
    Nguồn đã lấy được dữ liệu lần này: {escape(", ".join(r["name"] for r in results)) if results else "(không có)"} ·
    Đơn vị: triệu đồng/m² · Email tự động, chỉ mang tính tham khảo, không phải
    lời khuyên đầu tư.
  </p>
</body>
</html>"""


def build_plain_text(results, listings, timestamp):
    lines = [f"Gia nha dat Ha Noi theo quan/huyen - cap nhat {timestamp}", ""]
    if not results:
        lines.append("No source returned data this run. Check the workflow logs.")
    else:
        for r in results:
            lines.append(f"== {r['name']} ==")
            lines.append(r["render_text"](r["rows"]))
            lines.append("")
        typical_text = render_typical_price_text(compute_typical_prices(results))
        if typical_text:
            lines.append(typical_text)
            lines.append("")
        listings_text = render_sample_listings_text(listings)
        if listings_text:
            lines.append(listings_text)
    return "\n".join(lines)


def cmd_generate():
    if os.path.exists(EMAIL_DIR):
        for f in os.listdir(EMAIL_DIR):
            os.remove(os.path.join(EMAIL_DIR, f))
    os.makedirs(EMAIL_DIR, exist_ok=True)

    SAMPLE_LISTINGS.clear()
    results = run_all_sources()
    print(f"\n{len(results)}/{len(SOURCES)} source(s) returned data this run.")
    print(f"Collected {len(SAMPLE_LISTINGS)} sample listing(s) as a by-product of the fetches above.")

    max_listings = int(os.environ.get("MAX_SAMPLE_LISTINGS", "8"))
    if len(SAMPLE_LISTINGS) > max_listings:
        listings = random.sample(SAMPLE_LISTINGS, max_listings)
    else:
        listings = list(SAMPLE_LISTINGS)

    combined = {r["name"]: r["rows"] for r in results}
    price_hash = hash_data(combined)
    last_hash = load_last_hash()

    if results and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Gia nha dat Ha Noi - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(results, listings, timestamp)
    text_body = build_plain_text(results, listings, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"send": bool(results), "sources": [r["name"] for r in results]}, f)

    if results:
        save_last_hash(price_hash)
    print(f"Generated email ({len(results)} source(s) included). Saved to ./{EMAIL_DIR}/")


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
        print("Nothing to send this run (no source returned data, or prices unchanged).")
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
