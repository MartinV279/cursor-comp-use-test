#!/usr/bin/env python3
"""
TV Price Scraper & Comparator for Macedonian E-Commerce Stores.

Scrapes TVs from:
  - setec.mk
  - tehnomarket.com.mk
  - neptun.mk
  - galerija.com.mk

Normalizes product names and compares prices across stores.
"""

import re
import time
import logging
import sys
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

KNOWN_BRANDS = [
    "SAMSUNG", "LG", "SONY", "PHILIPS", "HISENSE", "TCL",
    "TELEFUNKEN", "VIVAX", "VOX", "TESLA", "PANASONIC", "TOSHIBA",
    "SHARP", "JVC", "THOMSON", "NEO", "FAVORIT", "ZEUS", "HOOBART",
    "FOX", "GRUNDIG", "BEKO", "DAEWOO", "SKYWORTH", "XIAOMI",
    "REALME", "NOKIA", "MOTOROLA", "CHIQ", "FUEGO", "HAIER",
    "BAUTECH", "HORIZON",
]

DESCRIPTOR_WORDS = re.compile(
    r"\b("
    r"телевизор|televizor|tv|smart|android\s*\d*|google|led|oled|qled|neo\s*qled|"
    r"mini\s*led|direct\s*led|qd[- ]?mini\s*led|nanocell|uhd|fhd|hd|full\s*hd|"
    r"hd\s*ready|4k|8k|4к|lcd|frameless|ambilight|ready|dvb[- ]?t2?|hdr\s*\d*|"
    r"webos|tizen\s*os|tizen|vidaa|titan\s*os|crystal|ultra\s*hd|ultra|"
    r"smart\s*tv|лед|андроид|"
    r"wi-?fi|bluetooth|atsc|ntsc|curved|flat|slim|ultra\s*slim|"
    r"premium|pro\s*display|interactive\s*display|"
    r"neo(?=\s+qled|\s+smart|\s*$)|"
    r"series|a\s+series|"
    r"\d{3,5}\s*hz"
    r")\b",
    re.I,
)

SIZE_PATTERN = re.compile(
    r"""
    ,?\s*\d{2,3}\s*[""\u2033]\s*              |  # 55"
    ,?\s*\d{2,3}\s*'{2}\s*                     |  # 55''
    ,?\s*\(\s*\d+\.?\d*\s*c?m\s*\)            |  # (139cm) or (215.9cm)
    \b\d{2,3}\s*(?:inch|инч)\b                 |  # 55 inch
    \b\d{2,3}\s*[""\u2033]\s*\(\d+\.?\d*c?m\)    # 55"(139cm)
    """,
    re.I | re.X,
)


@dataclass
class TV:
    name: str
    brand: str
    model: str
    price: int  # in MKD (ден.)
    old_price: int = 0
    store: str = ""
    url: str = ""
    screen_size: int = 0  # inches
    normalized_key: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_price(text: str) -> int:
    """Parse a Macedonian price string like '24.999' or '24,999' to int.

    Handles formats:
      - '24.999'       → 24999  (dot as thousands separator)
      - '24,999'       → 24999  (comma as thousands separator)
      - '36,490.00'    → 36490  (WooCommerce format with decimals)
      - '36,490.00 ден' → 36490
    """
    text = re.sub(r"[^\d,.]", "", text.strip())
    if re.match(r"^\d{1,3}(,\d{3})*\.\d{2}$", text):
        text = text.rsplit(".", 1)[0]
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else 0


def extract_screen_size(text: str) -> int:
    """Extract screen size in inches from a product name or model string."""
    m = re.search(r'(\d{2,3})\s*["\u201C\u201D\u2033]', text)
    if m:
        val = int(m.group(1))
        if 19 <= val <= 120:
            return val
    m = re.search(r"(\d{2,3})\s*'{2}", text)
    if m:
        val = int(m.group(1))
        if 19 <= val <= 120:
            return val
    m = re.search(r"(?:^|\s)(\d{2,3})\s*(?:inch|инч)", text, re.I)
    if m:
        return int(m.group(1))
    return 0


def infer_screen_size(model_code: str) -> int:
    """Try to infer screen size from a model code like '55PUS9010' or 'QE65QN90'."""
    m = re.match(r"^(?:UE|QE|KD|XR|K|LT)?-?(\d{2})(?=[A-Z])", model_code, re.I)
    if m:
        val = int(m.group(1))
        if 19 <= val <= 98:
            return val
    m = re.search(r"(?:^|\s)(\d{2})\s*[A-Z]", model_code)
    if m:
        val = int(m.group(1))
        if 24 <= val <= 98:
            return val
    return 0


def extract_brand(name: str) -> str:
    """Extract brand from product name. Scans for known brands anywhere."""
    upper = name.upper()
    for b in KNOWN_BRANDS:
        pattern = r"(?<![A-Z])" + re.escape(b) + r"(?![A-Z])"
        if re.search(pattern, upper):
            return b
    tokens = name.split()
    for t in tokens:
        if re.match(r"^[A-Z][A-Za-z]{2,}$", t) and t.upper() not in {
            "THE", "AND", "FOR", "PRO", "MAX", "PLUS", "ULTRA", "MINI",
            "SMART", "CRYSTAL", "FRAME", "QLED", "OLED", "NANO",
        }:
            return t.upper()
    return tokens[0].upper() if tokens else "UNKNOWN"


def extract_model_code(name: str, brand: str) -> str:
    """Extract the core model code from a product name.

    Strategy: find the brand in the string, take everything after it,
    strip all descriptive/marketing words and size info, then collapse
    to a clean model identifier.

    Examples:
      "4K UHD Smart FUEGO 85 ELU 720 GTV 85\"(215.9cm)" → "85ELU720GTV"
      "SAMSUNG QE-65QN90DATXXH QLED UHD 4K NEO SMART TV" → "QE65QN90DATXXH"
      "PHILIPS 55 PUS 9010 Ambilight" → "55PUS9010"
      "SONY K43S35B" → "K43S35B"
    """
    upper = name.upper()
    idx = upper.find(brand.upper())
    if idx != -1:
        after_brand = name[idx + len(brand):].strip()
    else:
        after_brand = name.strip()

    after_brand = re.sub(r"^[-–:\s]+", "", after_brand)

    after_brand = SIZE_PATTERN.sub(" ", after_brand)
    after_brand = DESCRIPTOR_WORDS.sub(" ", after_brand)

    after_brand = re.sub(r"\.\w{2,4}$", "", after_brand)  # .CEI, .CEII
    after_brand = re.sub(r"\s*/\s*\d{1,2}\b", "", after_brand)  # /12 suffix
    after_brand = re.sub(r",.*$", "", after_brand)  # trailing comma descriptions
    after_brand = re.sub(r"\(.*?\)", "", after_brand)  # parenthetical info
    after_brand = re.sub(r"\b(Серија|Series|Модел|Model)\b", "", after_brand, flags=re.I)
    after_brand = re.sub(r"\s{2,}", " ", after_brand).strip()
    after_brand = re.sub(r"^[-–:\s]+|[-–:\s]+$", "", after_brand)

    return after_brand


def model_to_key(model: str) -> str:
    """Collapse a model string to an alphanumeric key for matching.

    "QE-65QN90DATXXH" → "QE65QN90DATXXH"
    "85 ELU 720 GTV"  → "85ELU720GTV"
    "55 PUS 9010"     → "55PUS9010"
    """
    return re.sub(r"[^A-Z0-9]", "", model.upper())


def normalize_key(brand: str, model: str) -> str:
    """Build a normalized comparison key from brand + model code."""
    return f"{brand}_{model_to_key(model)}"


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

SETEC_BASE = (
    "https://setec.mk/category/televizori-55"
    "?sort=%25D0%259D%25D0%25B0%25D1%2598%25D0%25B5%25D0%25B2%25D1%2582%25D0%25B8%25D0%25BD%25D0%25BE"
    "&page={page}&minPrice=4995&maxPrice=427799"
)

TEHNOMARKET_BASE = "https://tehnomarket.com.mk/category/4335/televizori"

MIN_TV_PRICE = 5000

NOT_A_TV = re.compile(
    r"\b(tv\s*box|streaming\s*box|android\s*box|set[- ]?top|"
    r"floor\s*stand|wall\s*mount|bracket|remote\s*control|"
    r"cleaning\s*kit|screen\s*cleaner|projector\s*screen|"
    r"soundbar|sound\s*bar|headphone|earphone|"
    r"cable|adapter|hdmi\s*switch|splitter|extender)\b",
    re.I,
)


def scrape_setec(pw_browser) -> list[TV]:
    """Scrape all TVs from setec.mk using Playwright (JS-rendered).

    Setec has ~359 TVs. Uses the full category URL with sort/price
    filters and paginates through every page until empty.
    """
    log.info("Scraping setec.mk ...")
    tvs: list[TV] = []
    page_num = 1

    page = pw_browser.new_page()

    while True:
        url = SETEC_BASE.format(page=page_num)
        log.info(f"  setec.mk page {page_num} ...")
        try:
            page.goto(url, timeout=30000, wait_until="networkidle")
        except PlaywrightTimeout:
            log.warning(f"  Timeout on page {page_num}, retrying once...")
            try:
                page.goto(url, timeout=30000, wait_until="networkidle")
            except PlaywrightTimeout:
                log.warning(f"  Timeout again on page {page_num}, stopping.")
                break

        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        links = soup.find_all("a", href=re.compile(r"^/products/"))
        if not links:
            log.info(f"  No products on page {page_num}, done.")
            break

        for link in links:
            card = link.parent
            if not card:
                continue

            h3 = card.find("h3")
            name = h3.get_text(strip=True) if h3 else ""
            if not name:
                continue

            p_tag = card.find("p")
            brand_hint = p_tag.get_text(strip=True) if p_tag else ""

            card_text = card.get_text(separator=" ", strip=True)
            prices = re.findall(r"([\d,.]+)\s*ден", card_text)

            club_price = parse_price(prices[0]) if prices else 0
            regular_price = parse_price(prices[1]) if len(prices) > 1 else 0

            href = link.get("href", "")
            full_url = f"https://setec.mk{href}" if href.startswith("/") else href

            brand = extract_brand(brand_hint or name)
            model = extract_model_code(name, brand)
            size = extract_screen_size(name)
            if size == 0:
                size = extract_screen_size(model)
            if size == 0:
                size = infer_screen_size(model_to_key(model))

            tv = TV(
                name=name,
                brand=brand,
                model=model,
                price=club_price,
                old_price=regular_price,
                store="Setec",
                url=full_url,
                screen_size=size,
            )
            tv.normalized_key = normalize_key(tv.brand, tv.model)
            tvs.append(tv)

        page_num += 1

    page.close()
    log.info(f"  setec.mk: scraped {len(tvs)} TVs")
    return tvs


def scrape_tehnomarket() -> list[TV]:
    """Scrape all TVs from tehnomarket.com.mk (server-rendered HTML).

    Uses the main /category/4335/televizori URL which contains ALL TVs
    plus some accessories. Accessories are filtered out by price
    (real TVs cost ≥ 4000 ден).

    Captures both "Редовна Цена" (regular price) and "SMART цена"
    (discounted price).
    """
    log.info("Scraping tehnomarket.com.mk ...")
    tvs: list[TV] = []
    session = requests.Session()
    session.headers.update(HEADERS)

    page_num = 1
    while True:
        url = f"{TEHNOMARKET_BASE}?page={page_num}" if page_num > 1 else TEHNOMARKET_BASE
        log.info(f"  tehnomarket page {page_num} ...")

        try:
            resp = session.get(url, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"  Request error: {e}")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        products = soup.select("li.span4.product-fix")
        if not products:
            break

        for prod in products:
            name_el = prod.select_one(".product-name a")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            href = name_el.get("href", "")

            price_section = prod.select_one(".product-price")
            regular_price = 0
            smart_price = 0

            if price_section:
                nm_els = price_section.select(".nm")
                if nm_els:
                    regular_price = parse_price(nm_els[0].get_text())
                smart_el = price_section.select_one(".smart-products .nm")
                if smart_el:
                    smart_price = parse_price(smart_el.get_text())

            best_price = smart_price if smart_price > 0 else regular_price

            if best_price < MIN_TV_PRICE:
                continue
            if NOT_A_TV.search(name):
                continue

            brand = extract_brand(name)
            model = extract_model_code(name, brand)
            size = extract_screen_size(name)
            if size == 0:
                size = extract_screen_size(model)
            if size == 0:
                size = infer_screen_size(model_to_key(model))

            tv = TV(
                name=name,
                brand=brand,
                model=model,
                price=best_price,
                old_price=regular_price if smart_price > 0 else 0,
                store="Tehnomarket",
                url=href,
                screen_size=size,
            )
            tv.normalized_key = normalize_key(tv.brand, tv.model)
            tvs.append(tv)

        total_match = soup.find(string=re.compile(r"од (\d+) производи"))
        if total_match:
            total = int(re.search(r"од (\d+)", total_match).group(1))
            if page_num * 32 >= total:
                break
        else:
            if len(products) < 32:
                break

        page_num += 1
        time.sleep(0.3)

    log.info(f"  tehnomarket.com.mk: scraped {len(tvs)} TVs")
    return tvs


NEPTUN_BASE = "https://www.neptun.mk/televizori.nspx"


def scrape_neptun(pw_browser) -> list[TV]:
    """Scrape all TVs from neptun.mk using Playwright (JS-rendered prices).

    Uses /televizori.nspx which lists ALL TVs. Neptun repeats content
    after the last real page, so we stop when we see no new products.

    Captures both "Редовна цена" (regular) and "HaPPy цена" (discount).
    """
    log.info("Scraping neptun.mk ...")
    tvs: list[TV] = []
    page_num = 1
    seen_keys: set[str] = set()

    page = pw_browser.new_page()

    while True:
        url = f"{NEPTUN_BASE}?page={page_num}" if page_num > 1 else NEPTUN_BASE
        log.info(f"  neptun.mk page {page_num} ...")

        try:
            page.goto(url, timeout=30000, wait_until="networkidle")
        except PlaywrightTimeout:
            log.warning(f"  Timeout on page {page_num}, stopping.")
            break

        html = page.content()
        soup = BeautifulSoup(html, "lxml")

        cards = soup.find_all("div", class_="productCardBody")
        if not cards:
            log.info(f"  No cards on page {page_num}, done.")
            break

        new_on_page = 0
        for card in cards:
            title_el = card.find("h2")
            if not title_el:
                title_el = card.find(string=re.compile(r"Телевизор\s+\w+", re.I))
                if title_el:
                    name = title_el.strip()
                else:
                    continue
            else:
                name = title_el.get_text(strip=True)

            name = re.sub(r"^Телевизор\s+", "", name, flags=re.I).strip()

            link_el = card.find_parent("a") or card.find("a")
            href = ""
            if link_el:
                href = link_el.get("href", "")
                if href and not href.startswith("http"):
                    href = f"https://www.neptun.mk{href}"

            regular_price = 0
            happy_price = 0

            regular_spans = card.select(".regularPriceDisplay:not(.happyBgBox .regularPriceDisplay) .priceNum")
            if not regular_spans:
                regular_spans = card.select(".discountPrice .priceNum")
            if regular_spans:
                regular_price = parse_price(regular_spans[0].get_text())

            happy_box = card.select_one(".happyBgBox")
            if happy_box:
                happy_spans = happy_box.select(".priceNum")
                if happy_spans:
                    happy_price = parse_price(happy_spans[0].get_text())

            if happy_price == 0:
                happy_price = regular_price

            brand = extract_brand(name)
            model = extract_model_code(name, brand)
            size = extract_screen_size(name)
            if size == 0:
                size = extract_screen_size(model)
            if size == 0:
                size = infer_screen_size(model_to_key(model))

            tv = TV(
                name=name,
                brand=brand,
                model=model,
                price=happy_price,
                old_price=regular_price,
                store="Neptun",
                url=href,
                screen_size=size,
            )
            tv.normalized_key = normalize_key(tv.brand, tv.model)

            if tv.normalized_key in seen_keys:
                continue
            seen_keys.add(tv.normalized_key)
            tvs.append(tv)
            new_on_page += 1

        if new_on_page == 0:
            log.info(f"  No new products on page {page_num}, done.")
            break

        page_num += 1

    page.close()
    log.info(f"  neptun.mk: scraped {len(tvs)} unique TVs")
    return tvs


GALERIJA_BASE = "https://galerija.com.mk/product-category/televizori-i-domasno-kino/televizori"


def scrape_galerija() -> list[TV]:
    """Scrape all TVs from galerija.com.mk (WooCommerce, server-rendered).

    Captures both sale price (ins) and original price (del).
    Paginates until 404 or empty page.
    """
    log.info("Scraping galerija.com.mk ...")
    tvs: list[TV] = []
    session = requests.Session()
    session.headers.update(HEADERS)

    page_num = 1

    while True:
        url = (
            f"{GALERIJA_BASE}/page/{page_num}/"
            if page_num > 1
            else f"{GALERIJA_BASE}/"
        )
        log.info(f"  galerija.com.mk page {page_num} ...")

        try:
            resp = session.get(url, timeout=15)
            if resp.status_code == 404:
                break
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"  Request error: {e}")
            break

        soup = BeautifulSoup(resp.text, "lxml")
        products = soup.select("div.product-grid-item")
        if not products:
            break

        for prod in products:
            name_el = prod.select_one(".wd-entities-title a")
            if not name_el:
                continue
            name = name_el.get_text(strip=True)
            href = name_el.get("href", "")

            price_el = prod.select_one(".price")
            price = 0
            old_price = 0
            if price_el:
                ins_el = price_el.select_one("ins .woocommerce-Price-amount bdi")
                del_el = price_el.select_one("del .woocommerce-Price-amount bdi")
                regular_el = price_el.select_one(".woocommerce-Price-amount bdi")

                if ins_el:
                    price = parse_price(ins_el.get_text())
                elif regular_el:
                    price = parse_price(regular_el.get_text())

                if del_el:
                    old_price = parse_price(del_el.get_text())

            brand = extract_brand(name)
            model = extract_model_code(name, brand)
            size = extract_screen_size(name)
            if size == 0:
                size = extract_screen_size(model)
            if size == 0:
                size = infer_screen_size(model_to_key(model))

            tv = TV(
                name=name,
                brand=brand,
                model=model,
                price=price,
                old_price=old_price,
                store="Galerija",
                url=href,
                screen_size=size,
            )
            tv.normalized_key = normalize_key(tv.brand, tv.model)
            tvs.append(tv)

        page_num += 1
        time.sleep(0.5)

    log.info(f"  galerija.com.mk: scraped {len(tvs)} TVs")
    return tvs


# ---------------------------------------------------------------------------
# Matching & Comparison
# ---------------------------------------------------------------------------

def group_tvs(all_tvs: list[TV]) -> pd.DataFrame:
    """Group TVs by exact normalized_key.

    Each unique normalized_key becomes one row showing prices from
    whichever stores carry that exact model.  No fuzzy matching —
    models must have identical alphanumeric keys to be grouped.
    """
    groups: dict[str, list[TV]] = {}
    for tv in all_tvs:
        groups.setdefault(tv.normalized_key, []).append(tv)

    rows = []
    for key, group in groups.items():
        brand = group[0].brand
        sizes = [tv.screen_size for tv in group if tv.screen_size > 0]
        screen_size = max(set(sizes), key=sizes.count) if sizes else 0
        models = [tv.model for tv in group]
        representative_model = max(models, key=len) if models else ""

        store_prices: dict[str, int] = {}
        store_old: dict[str, int] = {}
        store_urls: dict[str, str] = {}
        for tv in group:
            if tv.store not in store_prices or (
                tv.price > 0 and (store_prices[tv.store] == 0 or tv.price < store_prices[tv.store])
            ):
                store_prices[tv.store] = tv.price
                store_old[tv.store] = tv.old_price
                store_urls[tv.store] = tv.url

        positive_prices = [p for p in store_prices.values() if p > 0]

        row = {
            "Brand": brand,
            "Model": representative_model,
            "Screen": f'{screen_size}"' if screen_size else "?",
            "Setec (ден)": store_prices.get("Setec", ""),
            "Tehnomarket (ден)": store_prices.get("Tehnomarket", ""),
            "Neptun (ден)": store_prices.get("Neptun", ""),
            "Galerija (ден)": store_prices.get("Galerija", ""),
            "Stores": len(store_prices),
            "Min Price": min(positive_prices) if positive_prices else 0,
            "Best Store": min(
                ((s, p) for s, p in store_prices.items() if p > 0),
                key=lambda x: x[1],
                default=("", 0),
            )[0],
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Brand", "Screen", "Min Price"])
    return df


def print_summary(all_tvs: list[TV], df: pd.DataFrame):
    """Print a summary of scraping results and price comparison."""
    print("\n" + "=" * 80)
    print("TV PRICE SCRAPER - RESULTS SUMMARY")
    print("=" * 80)

    store_counts = {}
    for tv in all_tvs:
        store_counts[tv.store] = store_counts.get(tv.store, 0) + 1

    print("\n📊 Scraped TV counts per store:")
    for store, count in sorted(store_counts.items()):
        print(f"   {store:15s}: {count:4d} TVs")
    print(f"   {'TOTAL':15s}: {len(all_tvs):4d} TVs")

    if df.empty:
        print("\nNo TVs found to compare.")
        return

    multi_store = df[df["Stores"] >= 2].copy()
    print(f"\n🔄 TVs found in multiple stores: {len(multi_store)}")

    if not multi_store.empty:
        print("\n" + "=" * 80)
        print("CROSS-STORE PRICE COMPARISON (TVs available in 2+ stores)")
        print("=" * 80)
        display_cols = [
            "Brand", "Model", "Screen",
            "Setec (ден)", "Tehnomarket (ден)", "Neptun (ден)", "Galerija (ден)",
            "Best Store",
        ]
        existing = [c for c in display_cols if c in multi_store.columns]
        print(multi_store[existing].to_string(index=False))

    print("\n" + "=" * 80)
    print("ALL SCRAPED TVs BY STORE")
    print("=" * 80)
    display_cols_all = [
        "Brand", "Model", "Screen",
        "Setec (ден)", "Tehnomarket (ден)", "Neptun (ден)", "Galerija (ден)",
        "Min Price", "Best Store",
    ]
    existing_all = [c for c in display_cols_all if c in df.columns]
    print(df[existing_all].head(80).to_string(index=False))
    if len(df) > 80:
        print(f"\n... and {len(df) - 80} more TVs (see tv_comparison.csv for full data)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    all_tvs: list[TV] = []

    tehno_tvs = scrape_tehnomarket()
    all_tvs.extend(tehno_tvs)

    galerija_tvs = scrape_galerija()
    all_tvs.extend(galerija_tvs)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        setec_tvs = scrape_setec(browser)
        all_tvs.extend(setec_tvs)

        neptun_tvs = scrape_neptun(browser)
        all_tvs.extend(neptun_tvs)

        browser.close()

    log.info(f"Total TVs scraped: {len(all_tvs)}")

    df = group_tvs(all_tvs)

    csv_path = "tv_comparison.csv"
    df.to_csv(csv_path, index=False)
    log.info(f"Saved full comparison to {csv_path}")

    raw_data = []
    for tv in all_tvs:
        raw_data.append({
            "store": tv.store,
            "brand": tv.brand,
            "name": tv.name,
            "model": tv.model,
            "screen_size": tv.screen_size,
            "price": tv.price,
            "old_price": tv.old_price,
            "url": tv.url,
            "normalized_key": tv.normalized_key,
        })
    raw_df = pd.DataFrame(raw_data)
    raw_csv = "tv_all_raw.csv"
    raw_df.to_csv(raw_csv, index=False)
    log.info(f"Saved raw data to {raw_csv}")

    print_summary(all_tvs, df)


if __name__ == "__main__":
    main()
