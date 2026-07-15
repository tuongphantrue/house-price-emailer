#!/usr/bin/env python3
"""
Hanoi House/Land Prices (by district) -> Email
(runs on GitHub Actions, no local computer needed)

Same shape as the gold-price-emailer / 9gag-meme-emailer this is modeled on:
fetches price data, then emails an HTML digest via Gmail SMTP. Runs in two
phases so the workflow can persist dedup state *between* them (see the
accompanying GitHub Actions workflow):

    python hanoi_house_price_emailer.py generate
        -> scrapes the price table, writes the composed email
           (subject/html/text) under ./email/, and updates the
           "last sent price" state file

    python hanoi_house_price_emailer.py send
        -> reads ./email/* and sends it via Gmail SMTP

SOURCE & AN IMPORTANT CAVEAT
-----------------------------
Vietnamese gold prices have a clean daily aggregator (giavang.org) with one
simple table per seller. Housing prices don't have a real equivalent: there
is no single site that publishes a clean, frequently-updated, per-district
"average price" table split out by property type (house vs. land vs.
apartment). Real estate portals like batdongsan.com.vn load their price
history behind JavaScript/an app, and most of what's public is prose
embedded in SEO articles, not structured data.

The one page found that IS server-rendered and reasonably table-shaped is
Mogi.vn's "Giá nhà đất" page:

    https://mogi.vn/gia-nha-dat

It lists an average price/m2 per district for Hanoi (and separately for
TPHCM) along with a month-over-month % change. This is a BLENDED "nha dat"
(house + land) figure, not split out by apartment vs. house vs. land -
Mogi doesn't publish that split anywhere in a clean scrapable format, so
this script does not attempt an apartment-only table. If you find a better
source for that split, this script's parsing function is the place to add
it (see parse_hanoi_table below).

Because prices here move much more slowly than gold, running this every 30
minutes will very often just re-send the same numbers (Mogi appears to
update monthly). Consider SEND_ONLY_ON_CHANGE=true (see below) if you'd
rather only get an email when the table actually changes.

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
     export SOURCE_URL="https://mogi.vn/gia-nha-dat"  # optional
     export STATE_FILE="state/last_price.json" # optional, dedup state file
     export ALLOW_INSECURE_SSL_FALLBACK="false" # optional, last-resort TLS bypass

NOTE ON SCRAPING
-----------------
Always worth checking the current robots.txt / terms of whatever site this
is pointed at before running it unattended long-term, e.g.:
https://mogi.vn/robots.txt

The page markup can change at any time - if `generate` reports 0 parsed
rows, open SOURCE_URL, inspect the Hanoi table, and update
parse_hanoi_table() below (it matches by district name, not by exact HTML
structure, which should make it reasonably resilient - but no guarantees).
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

# Every district/huyện of Hanoi, as labeled on mogi.vn (prefix + name).
# Matched against link text, so this list is what determines which rows on
# the page belong to Hanoi (the same page also lists TPHCM districts).
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


def parse_hanoi_table(html):
    """
    Parse the Hanoi section of mogi.vn/gia-nha-dat into a list of
    {area, price, change} rows.

    The page isn't cleanly separated in the DOM in an obvious way we can
    rely on long-term, so rather than depend on exact table/row structure,
    this matches by *district name* (HANOI_AREAS) against the page's
    flattened text, then looks at the text immediately following each
    match for a "NN triệu/m2" price and an optional "N,N%" change figure.
    This is more resilient to markup changes than a strict DOM walk, at
    the cost of being a bit more heuristic - if a run parses 0 rows, open
    SOURCE_URL and check the Hanoi section still looks like
    "Quận X | NN triệu/m2 | N% up/down arrow" per row.
    """
    def norm(s):
        # Collapse NBSP/whitespace and normalize to NFC so diacritics
        # compare equal regardless of which composed/decomposed form the
        # page happens to send them in.
        s = s.replace("\xa0", " ")
        s = re.sub(r"\s+", " ", s).strip()
        return unicodedata.normalize("NFC", s)

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [norm(l) for l in text.split("\n") if norm(l)]
    areas_norm = {norm(a): a for a in HANOI_AREAS}

    rows = []
    seen = set()
    for i, line in enumerate(lines):
        area = areas_norm.get(line)
        if not area or area in seen:
            continue
        # mogi.vn renders price + change together on the single line right
        # after the district name (e.g. "214 triệu/m2  4,9% ▲", or
        # "207 triệu/m2  —" when there's no change) - so only look at that
        # one line, never further, or a later row's % can bleed into this
        # one.
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
                # The arrow can land in its own text node (its own line)
                # right after the price+percent line.
                direction = "up" if lines[i + 2] == "▲" else "down"
        seen.add(area)
        rows.append({"area": area, "price": price, "change": change, "direction": direction})
    return rows


def fetch_hanoi_prices():
    html = fetch_page(SOURCE_URL)
    return parse_hanoi_table(html)


def _change_html(change, direction):
    if not change:
        return "<span style='color:#999'>—</span>"
    color = "#1a7f37" if direction == "up" else ("#cf222e" if direction == "down" else "#666")
    arrow = "▲" if direction == "up" else ("▼" if direction == "down" else "")
    return f"<span style='color:{color}'>{escape(change)}% {arrow}</span>"


def build_html(rows, source_url, timestamp):
    if not rows:
        body = (
            "<p>Could not parse the Hanoi price table this run. "
            f"Check <a href='{escape(source_url)}'>{escape(source_url)}</a> directly.</p>"
        )
    else:
        row_html = "\n".join(
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'><strong>{escape(r['area'])}</strong></td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{escape(r['price'])} triệu/m²</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>{_change_html(r['change'], r['direction'])}</td>"
            f"</tr>"
            for r in rows
        )
        body = f"""
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

    return f"""\
<html>
<body style="margin:0; padding:20px; background:#f4f4f4; font-family:Arial,Helvetica,sans-serif;">
  <h1 style="color:#1a5fb4;">Giá nhà đất Hà Nội theo quận/huyện</h1>
  <p style="color:#555;">Cập nhật {escape(timestamp)}</p>
  {body}
  <p style="color:#999; font-size:12px; margin-top:20px;">
    Nguồn: <a href="{escape(source_url)}">{escape(source_url)}</a> ·
    Đây là giá "nhà đất" nói chung (nhà + đất, gộp), không tách riêng chung cư -
    hiện chưa có nguồn công khai, cập nhật thường xuyên và tách bạch theo loại
    hình bất động sản tương đương giá vàng ·
    Đơn vị: triệu đồng/m² · Email tự động, chỉ mang tính tham khảo, không phải
    lời khuyên đầu tư.
  </p>
</body>
</html>"""


def build_plain_text(rows, source_url, timestamp):
    lines = [f"Gia nha dat Ha Noi theo quan/huyen - cap nhat {timestamp}", ""]
    if not rows:
        lines.append("Could not parse the Hanoi price table this run.")
    else:
        for r in rows:
            change_str = f"{r['change']}%" if r["change"] else "—"
            lines.append(f"{r['area']}: {r['price']} trieu/m2 ({change_str} so voi thang truoc)")
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

    print(f"Fetching {SOURCE_URL} ...")
    try:
        rows = fetch_hanoi_prices()
    except requests.RequestException as e:
        print(f"Failed to fetch price page: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsed {len(rows)} Hanoi district row(s).")
    if not rows:
        print(
            "  0 rows parsed - the page markup may have changed. "
            f"Open {SOURCE_URL} and check parse_hanoi_table().",
            file=sys.stderr,
        )

    price_hash = hash_data(rows)
    last_hash = load_last_hash()

    if rows and SEND_ONLY_ON_CHANGE and price_hash == last_hash:
        print("Prices unchanged since last run and SEND_ONLY_ON_CHANGE=true - skipping email.")
        with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
            json.dump({"send": False}, f)
        return

    now, timestamp = resolve_timestamp()
    subject = f"Gia nha dat Ha Noi - {now.strftime('%d/%m/%Y %H:%M')}"
    html_body = build_html(rows, SOURCE_URL, timestamp)
    text_body = build_plain_text(rows, SOURCE_URL, timestamp)

    with open(os.path.join(EMAIL_DIR, "subject.txt"), "w") as f:
        f.write(subject)
    with open(os.path.join(EMAIL_DIR, "body.html"), "w") as f:
        f.write(html_body)
    with open(os.path.join(EMAIL_DIR, "body.txt"), "w") as f:
        f.write(text_body)
    with open(os.path.join(EMAIL_DIR, "meta.json"), "w") as f:
        json.dump({"send": True, "rows": len(rows)}, f)

    save_last_hash(price_hash)
    print(f"Generated email ({len(rows)} rows). Saved to ./{EMAIL_DIR}/")


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
