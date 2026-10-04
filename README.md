# PEAD J-Curve Radar (auto-updating)

1. Create a new GitHub repo (public is simplest for free Pages) and push these files.
2. Settings > Pages > Source: "Deploy from a branch", branch `main`, folder `/docs`.
3. Actions tab > "PEAD scan" > Run workflow once to create `docs/data.json`.
4. Your page: `https://<user>.github.io/<repo>/`. It re-reads data every 5 minutes.

Schedule: hourly, about 9:35 am to 3:35 pm IST on weekdays, plus 5:00 pm. Change it in `.github/workflows/scan.yml`.

Edit `companies.json` to add stocks, correct result dates, set `bucket`, `entry`, `sl`.
A stock within 2% of its `entry` shows "NEAR ENTRY".

Notes: Yahoo (yfinance) is an unofficial, delayed source and can break or rate-limit. NSE often blocks
GitHub's servers, so the calendar check may fail; result dates then come only from `companies.json`.
GitHub disables scheduled workflows after 60 days of no repo activity, so re-enable if needed.
