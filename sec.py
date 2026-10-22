"""Map tickers to permanent SEC company ids (CIK).

The SEC's ticker file is downloaded at most once a day and cached in
data/sec_company_tickers.json. The SEC requires a User-Agent with a contact
email, e.g. "Your Name you@example.com". It is read from the SEC_USER_AGENT
environment variable, else built from the email in "sec.contact_email" in
vnpy's vt_setting.json (editable in the vnpy GUI's global settings).
"""
import json
import os
import time
from pathlib import Path

import requests
from vnpy.trader.setting import SETTINGS

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
CACHE_PATH = Path(__file__).resolve().parent / "data" / "sec_company_tickers.json"
CACHE_MAX_AGE = 24 * 60 * 60
CONTACT_EMAIL = SETTINGS.get("sec.contact_email") or "admin@example.com"
USER_AGENT = os.environ.get("SEC_USER_AGENT") or f"vnpy_ctastrategydr screener {CONTACT_EMAIL}"


def load_cik_map() -> dict[str, tuple[int, str]]:
    """Return ticker -> (cik, company name). Empty if the SEC file is unavailable."""
    stale = not CACHE_PATH.exists() or time.time() - CACHE_PATH.stat().st_mtime > CACHE_MAX_AGE
    if stale:
        try:
            response = requests.get(TICKERS_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
            response.raise_for_status()
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(response.text)
        except requests.RequestException as error:
            # Fall back to an older cached copy, if any.
            print(f"SEC ticker file unavailable: {error}")
    if not CACHE_PATH.exists():
        return {}
    data = json.loads(CACHE_PATH.read_text())
    fields = data["fields"]
    cik_i, name_i, ticker_i = fields.index("cik"), fields.index("name"), fields.index("ticker")
    return {row[ticker_i]: (int(row[cik_i]), row[name_i]) for row in data["data"]}
