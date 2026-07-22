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

# ---------------------------------------------------------------------------
# Email design tokens. Palette is drawn from Hanoi's own visual vocabulary
# rather than a generic dashboard look: the ochre/mustard is the color of
# the French-colonial building facades throughout the Old Quarter and
# government buildings; the lacquer red-orange echoes traditional sơn mài
# lacquerware and the Thê Húc bridge. Price-direction colors follow the
# Vietnamese/Asian market convention (tăng/up = red, giảm/down = green) -
# the opposite of the US convention, but the correct one for this
# audience and content. Serif (Georgia) for headings/prices gives a
# "ledger" quality fitting a price digest; sans (Helvetica/Arial) for
# labels and table data keeps numbers easy to scan. Both are near-universal
# system fonts, since email clients can't reliably load custom web fonts.
# ---------------------------------------------------------------------------

C_BG = "#EDE6D6"           # outer page background
C_PANEL = "#F7F2E7"        # main content panel (warm cream)
C_HEADER = "#B8823A"       # ochre header band
C_HEADER_TEXT = "#FFFBF3"
C_HEADER_MUTED = "#F1DDB4"
C_INK = "#2B2420"          # body text (warm near-black)
C_MUTED = "#8A7A63"        # secondary text
C_MUTED_LIGHT = "#9C9080"  # footer text
C_RULE = "#E4D9C4"         # borders/dividers
C_RULE_LIGHT = "#F1EBDC"   # row dividers within tables
C_ZEBRA = "#FBF8F1"        # alternate row background
C_CARD = "#FFFFFF"
C_UP = "#B8471F"           # tăng/increase (Vietnamese convention: red)
C_UP_BG = "#FBE7DD"
C_DOWN = "#4B7A63"         # giảm/decrease (Vietnamese convention: green)
C_DOWN_BG = "#E3ECE6"
C_TAG_BG = "#F1E3C9"
C_TAG_TEXT = "#8A5A1E"
C_EYEBROW = "#9C6A22"

F_DISPLAY = "Georgia, 'Times New Roman', serif"
F_BODY = "Helvetica, Arial, sans-serif"


def section_eyebrow_html(kicker, title, subtitle=None):
    subtitle_html = f'<div style="font-family:{F_BODY};font-size:12px;color:{C_MUTED};margin-top:4px;">{escape(subtitle)}</div>' if subtitle else ""
    return f"""
  <div style="font-family:{F_BODY};font-size:11px;letter-spacing:1.5px;color:{C_EYEBROW};text-transform:uppercase;font-weight:bold;">— {escape(kicker)}</div>
  <div style="font-family:{F_DISPLAY};font-size:18px;color:{C_INK};margin-top:2px;">{title}</div>
  {subtitle_html}"""


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
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("hash")
    except (json.JSONDecodeError, OSError) as e:
        print(f"  could not read {path} ({e}) - starting with empty dedup state", file=sys.stderr)
        return None


def save_last_hash(price_hash, path=STATE_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
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
    # Normalize to NFC here, at the single point all fetched content
    # passes through - see the note on fetch_page() for why this matters.
    return unicodedata.normalize("NFC", resp.text)


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
                # Normalize to NFC before returning. This turned out to be
                # the actual root cause of the sample-listings feature
                # matching 0 cards for a long time: r.jina.ai appears to
                # emit Vietnamese text in NFD (decomposed) form, which is
                # visually - and even when printed to a log - identical to
                # NFC, but fails every substring/regex match against an
                # NFC literal in this source file (e.g. "Ảnh đại diện" in
                # NFC never matches its own NFD-decomposed self as a
                # substring). Price-range extraction was never affected
                # because that path already ran fetched lines through
                # norm() -> unicodedata.normalize("NFC", ...) as part of
                # _flatten_to_lines(); listing-card extraction operated on
                # the raw fetched text directly and had no such step. Now
                # every consumer of fetch_page()'s return value gets
                # consistently normalized text, regardless of what form
                # the source actually sent.
                return unicodedata.normalize("NFC", resp.text)

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
            return f"<span style='color:{C_MUTED};font-size:13px;'>—</span>"
        color = C_UP if direction == "up" else (C_DOWN if direction == "down" else C_MUTED)
        bg = C_UP_BG if direction == "up" else (C_DOWN_BG if direction == "down" else C_RULE_LIGHT)
        arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "")
        return f"<span style='display:inline-block;background:{bg};color:{color};font-size:12px;font-weight:bold;padding:2px 9px;border-radius:10px;'>{arrow} {escape(change)}%</span>"

    row_html = "\n".join(
        f"<tr style='{'background:' + C_ZEBRA + ';' if i % 2 else ''}'>"
        f"<td style='padding:10px 16px;font-family:{F_BODY};font-size:14px;color:{C_INK};border-bottom:1px solid {C_RULE_LIGHT};'>{escape(r['area'])}</td>"
        f"<td style='padding:10px 16px;font-family:{F_BODY};font-size:14px;color:{C_INK};text-align:right;border-bottom:1px solid {C_RULE_LIGHT};'>{escape(r['price'])} triệu/m²</td>"
        f"<td style='padding:10px 16px;text-align:right;border-bottom:1px solid {C_RULE_LIGHT};'>{change_html(r['change'], r['direction'])}</td></tr>"
        for i, r in enumerate(rows)
    )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:separate;width:100%;max-width:600px;background:{C_CARD};border:1px solid {C_RULE};border-radius:8px;overflow:hidden;">
    <thead><tr>
      <th style="padding:10px 16px;text-align:left;font-family:{F_BODY};font-size:11px;color:{C_MUTED};text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid {C_RULE};">Quận / Huyện</th>
      <th style="padding:10px 16px;text-align:right;font-family:{F_BODY};font-size:11px;color:{C_MUTED};text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid {C_RULE};">Giá trung bình</th>
      <th style="padding:10px 16px;text-align:right;font-family:{F_BODY};font-size:11px;color:{C_MUTED};text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid {C_RULE};">So với tháng trước</th>
    </tr></thead>
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
# Confirmed structure (via a production diagnostic dump, not assumption):
# each listing card is an outer Markdown link whose *link text* contains
# one or more embedded photo images before the descriptive text - e.g.
#   [![Image 1: Ảnh đại diện](https://file4.batdongsan.com.vn/crop/.../a.jpg)![Image 2: Ảnh đại diện](https://file4.batdongsan.com.vn/crop/.../b.jpg)
#    TITLE PRICE ·AREA m² ... P. WARD (...)](https://batdongsan.com.vn/listing-url "title")
# An earlier version of this assumed "Ảnh đại diện" appeared as plain
# link text (based on inspecting these pages through a different fetch
# path than what the actual GitHub Actions run uses) - it never does;
# it's always the alt text of a nested image tag, which is why the old
# pattern matched 0 cards on every single page despite being fetched
# correctly. The outer closing is distinguished from the inner images'
# closings by host: images close with "](https://file4.batdongsan.com.vn/...)",
# the real listing link closes with "](https://batdongsan.com.vn/...)"
# (no "file4." subdomain) - matching specifically on the latter lets the
# scan correctly skip over any number of embedded images to find the
# actual listing URL.
#
# This only parses out of the reader-proxy's Markdown rendering; a raw
# HTML direct-fetch fallback won't match, so districts that fell back to
# a direct fetch just won't contribute a sample - fine, since this is
# illustrative, not the main price data.
# ---------------------------------------------------------------------------

SAMPLE_LISTINGS = []  # reset per run in cmd_generate(); populated by fetch_batdongsan_category

# How many candidate listings to pull per district page before filtering
# (a bigger pool means more chances of finding ones under the price cap,
# since a district page's first few "featured" listings tend to skew
# toward premium/expensive properties) and how many make it into the
# final email after filtering.
LISTING_CANDIDATES_PER_DISTRICT = int(os.environ.get("LISTING_CANDIDATES_PER_DISTRICT", "6"))

# Only show listings at or under this total price, in tỷ đồng (billions
# of VND). None disables the filter (show everything found). Listings
# priced "Giá thỏa thuận" (negotiable, no figure given) are excluded
# when a cap is set, since there's no number to compare against - not
# assumed to pass or fail the cap.
_max_price_env = os.environ.get("LISTING_MAX_PRICE_TY", "2")
LISTING_MAX_PRICE_TY = float(_max_price_env) if _max_price_env.strip() else None


# Plausible price/m2 range for Hanoi real estate, in trieu/m2, used to
# disambiguate ambiguous total prices below - covers everything observed
# across categories so far, from cheap outlying-huyen land (~60) up to
# prime Hoan Kiem street frontage (~2950), with margin on both ends.
PLAUSIBLE_PRICE_PER_M2_TRIEU = (15, 4000)


def listing_price_to_ty(price_str, area_m2_str=None):
    """Convert a listing's price string ('15 tỷ', '6,4 tỷ', '1.250 tỷ',
    '980 triệu', 'Giá thỏa thuận') into a comparable value in tỷ đồng
    (billions of VND), or None if it can't be parsed as a number (e.g.
    negotiable price with no figure).

    A price shaped like "X.YYY" (a dot followed by exactly 3 digits, no
    comma) is genuinely ambiguous - it could be a decimal (e.g. "2.500
    tỷ" commonly means 2.5 tỷ, three decimal places as a formatting
    convention) or a thousands separator (e.g. a hotel listing at
    "1.250 tỷ" can genuinely mean 1,250 tỷ). There's no way to tell from
    the string alone, and guessing wrong in either direction has already
    caused real bugs here - once by inflating ordinary apartment prices
    1000x, once by letting a nine-figure central-Hanoi commercial
    property slip through a "< 2 tỷ" filter as if it were a small
    apartment. So when area_m2_str is available, this instead computes
    price/m² under both interpretations and picks whichever lands in a
    plausible range for Hanoi real estate - a 476m² central street-front
    property at "1.25 tỷ" implies ~2.6 triệu/m², far below what land
    costs anywhere in Hanoi, while "1,250 tỷ" implies ~2,626 triệu/m²,
    right in line with what prime addresses in that category actually
    go for.

    Caveat: this assumes area_m2_str is the land footprint, but for a
    multi-story street-front building it can instead be total floor
    area summed across every story (a 5-floor building on a modest
    ~95m² plot might list "476 m²" as combined floor area, not land) -
    there's no reliable way to tell which from the string alone. So this
    is a real heuristic with a real blind spot, not a solved
    disambiguation. Given that, if both interpretations land in the
    plausible range, or area isn't available, or neither is plausible,
    this returns None (skip the listing) rather than guessing - two
    different guessing strategies have both caused real, user-visible
    mistakes here already (inflating ordinary prices 1000x, and letting
    a nine-figure central-Hanoi property through a "< 2 tỷ" filter), so
    when genuinely unresolved, the listing is left out rather than
    risking a third wrong guess.
    """
    if "thỏa thuận" in price_str.lower():
        return None
    m = re.search(r'([\d][\d.,]*)\s*(tỷ|triệu)', price_str, re.IGNORECASE)
    if not m:
        return None
    num_str, unit = m.group(1), m.group(2).lower()

    if "," in num_str or not re.fullmatch(r'\d{1,3}(\.\d{3})+', num_str):
        value = _vn_to_float(num_str)
        return value if unit == "tỷ" else value / 1000

    decimal_ty = float(num_str) if unit == "tỷ" else float(num_str) / 1000
    thousands_ty = float(num_str.replace(".", "")) if unit == "tỷ" else float(num_str.replace(".", "")) / 1000

    area = None
    if area_m2_str:
        try:
            area = _vn_to_float(area_m2_str)
        except ValueError:
            area = None

    if area and area > 0:
        lo, hi = PLAUSIBLE_PRICE_PER_M2_TRIEU
        plausible = [ty for ty in (decimal_ty, thousands_ty) if lo <= (ty * 1000) / area <= hi]
        if len(plausible) == 1:
            return plausible[0]

    return None

LISTING_LINK_RE = re.compile(
    r'\[(.*?Ảnh đại diện.*?)\]\((https://batdongsan\.com\.vn/[^\s\)]+)(?:\s+"([^"]*)")?\)',
    re.DOTALL,
)
LISTING_IMAGE_RE = re.compile(r'!\[Image \d+: Ảnh đại diện\]\((https://[^\s\)]+)\)')
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
        img_m = LISTING_IMAGE_RE.search(link_text)
        title = (title_attr or "").strip()
        if not title:
            # Fall back to the link text with embedded image markdown
            # stripped out, since raw link text otherwise starts with
            # "![Image 1: Ảnh đại diện](...)..." noise.
            title = LISTING_IMAGE_RE.sub("", link_text).strip()[:80].strip()
        out.append({
            "category": category_label,
            "district": district_label,
            "title": title,
            "price": pa_m.group(1).strip(),
            "area": pa_m.group(2).strip(),
            "ward": ward_m.group(1).strip() if ward_m else district_label,
            "url": url,
            "image": img_m.group(1) if img_m else None,
        })
    return out


# meta-og:image appears two different ways depending on which fetch path
# was used: as a YAML-frontmatter-style line in the reader-proxy's
# Markdown output ("meta-og:image: https://...", seen when inspecting
# these pages directly), or as a standard <meta property="og:image"
# content="..."> tag in raw HTML from a direct fetch. Listing detail
# pages set this per-listing (unlike the lazy-loaded card thumbnails on
# the category/district listing pages, which don't expose a real src),
# so this is the reliable way to get an actual photo for a listing.
OG_IMAGE_MD_RE = re.compile(r'meta-og:image:\s*(\S+)', re.IGNORECASE)
OG_IMAGE_HTML_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
    re.IGNORECASE,
)


def fetch_listing_image(url, label):
    """Best-effort fetch of a listing's og:image. Returns None on any
    failure rather than raising - a missing photo just means that card
    renders without one, which is fine for illustrative content.
    """
    try:
        content = fetch_page(url, label=label)
    except requests.RequestException as e:
        print(f"  [{label}] image fetch failed: {e}", file=sys.stderr)
        return None
    m = OG_IMAGE_MD_RE.search(content)
    if m:
        return m.group(1)
    m = OG_IMAGE_HTML_RE.search(content)
    if m:
        return m.group(1) or m.group(2)
    return None


def attach_listing_images(listings):
    """Mutates each listing dict in place, filling in an 'image' key for
    any listing that doesn't already have one. Most listings now get
    their image for free from extract_sample_listings (pulled directly
    out of the card's embedded photo tag, no extra request) - this is
    only a fallback fetch for the remainder, so it costs far fewer
    requests than before.
    """
    for l in listings:
        if l.get("image"):
            continue
        l["image"] = fetch_listing_image(l["url"], label=f"Image/{l['district']}")
    return listings


def render_sample_listings_html(listings):
    if not listings:
        return ""
    cards = "\n".join(
        f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{C_CARD};border:1px solid {C_RULE};border-radius:8px;overflow:hidden;margin-bottom:12px;">
    {f'<tr><td><img src="{escape(l["image"])}" width="100%" alt="" style="display:block;height:180px;object-fit:cover;" /></td></tr>' if l.get("image") else ""}
    <tr><td style="padding:14px 16px;">
      <span style="display:inline-block;background:{C_TAG_BG};color:{C_TAG_TEXT};font-size:10px;font-weight:bold;text-transform:uppercase;letter-spacing:0.5px;padding:3px 9px;border-radius:10px;">{escape(l['category'])}</span>
      <div style="font-family:{F_DISPLAY};font-size:15px;color:{C_INK};margin-top:9px;line-height:1.35;"><a href="{escape(l['url'])}" style="color:{C_INK};text-decoration:none;">{escape(l['title'])}</a></div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px;"><tr>
        <td style="font-family:{F_DISPLAY};font-size:16px;color:{C_INK};font-weight:bold;">{escape(l['price'])}</td>
        <td style="font-family:{F_BODY};font-size:12px;color:{C_MUTED};text-align:right;">{escape(l['area'])} m² · {escape(l['ward'])}</td>
      </tr></table>
    </td></tr>
    </table>"""
        for l in listings
    )
    title = "Nhà mẫu tham khảo"
    subtitle = "Một vài tin đăng thực tế lấy từ Batdongsan.com.vn, chỉ mang tính minh họa - không phải danh sách đầy đủ hay gợi ý mua."
    if LISTING_MAX_PRICE_TY is not None:
        title += f" (dưới {LISTING_MAX_PRICE_TY:g} tỷ)"
        subtitle += f" Chỉ hiển thị tin có giá ≤ {LISTING_MAX_PRICE_TY:g} tỷ - tin ghi 'giá thỏa thuận' (không có số) không được tính vào đây."
    eyebrow = section_eyebrow_html("Tin đăng thực tế", title, subtitle)
    return f"""
  {eyebrow}
  <div style="margin-top:12px;">{cards}</div>"""


def render_sample_listings_text(listings):
    if not listings:
        return ""
    header = "== NHA MAU THAM KHAO"
    if LISTING_MAX_PRICE_TY is not None:
        header += f" (duoi {LISTING_MAX_PRICE_TY:g} ty)"
    header += " =="
    lines = [header]
    for l in listings:
        lines.append(f"[{l['category']}] {l['title']} - {l['price']} - {l['area']} m2 - {l['ward']}")
        lines.append(f"  {l['url']}")
        if l.get("image"):
            lines.append(f"  Anh: {l['image']}")
    return "\n".join(lines)


def fetch_batdongsan_category(url_prefix, label):
    rows = []
    diagnosed = False
    for name, slug in DISTRICT_SLUGS:
        url = f"https://batdongsan.com.vn/{url_prefix}-{slug}"
        try:
            html = fetch_page(url, label=f"{label}/{slug}")
        except requests.RequestException as e:
            print(f"  [{label}/{slug}] fetch failed: {e}", file=sys.stderr)
            continue
        before = len(SAMPLE_LISTINGS)
        SAMPLE_LISTINGS.extend(extract_sample_listings(html, category_label=label, district_label=name, max_samples=LISTING_CANDIDATES_PER_DISTRICT))
        if len(SAMPLE_LISTINGS) == before and not diagnosed:
            # 0 listings found on this page - show what's actually there
            # instead of guessing at the format again. Only once per
            # category run (not per district) to keep the log readable.
            diagnosed = True
            marker_count = html.count("Ảnh đại diện")
            if marker_count == 0:
                print(f"  [{label}/{slug}] listings diagnostic: 'Ảnh đại diện' does not appear anywhere in the {len(html)}-byte response - the card format has likely changed. First 500 chars: {html[:500]!r}", file=sys.stderr)
            else:
                idx = html.find("Ảnh đại diện")
                print(f"  [{label}/{slug}] listings diagnostic: 'Ảnh đại diện' appears {marker_count}x but the card regex still didn't match. Snippet around first occurrence: {html[max(0,idx-50):idx+500]!r}", file=sys.stderr)
        text = norm(" ".join(_flatten_to_lines(html)))
        m = BDS_RANGE_RE.search(text) or BDS_RANGE_SHARED_UNIT_RE.search(text)
        if not m:
            print(f"  [{label}/{slug}] no price range found in response - skipping this district.", file=sys.stderr)
            continue
        rows.append({"area": name, "low": m.group(1), "high": m.group(2)})
    return rows


def render_range_table(rows):
    row_html = "\n".join(
        f"<tr style='{'background:' + C_ZEBRA + ';' if i % 2 else ''}'>"
        f"<td style='padding:10px 16px;font-family:{F_BODY};font-size:14px;color:{C_INK};border-bottom:1px solid {C_RULE_LIGHT};'>{escape(r['area'])}</td>"
        f"<td colspan='2' style='padding:10px 16px;font-family:{F_BODY};font-size:14px;color:{C_INK};text-align:right;border-bottom:1px solid {C_RULE_LIGHT};'>{escape(r['low'])} - {escape(r['high'])} triệu/m²</td></tr>"
        for i, r in enumerate(rows)
    )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:separate;width:100%;max-width:600px;background:{C_CARD};border:1px solid {C_RULE};border-radius:8px;overflow:hidden;">
    <thead><tr>
      <th style="padding:10px 16px;text-align:left;font-family:{F_BODY};font-size:11px;color:{C_MUTED};text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid {C_RULE};">Quận</th>
      <th colspan="2" style="padding:10px 16px;text-align:right;font-family:{F_BODY};font-size:11px;color:{C_MUTED};text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid {C_RULE};">Khoảng giá</th>
    </tr></thead>
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
    header_cells = "".join(
        f"<th style='padding:10px 16px;text-align:right;font-family:{F_BODY};font-size:11px;color:{C_MUTED};text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid {C_RULE};'>{size} m²</th>"
        for size in TYPICAL_SIZES_M2
    )
    body_rows = "\n".join(
        f"<tr style='{'background:' + C_ZEBRA + ';' if i % 2 else ''}'>"
        f"<td style='padding:10px 16px;font-family:{F_BODY};font-size:14px;color:{C_INK};border-bottom:1px solid {C_RULE_LIGHT};'>{escape(t['label'])}</td>"
        + "".join(
            f"<td style='padding:10px 16px;font-family:{F_BODY};font-size:13px;color:{C_INK};text-align:right;border-bottom:1px solid {C_RULE_LIGHT};'>{escape(t['sizes'][size])}</td>"
            for size in TYPICAL_SIZES_M2
        )
        + "</tr>"
        for i, t in enumerate(typical)
    )
    eyebrow = section_eyebrow_html(
        "Ước tính", "Giá nhà điển hình tại Hà Nội",
        "Tính từ giá trung bình/m² ở trên nhân với diện tích tham khảo - chỉ mang tính minh họa, không phải giá thực tế của một căn nhà cụ thể.",
    )
    return f"""
  {eyebrow}
  <table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:12px;border-collapse:separate;width:100%;max-width:600px;background:{C_CARD};border:1px solid {C_RULE};border-radius:8px;overflow:hidden;">
  <thead><tr>
    <th style="padding:10px 16px;text-align:left;font-family:{F_BODY};font-size:11px;color:{C_MUTED};text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid {C_RULE};">Loại hình</th>
    {header_cells}
  </tr></thead>
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
        sections = f"<p style='font-family:{F_BODY};color:{C_MUTED};padding:0 36px;'>Không có nguồn nào lấy được dữ liệu lần này. Kiểm tra log của workflow.</p>"
    else:
        sections = "\n".join(
            f"""
<tr><td style="padding:28px 36px 0;">
  {section_eyebrow_html(f"Nguồn {i}", escape(r['name']))}
</td></tr>
<tr><td style="padding:12px 36px 0;">
  {r['render_html'](r['rows'])}
</td></tr>"""
            for i, r in enumerate(results, start=1)
        )

    typical_section = render_typical_price_html(compute_typical_prices(results))
    typical_block = f'<tr><td style="padding:30px 36px 0;">{typical_section}</td></tr>' if typical_section else ""

    listings_section = render_sample_listings_html(listings)
    listings_block = f'<tr><td style="padding:30px 36px 0;">{listings_section}</td></tr>' if listings_section else ""

    district_count = len({row["area"] for r in results for row in r["rows"] if "area" in row})
    stat_strip = f"""
<tr><td style="padding:18px 36px 0;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
    <td width="33%" style="padding:14px 0;text-align:center;border-right:1px solid {C_RULE};">
      <div style="font-family:{F_DISPLAY};font-size:22px;color:{C_INK};">{len(SOURCES)}</div>
      <div style="font-family:{F_BODY};font-size:10px;color:{C_MUTED};text-transform:uppercase;letter-spacing:1px;">Nguồn dữ liệu</div>
    </td>
    <td width="33%" style="padding:14px 0;text-align:center;border-right:1px solid {C_RULE};">
      <div style="font-family:{F_DISPLAY};font-size:22px;color:{C_INK};">{len(results)}/{len(SOURCES)}</div>
      <div style="font-family:{F_BODY};font-size:10px;color:{C_MUTED};text-transform:uppercase;letter-spacing:1px;">Đã lấy được</div>
    </td>
    <td width="34%" style="padding:14px 0;text-align:center;">
      <div style="font-family:{F_DISPLAY};font-size:22px;color:{C_INK};">{district_count}</div>
      <div style="font-family:{F_BODY};font-size:10px;color:{C_MUTED};text-transform:uppercase;letter-spacing:1px;">Quận/huyện</div>
    </td>
  </tr></table>
</td></tr>"""

    return f"""\
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
</head>
<body style="margin:0;padding:0;background:{C_BG};font-family:{F_BODY};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{C_BG};">
<tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:640px;max-width:100%;background:{C_PANEL};border-radius:10px;overflow:hidden;">

<tr><td style="background:{C_HEADER};padding:32px 36px 26px;">
  <div style="font-family:{F_BODY};font-size:11px;letter-spacing:2px;color:{C_HEADER_MUTED};text-transform:uppercase;font-weight:bold;">Bản tin giá bất động sản</div>
  <div style="font-family:{F_DISPLAY};font-size:30px;color:{C_HEADER_TEXT};margin-top:6px;">Nhà đất Hà Nội</div>
  <div style="height:2px;background:{C_HEADER_MUTED};opacity:0.5;margin:14px 0 10px;width:64px;"></div>
  <div style="font-family:{F_BODY};font-size:12px;color:{C_HEADER_MUTED};">Cập nhật {escape(timestamp)}</div>
</td></tr>
{stat_strip}
{sections}
{typical_block}
{listings_block}

<tr><td style="padding:30px 36px 32px;">
  <div style="height:1px;background:{C_RULE};margin-bottom:16px;"></div>
  <div style="font-family:{F_BODY};font-size:11px;color:{C_MUTED_LIGHT};line-height:1.6;">
    Nguồn đã lấy được dữ liệu lần này: {escape(", ".join(r["name"] for r in results)) if results else "(không có)"} ·
    Đơn vị: triệu đồng/m² · Email tự động, chỉ mang tính tham khảo, không phải
    lời khuyên đầu tư.
  </div>
</td></tr>

</table>
</td></tr>
</table>
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

    candidates = SAMPLE_LISTINGS
    if LISTING_MAX_PRICE_TY is not None:
        priced = [(l, listing_price_to_ty(l["price"], l.get("area"))) for l in candidates]
        candidates = [l for l, ty in priced if ty is not None and ty <= LISTING_MAX_PRICE_TY]
        print(f"Filtered to {len(candidates)}/{len(SAMPLE_LISTINGS)} listing(s) at or under {LISTING_MAX_PRICE_TY:g} tỷ (listings with an unparseable or negotiable price are excluded, not assumed to pass).")

    max_listings = int(os.environ.get("MAX_SAMPLE_LISTINGS", "15"))
    if len(candidates) > max_listings:
        listings = random.sample(candidates, max_listings)
    else:
        listings = list(candidates)

    if listings and os.environ.get("FETCH_LISTING_IMAGES", "true").lower() == "true":
        attach_listing_images(listings)
        print(f"Fetched images for {sum(1 for l in listings if l.get('image'))}/{len(listings)} sample listing(s).")

    combined = {r["name"]: r["rows"] for r in results}
    price_hash = hash_data(combined)
    last_hash = load_last_hash()

    if results and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Gia nha dat Ha Noi - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(results, listings, timestamp)
    text_body = build_plain_text(results, listings, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w", encoding="utf-8") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w", encoding="utf-8") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w", encoding="utf-8") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w", encoding="utf-8") as f:
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
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if not meta.get("send", False):
        print("Nothing to send this run (no source returned data, or prices unchanged).")
        return

    with open(os.path.join(EMAIL_DIR, "subject.txt"), encoding="utf-8") as f:
        subject = f.read()
    with open(os.path.join(EMAIL_DIR, "body.html"), encoding="utf-8") as f:
        html_body = f.read()
    with open(os.path.join(EMAIL_DIR, "body.txt"), encoding="utf-8") as f:
        text_body = f.read()

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

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
