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
  6-10. Nhatot.com, Alonhadat.com.vn, Cafeland.vn, Homedy.com, Dothi.net -
     best-effort generic scans. I have no network access to these sites
     from where this script was written, so these URLs and the generic
     parser are an educated guess, not a verified integration - expect
     some of these to come back empty and get skipped. That's fine, it's
     what the whole "skip silently" design is for. If you want one of
     these fixed for real, share the Action log lines for that source
     (look for its [label] diagnostic lines) and the parser can be
     adjusted to match what's actually there.

If you find a genuinely reliable apartment-only or house-only aggregator
for Hanoi, adding it as another SOURCES entry is the way to go - see
generic_district_scan() for the easiest way to wire one in.

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


def fetch_page(url, label=""):
    """GET a page's content, printing diagnostics (status code, response
    length, anti-bot-challenge detection) instead of a bare failure.
    Tries a public reader proxy first (different infrastructure than
    GitHub's runners - relevant since a flat 403 regardless of headers is
    an IP-range block, not a markup problem), falling back to a direct
    fetch.
    """
    if USE_READER_PROXY:
        proxied_url = READER_PROXY_PREFIX + url
        try:
            resp = requests.get(proxied_url, headers={"Accept": "text/plain"}, timeout=25)
            print(f"  [{label}] reader-proxy GET {proxied_url} -> HTTP {resp.status_code}, {len(resp.text)} bytes", file=sys.stderr)
            if resp.status_code == 200 and not _looks_like_challenge(resp.text):
                return resp.text
            print(f"  [{label}] reader proxy didn't return usable content - falling back to a direct fetch.", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  [{label}] reader proxy fetch failed ({e}) - falling back to a direct fetch.", file=sys.stderr)

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
# generic catch-all for "some price near a district name", used by the
# best-effort generic sources - either a single figure or a range.
GENERIC_PRICE_RE = re.compile(
    r"[\d][\d.,]*(?:\s*-\s*[\d][\d.,]*)?\s*(?:triệu|tr)(?:\s*đồng)?\s*/\s*m2?²?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Source: Mogi.vn - one page, every Hanoi district, blended avg price/m2
# + month-over-month % change.
# ---------------------------------------------------------------------------

MOGI_URL = os.environ.get("MOGI_URL", "https://mogi.vn/gia-nha-dat")


def fetch_mogi():
    html = fetch_page(MOGI_URL, label="Mogi.vn")
    lines = _flatten_to_lines(html)
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

def fetch_batdongsan_category(url_prefix, label):
    rows = []
    for name, slug in DISTRICT_SLUGS:
        url = f"https://batdongsan.com.vn/{url_prefix}-{slug}"
        try:
            html = fetch_page(url, label=f"{label}/{slug}")
        except requests.RequestException as e:
            print(f"  [{label}/{slug}] fetch failed: {e}", file=sys.stderr)
            continue
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
# Best-effort generic sources: scan a Hanoi-wide page for any of our known
# district names, and grab whatever price-shaped text sits on the next
# line. These URLs/parsers are educated guesses, not verified
# integrations - expected to come back empty on some of these, which is
# fine, they're just skipped.
# ---------------------------------------------------------------------------

def generic_district_scan(url, label):
    html = fetch_page(url, label=label)
    lines = _flatten_to_lines(html)
    areas_norm = {norm(a): a for a in HANOI_AREAS}
    rows = []
    seen = set()
    for i, line in enumerate(lines):
        area = areas_norm.get(line)
        if not area or area in seen or i + 1 >= len(lines):
            continue
        m = GENERIC_PRICE_RE.search(lines[i + 1])
        if not m:
            continue
        seen.add(area)
        rows.append({"area": area, "price_text": m.group(0)})
    return rows


def render_freeform_table(rows):
    row_html = "\n".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['price_text'])}</td></tr>"
        for r in rows
    )
    return f"""
    <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;width:100%;max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:14px;">
    <thead><tr style="background:#f5f5f5;"><th style="padding:8px 12px;text-align:left;">Quận / Huyện</th><th style="padding:8px 12px;text-align:right;">Giá</th></tr></thead>
    <tbody>{row_html}</tbody>
    </table>"""


def render_freeform_text(rows):
    return "\n".join(f"{r['area']}: {r['price_text']}" for r in rows)


# ---------------------------------------------------------------------------
# Source roster - each is fully independent; a failure here just means
# that entry gets left out of the email (see cmd_generate).
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
        "name": "Nhatot.com",
        "fetch": lambda: generic_district_scan("https://www.nhatot.com/mua-ban-bat-dong-san-ha-noi", "Nhatot.com"),
        "render_html": render_freeform_table,
        "render_text": render_freeform_text,
    },
    {
        "name": "Alonhadat.com.vn",
        "fetch": lambda: generic_district_scan("https://alonhadat.com.vn/nha-dat/ha-noi.html", "Alonhadat.com.vn"),
        "render_html": render_freeform_table,
        "render_text": render_freeform_text,
    },
    {
        "name": "Cafeland.vn",
        "fetch": lambda: generic_district_scan("https://cafeland.vn/du-an/ha-noi/", "Cafeland.vn"),
        "render_html": render_freeform_table,
        "render_text": render_freeform_text,
    },
    {
        "name": "Homedy.com",
        "fetch": lambda: generic_district_scan("https://homedy.com/ban-nha-dat-ha-noi", "Homedy.com"),
        "render_html": render_freeform_table,
        "render_text": render_freeform_text,
    },
    {
        "name": "Dothi.net",
        "fetch": lambda: generic_district_scan("https://dothi.net/nha-dat-ban-ha-noi.htm", "Dothi.net"),
        "render_html": render_freeform_table,
        "render_text": render_freeform_text,
    },
]


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


def build_html(results, timestamp):
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
    return f"""\
<html>
<body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
  <h1 style="color:#1a5fb4;">Giá nhà đất Hà Nội theo quận/huyện</h1>
  <p style="color:#555;">Cập nhật {escape(timestamp)}</p>
  {sections}
  <p style="color:#999; font-size:12px; margin-top:20px;">
    Nguồn đã lấy được dữ liệu lần này: {escape(", ".join(r["name"] for r in results)) if results else "(không có)"} ·
    Đơn vị: triệu đồng/m² · Email tự động, chỉ mang tính tham khảo, không phải
    lời khuyên đầu tư.
  </p>
</body>
</html>"""


def build_plain_text(results, timestamp):
    lines = [f"Gia nha dat Ha Noi theo quan/huyen - cap nhat {timestamp}", ""]
    if not results:
        lines.append("No source returned data this run. Check the workflow logs.")
    else:
        for r in results:
            lines.append(f"== {r['name']} ==")
            lines.append(r["render_text"](r["rows"]))
            lines.append("")
    return "\n".join(lines)


def cmd_generate():
    if os.path.exists(EMAIL_DIR):
        for f in os.listdir(EMAIL_DIR):
            os.remove(os.path.join(EMAIL_DIR, f))
    os.makedirs(EMAIL_DIR, exist_ok=True)

    results = run_all_sources()
    print(f"\n{len(results)}/{len(SOURCES)} source(s) returned data this run.")

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
    html_body = build_html(results, timestamp)
    text_body = build_plain_text(results, timestamp)

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
