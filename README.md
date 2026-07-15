# Hanoi House/Land Price Emailer (runs on GitHub Actions, no local computer needed)

Emails you the average house/land price per m² for every district (quận)
and rural district (huyện) of Hanoi, sourced from Mogi.vn's price page,
automatically via GitHub's free scheduled-workflow runners.

## Important: read this before relying on it

Gold prices have a clean daily aggregator site with one simple table per
seller. **Hanoi housing prices don't have a real equivalent.** There's no
public site that publishes a clean, structured, frequently-updated table
split out by property type (house vs. apartment vs. land) the way
giavang.org does for gold sellers.

The one source found that's server-rendered and table-shaped is Mogi.vn's
[Giá nhà đất](https://mogi.vn/gia-nha-dat) page. It gives one **blended**
average price/m² per district (houses + land together), with a
month-over-month % change - not split into a separate "apartment" table.
Real per-district apartment breakdowns on other sites (batdongsan.com.vn,
Mogi's own per-district detail pages) are either loaded via JavaScript
after the page loads, or buried as prose inside SEO articles - neither is
something a plain HTTP scraper can reliably read. If you find a clean
source for that split, `parse_hanoi_table()` in the script is where to
wire it in.

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
  revoked, or mogi.vn changed its page markup (see below).
- If a run reports "Parsed 0 rows", the site's HTML structure probably
  changed. Open <https://mogi.vn/gia-nha-dat>, check the Hanoi section
  still lists each district's name followed by a "NN triệu/m2" price and a
  "N,N%" change, and adjust `parse_hanoi_table` in
  `hanoi_house_price_emailer.py` to match.
- Always worth checking the current `robots.txt` / terms before running
  this unattended long-term: <https://mogi.vn/robots.txt>

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
