# Hanoi House/Land Price Emailer (runs on GitHub Actions, no local computer needed)

Emails you house/land prices for Hanoi by district (quận) and rural
district (huyện), pulled from up to 10 independent sources, automatically
via GitHub's free scheduled-workflow runners.

## Important: read this before relying on it

Gold prices have a clean daily aggregator site with one simple table per
seller. **Hanoi housing prices don't have a real equivalent.** There's no
public site that publishes a clean, structured, frequently-updated table
split out by property type (house vs. apartment vs. land) the way
giavang.org does for gold sellers. On top of that, some real estate sites
front their pages with Cloudflare-style protection that blocklists
GitHub Actions' shared runner IPs outright (confirmed via testing: a flat
`403 Forbidden` on every single request, regardless of headers - that's
an IP-range block, not a markup problem, and no amount of header-tweaking
fixes it).

Given that, this script hedges hard: it tries **10 independent sources**,
and treats every one of them as fully expendable - if a source errors,
gets blocked, or its page structure doesn't match what the parser
expects, it's silently left out of that run's email. No error
placeholders, no partial-failure noise - just whatever sources actually
came through, each in its own clearly labeled section, with a footer
listing which ones made it in. If literally none come through, no email
gets sent at all rather than sending an empty one.

1. **Mogi.vn** ([gia-nha-dat](https://mogi.vn/gia-nha-dat)) - one blended
   average price/m² per district (house + land together), with a
   month-over-month % change. One page covers every Hanoi district.
2-5. **Batdongsan.com.vn**, one source per property type - confirmed
   working via the reader-proxy workaround (see below) - each a min/max
   price/m² range, fetched one page per district (12 main urban
   districts; outlying huyện don't appear to have these page types):
   - Nhà mặt phố (street-front houses)
   - Chung cư (apartments) - the one genuinely separate apartment table
   - Nhà riêng (regular houses)
   - Đất nền (land)
6-10. **Nhatot.com, Alonhadat.com.vn, Cafeland.vn, Homedy.com, Dothi.net**
   - best-effort generic scans, added for extra redundancy. These are
     educated-guess URLs and a generic parser, not verified integrations -
     don't be surprised if some of these consistently come back empty,
     that's expected and harmless given the "just skip it" design. If you
     want one fixed for real, check that source's `[label]` diagnostic
     lines in the Action's log and share them back.

If you find a source and it's not wired in, `generic_district_scan()` in
the script is the easiest way to add one - it just needs a URL.

## Typical total price section

Below the per-district tables, the email adds a "Giá nhà điển hình tại
Hà Nội" (typical house prices) section - this isn't a new scrape, it's
arithmetic on the per-m² data already fetched: for each category (nhà mặt
phố, chung cư, nhà riêng, đất nền, and Mogi's blended average), it
averages that category's price/m² across whichever districts came back
this run, then multiplies by a few common sizes (30/50/70/100 m²) to give
an illustrative total price (e.g. "a typical 50m² chung cư costs about
2.25 - 4.5 tỷ"). It only shows categories that actually had data that run,
and it's explicitly labeled as an illustration, not the real price of any
specific property - actual prices depend heavily on exact location,
frontage, legal status, and condition. To change the reference sizes,
edit `TYPICAL_SIZES_M2` near the top of `hanoi_house_price_emailer.py`.

**If a run comes back with 0 rows for a source**, `fetch_page()` prints
diagnostics to the Action's log: the HTTP status code, response size, and
whether the response looks like a JS/anti-bot challenge page (Cloudflare
and similar) rather than real content.

As a workaround for IP-range blocks, `fetch_page()` tries a public
"reader" proxy (`r.jina.ai`) first - it fetches the page on its own
infrastructure (a different IP/fingerprint than GitHub's runners) and
returns the text, falling back to a direct fetch if that doesn't work
either. This is set via `USE_READER_PROXY=true` (the default). It's a
best-effort workaround, not a guarantee - the underlying sites could block
the proxy's IPs too, or change behavior at any time. If both the proxy
and the direct fetch keep
getting blocked, the realistic remaining options are: running this from a
non-cloud/residential IP instead of GitHub Actions (e.g. your own
computer via cron), or a paid scraping API service that maintains
residential IPs - both add real cost/complexity for what's meant to be a
simple free digest, worth weighing against just checking prices
manually.

Also worth knowing: unlike gold, this data does not update every 30
minutes - Mogi appears to refresh it roughly monthly. Running the workflow
every 30 minutes will very often just re-send the same numbers unless you
turn on `SEND_ONLY_ON_CHANGE` (see below).

## One-time setup (~5 minutes)

1. **Create a GitHub account** if you don't have one: <https://github.com/join>

2. **Create a new repository**
   - Click "+" (top right) -> "New repository"
   - Name it anything, e.g. `hanoi-house-price-emailer`
   - Set it to **Private** (recommended, keeps your workflow config private)
   - Click "Create repository"

3. **Upload these files** to the repo (drag-and-drop works fine via the
   GitHub web UI: "Add file" -> "Upload files"), keeping the folder structure:
   - `hanoi_house_price_emailer.py`
   - `requirements.txt`
   - `.github/workflows/send-house-price.yml`

4. **Create a Gmail App Password** (your normal Gmail password won't work):
   - Turn on 2-Step Verification: <https://myaccount.google.com/signinoptions/two-step-verification>
   - Then create an app password: <https://myaccount.google.com/apppasswords>
   - Choose "Mail" as the app, copy the 16-character password it gives you.

5. **Add your secrets to the repo** (this keeps your email/password out of the code):
   - In your repo: Settings -> Secrets and variables -> Actions -> "New repository secret"
   - Add three secrets:
     * `GMAIL_ADDRESS` = your Gmail address
     * `GMAIL_APP_PASSWORD` = the 16-character app password from step 4
     * `HOUSE_RECIPIENT` = the email address that should receive the price update

6. **Test it manually**
   - Go to the "Actions" tab in your repo
   - Click "Send Hanoi House Price" on the left
   - Click "Run workflow" -> "Run workflow" (green button)
   - Wait ~10-15 seconds, refresh, click into the run to see logs / confirm success
   - Check the recipient inbox for the email

That's it - from now on it runs automatically on the schedule below.

## Changing the schedule

Open `.github/workflows/send-house-price.yml` and edit this line:

```
- cron: "*/30 * * * *"
```

Cron format is `minute hour day month weekday`, always in **UTC**. Given
how slowly this data actually moves, a daily or weekly cadence is probably
more sensible than every 30 minutes:

- `0 1 * * *` -> once a day at 1am UTC (8am Vietnam, UTC+7)
- `0 1 * * 1` -> once a week, Monday 1am UTC
- `*/30 * * * *` -> every 30 minutes (current setting)

## Only emailing on price changes

Currently `SEND_ONLY_ON_CHANGE` is `"false"` in the workflow, so **every**
scheduled run sends an email with that moment's prices, whether or not
they've moved since last time. Given this data barely changes between
runs, you'll likely want:

```
SEND_ONLY_ON_CHANGE: "true"
```

in the "Generate email" step of the workflow. With that on, `generate`
compares the freshly scraped prices against a hash saved from the last
run - stored in `state/last_price.json` on a dedicated `house-price-state`
branch the workflow creates/updates automatically - and skips the email if
nothing changed.

## Notes

- GitHub Actions free tier includes 2,000 minutes/month for private repos.
- You can also trigger it manually anytime via the "Run workflow" button.
- If the run fails, check the Actions tab -> the failed run -> logs. Common
  causes: a secret is missing/misspelled, the Gmail app password was
  revoked, or a source's page markup/anti-bot behavior changed (see below).
- If a run reports 0 rows for a source, check that source's diagnostic
  lines in the log (HTTP status, response size, and a note if the response
  looks like a JS/anti-bot challenge page). Open the source URL yourself
  in a browser to compare against what the log shows, and adjust
  `parse_mogi` / `parse_batdongsan_district` in
  `hanoi_house_price_emailer.py` if the page structure changed.
- Always worth checking the current `robots.txt` / terms before running
  this unattended long-term:
  <https://mogi.vn/robots.txt> and <https://batdongsan.com.vn/robots.txt>

## Running locally instead

```
pip install -r requirements.txt
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
export HOUSE_RECIPIENT="you@gmail.com"
python hanoi_house_price_emailer.py generate
python hanoi_house_price_emailer.py send
```

Schedule it yourself with cron (`crontab -e`):

```
0 1 * * * cd /path/to/hanoi-house-price-emailer && /usr/bin/python3 hanoi_house_price_emailer.py generate && /usr/bin/python3 hanoi_house_price_emailer.py send >> house_emailer.log 2>&1
```
