import json
import time
import random
import re
import os
import pathlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import pandas as pd
from dateutil import parser as dateparse
#from IPython.display import display, HTML

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
NOW = datetime.now(timezone.utc)

# Toggle deep-link resolution (set to "false" in GitHub Actions to avoid rate-limits)
RESOLVE_DEEP_LINKS = os.getenv("RESOLVE_DEEP_LINKS", "true").lower() == "true"

_LEVEL_PATTERNS = [
    (re.compile(r"\bpostdoc(?:toral)?\b", re.I),                                              "Postdoctoral"),
    (re.compile(r"\bph\.?d\.?\b|\bdoctoral?\b", re.I),                                       "PhD"),
    (re.compile(r"\bmaster(?:s|'s)?\b|\bm\.?sc\.?\b|\bmba\b|\bpostgraduate\b", re.I),        "Masters"),
    (re.compile(r"\bbachelor(?:s|'s)?\b|\bundergraduate?\b|\bb\.?sc\.?\b", re.I),            "Bachelors"),
    (re.compile(r"\bshort\s+course\b|\btraining\b|\bcertificate\b|\bworkshop\b", re.I),      "Short Course"),
    (re.compile(r"\bresearch\s+fellowship\b|\bresearch\s+grant\b", re.I),                     "Research Fellowship"),
]

_FULL_FUND = re.compile(
    r"\bfully?\s*funded\b|\bfull\s+scholarship\b"
    r"|\bcovers?\s+(?:all|full|tuition|living|airfare)\b"
    r"|\ball\s+expenses?\s+(?:covered|paid|included)\b",
    re.I,
)
_PARTIAL_FUND = re.compile(
    r"\bpartial(?:ly)?\s*funded\b|\btuition\s+waiver\b|\bfee\s+waiver\b|\bstipend\s+only\b",
    re.I,
)

_ELIGIBILITY = [
    (re.compile(r"\bkenya(?:n)?\b", re.I),                                                   "Kenyan students"),
    (re.compile(r"\bafric(?:a|an)\b", re.I),                                                 "African students"),
    (re.compile(r"\bdeveloping\s+countr(?:y|ies)\b", re.I),                                  "Developing countries"),
    (re.compile(r"\binternat(?:ional|ionally)\b|\ball\s+nationalities\b|\bworldwide\b", re.I),"International"),
]


def infer_level(text):
    for pattern, label in _LEVEL_PATTERNS:
        if pattern.search(text):
            return label
    return "Multiple / Unspecified"


def infer_funding(text):
    if _FULL_FUND.search(text):
        return "Fully Funded"
    if _PARTIAL_FUND.search(text):
        return "Partial"
    return "Unknown"

def infer_eligible_countries(text):
    for pattern, label in _ELIGIBILITY:
        if pattern.search(text):
            return label
    return "International (check website)"

def enrich(row, description=""):
    text = f"{row.get('name', '')} {description}"
    row["level"]               = infer_level(text)
    row["funding_type"]        = infer_funding(text)
    row["eligible_countries"]  = infer_eligible_countries(text)
    return row
#rss sources
RSS_SOURCES = [
    {
        "name":    "Opportunity Desk",
        "url":     "https://opportunitydesk.org/feed/",
        "country": "Multiple",
    },
    {
        "name":    "Scholars4Dev",
        "url":     "https://www.scholars4dev.com/feed/",
        "country": "Multiple",
    },
    {
        "name":    "AfterSchoolAfrica - Australia Awards",
        "url":     "https://www.afterschoolafrica.com/tag/australia/feed/",
        "country": "Australia",
    },
    {
        "name":    "AfterSchoolAfrica - DAAD",
        "url":     "https://www.afterschoolafrica.com/tag/daad/feed/",
        "country": "Germany",
    },
    {
        "name":    "Erasmus Mundus",
        "url":     "https://www.eacea.ec.europa.eu/node/253/rss_en",
        "country": "Europe",
    },
    {
        "name":    "GlobalSouthOpportunities",
        "url":     "https://www.globalsouthopportunities.com/category/scholarships/feed/",
        "country": "Multiple",
    },
]
HTML_SOURCES = [
    {
        "name":    "AfterSchoolAfrica - USA",
        "url":     "https://www.afterschoolafrica.com/scholarship/by-country/usa/",
        "country": "USA",
    },
    {
        "name":    "AfterSchoolAfrica - UK",
        "url":     "https://www.afterschoolafrica.com/scholarship/by-country/uk/",
        "country": "UK",
    },
    {
        "name":    "AfterSchoolAfrica - China",
        "url":     "https://www.afterschoolafrica.com/scholarship/by-country/scholarship-in-china/",  # ← fixed
        "country": "China",
    },
    {
        "name":    "AfterSchoolAfrica - Japan",
        "url":     "https://www.afterschoolafrica.com/scholarship/by-country/scholarship-in-japan/",  # ← fixed
        "country": "Japan",
    },
    {
        "name":    "AfterSchoolAfrica - Canada",
        "url":     "https://www.afterschoolafrica.com/scholarship/by-country/scholarship-in-canada/",  # ← fixed
        "country": "Canada",
    },
    {
        "name":    "AfterSchoolAfrica - South Africa",
        "url":     "https://www.afterschoolafrica.com/tag/scholarship-in-south-africa/",  # ← tag URL, not by-country
        "country": "South Africa",
    },
    {
        "name":    "AfterSchoolAfrica - Italy",
        "url":     "https://www.afterschoolafrica.com/scholarship/by-country/scholarship-in-italy/",  # ← fixed
        "country": "Italy",
    },
]


MCF_URL = (
    "https://mastercardfdn.org/en/what-we-do/our-programs/"
    "mastercard-foundation-scholars-program/where-to-apply/"
)



# CELL 6 - Helpers

def get_session():
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=2,      # waits 2s, 4s, 8s between retries
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.mount("http://", HTTPAdapter(max_retries=retries))
    session.headers.update(HEADERS)
    return session

SESSION = get_session()

def get_soup(url, parser="xml"):
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        return BeautifulSoup(r.text, parser)
    except Exception as e:
        print(f"  [ERROR] {url} -> {e}")
        return None


_DEADLINE_RE = re.compile(
    r"(?:deadline|closing\s+date|due\s*date)\s*[:\-]?\s*([A-Za-z0-9 ,/\-.]+)",
    re.I,
)

def extract_deadline(text):
    """Try to parse a deadline date from free text. Returns ISO string or ''."""
    if not text:
        return ""
    plain = re.sub(r"<[^>]+>", " ", text)
    m = _DEADLINE_RE.search(plain)
    if not m:
        return ""
    try:
        return dateparse.parse(m.group(1).strip()[:40], fuzzy=True).strftime("%Y-%m-%d")
    except Exception:
        return ""


def deadline_status(iso_date):
    if not iso_date:
        return "Unknown"
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days = (dt - NOW).days
        if days < 0:   return "Closed"
        if days <= 14: return "Closing Soon"
        if days <= 30: return "Closing Mid"
        return "Open"
    except Exception:
        return "Unknown"


# Known aggregator domains whose article pages we should look through
# to find the real program URL.
_AGGREGATOR_DOMAINS = {
    "opportunitydesk.org",
    "scholars4dev.com",
    "afterschoolafrica.com",
    "eacea.ec.europa.eu",
}


# Any link pointing to these is skipped in both passes.
_SKIP_DOMAINS = {
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "youtube.com", "tiktok.com",
    "whatsapp.com", "t.me", "telegram.me", "telegram.org",
    "reddit.com", "pinterest.com",
    "google.com", "google.co.ke",
    "paypal.com", "bit.ly", "tinyurl.com",
    "yocket.com", "afterschoolafrica.com", "opportunitydesk.org",
    "scholars4dev.com", "eacea.ec.europa.eu",

}

# Anchor text patterns that strongly suggest an official program link.
_PROGRAM_LINK_RE = re.compile(
    r"\b(apply\s+(here|now)|official\s+(website|page|link)|"
    r"click\s+here\s+to\s+apply|programme?\s+(website|page)|"
    r"more\s+information|visit\s+(website|page)|learn\s+more)\b",
    re.I,
)

# Domains that are credible program hosts — used to filter pass 2.
# Extend this list as you encounter legitimate hosts.
_PROGRAM_DOMAINS = re.compile(
    r"(\.edu|\.ac\.|\.gov|\.org|daad\.de|chevening\.org|"
    r"commonwealthscholarships|mastercardfdn|scholarships\.gov\.au|"
    r"erasmusplus|eacea|fulbright|aga-khan|worldbank|afdb\.org|"
    r"britishcouncil|idrc\.ca|gates|rockefeller|ford|carnegie|"
    r"soros|macfound|hewlett|mellon|nuffield|wellcome|"
    r"un\.org|undp|unicef|unesco|unfccc|who\.int|ilo\.org|"
    r"ausaid|dfat\.gov\.au|giz\.de|usaid\.gov|dfid|fcdo\.gov\.uk)",
    re.I,
)


def fetch_program_url(article_url):
    """
    Visit an aggregator article page and return the real program/apply URL.
    Falls back to the article URL if nothing credible is found.

    Pass 1: anchor whose visible text matches apply/official language,
            pointing to any non-skipped external domain.
    Pass 2: first external link whose domain matches _PROGRAM_DOMAINS.
    Fallback: return the original article URL unchanged.
    """

    domain = urlparse(article_url).netloc.replace("www.", "")
    if domain not in _AGGREGATOR_DOMAINS:
        return article_url

    soup = get_soup(article_url, parser="lxml")
    if not soup:
        return article_url

    def is_skip(href):
        parsed = urlparse(href)
        d = parsed.netloc.replace("www.", "")
        return (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or d in _SKIP_DOMAINS
        )

    anchors = soup.find_all("a", href=True)

    # Pass 1: text strongly suggests an apply/official link
    for a in anchors:
        href = a["href"].strip()
        if is_skip(href):
            continue
        if _PROGRAM_LINK_RE.search(a.get_text(strip=True)):
            return href

    # Pass 2: first link whose domain looks like a credible program host
    for a in anchors:
        href = a["href"].strip()
        if is_skip(href):
            continue
        if _PROGRAM_DOMAINS.search(href):
            return href

    # Nothing credible found — keep the aggregator article URL
    return article_url


# CELL 7 - Scraper

def scrape_rss(source):
    rows = []
    soup = get_soup(source["url"])
    if not soup:
        return rows

    items = soup.select("item")
    if not items:
        print(f"  [{source['name']}] no items found")
        return rows

    for item in items:
        name_el = item.select_one("title")
        name = name_el.get_text(strip=True) if name_el else ""
        if not name:
            continue

        # <link> in RSS is a text node, not an attribute
        link_el = item.select_one("link")
        article_url = ""
        if link_el:
            article_url = (link_el.get_text(strip=True)
                           or str(link_el.next_sibling or "")).strip()

        desc_el = item.select_one("description") or item.select_one("encoded")
        description = desc_el.get_text(strip=True) if desc_el else ""

        pub_el = item.select_one("pubDate")
        pub_date = pub_el.get_text(strip=True) if pub_el else ""
        deadline = extract_deadline(description)

        st = deadline_status(deadline)
        if st == "Closed":
            continue

        # Resolve the real program URL from the aggregator article page
        if article_url and RESOLVE_DEEP_LINKS:
            program_url = fetch_program_url(article_url)
            time.sleep(random.uniform(1.0, 2.0))   # polite delay per article fetch
        else:
            program_url = article_url or source["url"]

        row = {
            "name":         name,
            "country":      source["country"],
            "source":       source["name"],
            "date":         pub_date,
            "deadline":     deadline,
            "status":       st,
            "article_url":  article_url,          # aggregator page (for reference)
            "link":         program_url,           # actual program/apply URL
        }
        row = enrich(row, description)
        rows.append(row)

    print(f"  [{source['name']}] {len(rows)} items")
    return rows

def scrape_html(source, max_pages=3):
    rows = []
    base_url = source["url"].rstrip("/") + "/"
    is_tag_url = "/tag/" in base_url  # tag archives paginate differently

    for page in range(1, max_pages + 1):
        if page == 1:
            url = base_url
        elif is_tag_url:
            url = f"{base_url}?paged={page}"   # /tag/.../?paged=2
        else:
            url = f"{base_url}page/{page}/"    # /scholarship/.../page/2/

        soup = get_soup(url, parser="lxml")
        if not soup:
            break

        title_links = soup.select("h2 a[href*='afterschoolafrica.com']")

        if not title_links:
            print(f"  [{source['name']}] page {page}: no items found, stopping")
            break

        page_rows = 0
        for title_el in title_links:
            name = title_el.get_text(strip=True)
            article_url = title_el.get("href", "").strip()
            if not name or not article_url:
                continue

            parent = title_el.find_parent(["div", "section", "article", "li"])
            description = parent.get_text(strip=True) if parent else ""

            date_el = parent.find("time") if parent else None
            pub_date = ""
            if date_el:
                pub_date = date_el.get("datetime", "") or date_el.get_text(strip=True)

            deadline = extract_deadline(description)
            st = deadline_status(deadline)
            if st == "Closed":
                continue

            if article_url and RESOLVE_DEEP_LINKS:
                program_url = fetch_program_url(article_url)
                time.sleep(random.uniform(1.0, 2.0))
            else:
                program_url = article_url or source["url"]

            row = {
                "name":        name,
                "country":     source["country"],
                "source":      source["name"],
                "date":        pub_date,
                "deadline":    deadline,
                "status":      st,
                "article_url": article_url,
                "link":        program_url,
            }
            row = enrich(row, description)
            rows.append(row)
            page_rows += 1

        print(f"  [{source['name']}] page {page}: {page_rows} items")

        if len(title_links) < 4:
            break

        time.sleep(random.uniform(1.5, 3.0))

    return rows
# CELL 8 - Deduplicate

def deduplicate(df):
    if df.empty:
        return df
    norm = (
        df["name"]
        .str.lower()
        .str.replace(r"[^\w\s]", "", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.strip()
    )
    return df[~norm.duplicated(keep="first")].reset_index(drop=True)


# CELL 9 - Run

all_rows = []

for src in RSS_SOURCES:
    print(f"\n> {src['name']}")
    all_rows.extend(scrape_rss(src))
    time.sleep(random.uniform(1.5, 3.0))

for src in HTML_SOURCES:
    print(f"\n> {src['name']}")
    all_rows.extend(scrape_html(src))
    time.sleep(random.uniform(1.5, 3.0))

df = pd.DataFrame(all_rows) if all_rows else pd.DataFrame()
before = len(df)
df = deduplicate(df)
print(f"\nDeduplication: {before} -> {len(df)} rows ({before - len(df)} removed)")
print(f"Total: {len(df)} scholarships")

if not df.empty:
    output_path = "scholarship/scholarships.json"
    pathlib.Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    data = df.to_dict(orient="records") 
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Saved {len(df)} scholarships to {output_path}")
else:
    print("No scholarships found.")
