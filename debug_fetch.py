#!/usr/bin/env python3
"""
Diagnostic script - run this once to see what mogi.vn is actually sending
back to `requests` (as opposed to what a browser or another fetch tool
might see), so we can fix the parsing regexes in
hanoi_house_price_emailer.py against real data.

Usage:
    pip install requests beautifulsoup4 certifi
    python debug_fetch.py

Paste the full printed output back - it's designed to stay short and not
leak anything sensitive (no secrets/env vars are touched here).
"""
import re
import unicodedata

import certifi
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def norm(s):
    s = s.replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return unicodedata.normalize("NFC", s)


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=15, verify=certifi.where())
    return resp.status_code, resp.text


def check_district(name, url):
    print(f"\n=== {name} ({url}) ===")
    status, html = fetch(url)
    print(f"HTTP status: {status}")
    print(f"Response length: {len(html)} chars")

    soup = BeautifulSoup(html, "html.parser")
    text = norm(soup.get_text(" "))

    # does the raw text contain "căn hộ" anywhere at all?
    idxs = [m.start() for m in re.finditer("căn hộ", text, re.IGNORECASE)]
    print(f"Occurrences of 'căn hộ' in extracted text: {len(idxs)}")
    for i in idxs[:3]:
        snippet = text[max(0, i - 40):i + 80]
        print(f"  ...{snippet}...")

    # does it look like a bot-block / challenge page?
    lower = html.lower()
    for marker in ["captcha", "cloudflare", "access denied", "just a moment", "checking your browser"]:
        if marker in lower:
            print(f"  ! possible bot-block marker found: {marker!r}")


def check_blended():
    url = "https://mogi.vn/gia-nha-dat"
    print(f"\n=== BLENDED TABLE ({url}) ===")
    status, html = fetch(url)
    print(f"HTTP status: {status}")
    print(f"Response length: {len(html)} chars")

    soup = BeautifulSoup(html, "html.parser")
    text = norm(soup.get_text(" "))

    sample_areas = ["Quận Cầu Giấy", "Quận Ba Đình", "Quận Hoàn Kiếm"]
    for area in sample_areas:
        idx = text.find(area)
        if idx == -1:
            print(f"  '{area}' NOT found in extracted text at all")
        else:
            snippet = text[idx:idx + 120]
            print(f"  '{area}' found, followed by: ...{snippet}...")

    lower = html.lower()
    for marker in ["captcha", "cloudflare", "access denied", "just a moment", "checking your browser"]:
        if marker in lower:
            print(f"  ! possible bot-block marker found: {marker!r}")


if __name__ == "__main__":
    check_blended()
    check_district("Quận Cầu Giấy", "https://mogi.vn/gia-nha-dat-quan-cau-giay-qd290")
    check_district("Quận Ba Đình", "https://mogi.vn/gia-nha-dat-quan-ba-dinh-qd289")
    check_district("Quận Tây Hồ", "https://mogi.vn/gia-nha-dat-quan-tay-ho-qd296")
