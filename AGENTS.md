# AGENTS.md

## Cursor Cloud specific instructions

This is a Python-based TV price scraper that compares prices across 4 Macedonian e-commerce stores.

### Dependencies

Install Python dependencies: `pip3 install -r requirements.txt`

Playwright browser (Chromium) is required for JS-rendered sites (setec.mk, neptun.mk):
```
playwright install chromium
playwright install-deps chromium
```

### Running the scraper

```bash
export PATH="$HOME/.local/bin:$PATH"
python3 scraper.py
```

This will scrape all 4 stores, normalize names, compare prices, and output:
- Console summary with cross-store price comparison
- `tv_comparison.csv` — grouped comparison data
- `tv_all_raw.csv` — raw scraped data

### Site-specific notes

| Store | Method | Notes |
|-------|--------|-------|
| setec.mk | Playwright (JS-rendered Next.js) | ~359 TVs, paginated via `?page=N` |
| tehnomarket.com.mk | requests + BS4 (server-rendered) | Uses subcategories for actual TVs (not accessories) |
| neptun.mk | Playwright (AngularJS, dynamic prices) | Prices extracted via `.priceNum` spans in `.productCardBody` |
| galerija.com.mk | requests + BS4 (WooCommerce) | WooCommerce format prices (`36,490.00 ден`) |

### Gotchas

- Neptun prices use European number format with dots as thousand separators (e.g., `24.999` = 24,999 MKD).
- Galerija prices use WooCommerce format with commas as thousand separators and `.00` decimals.
- Tehnomarket's main "ТЕЛЕВИЗОРИ" category includes TV accessories; the scraper uses specific subcategories (19-43" LED, 46-85" LED, QLED) for actual TVs.
- Setec and Neptun require Playwright headless Chromium since their product data is JS-rendered.
