#!/usr/bin/env python3
"""
Flask web UI for the TV Price Comparator.
Loads scraped data from CSV and provides search/filter/compare functionality.
"""

import os
import subprocess
import sys
import threading

import pandas as pd
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

DATA_LOCK = threading.Lock()
RAW_CSV = "tv_all_raw.csv"
COMPARISON_CSV = "tv_comparison.csv"


def load_raw_data() -> pd.DataFrame:
    if not os.path.exists(RAW_CSV):
        return pd.DataFrame()
    df = pd.read_csv(RAW_CSV)
    df = df.fillna("")
    for col in ["price", "old_price", "screen_size"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def load_comparison_data() -> pd.DataFrame:
    if not os.path.exists(COMPARISON_CSV):
        return pd.DataFrame()
    df = pd.read_csv(COMPARISON_CSV)
    df = df.fillna("")
    return df


def build_comparison_table(raw_df: pd.DataFrame) -> list[dict]:
    """Build a list of dicts for the comparison view from raw data."""
    if raw_df.empty:
        return []

    from thefuzz import fuzz

    groups = {}
    for _, row in raw_df.iterrows():
        key = str(row.get("normalized_key", ""))
        brand = str(row.get("brand", ""))
        screen = int(row.get("screen_size", 0))

        matched_key = None
        best_score = 0
        for gk, gdata in groups.items():
            if gdata["brand"] != brand:
                continue
            score = fuzz.ratio(key, gk)
            if score > best_score:
                best_score = score
                matched_key = gk

        if matched_key and best_score >= 80:
            gk = matched_key
        else:
            gk = key
            groups[gk] = {
                "brand": brand,
                "screen_size": screen,
                "models": [],
                "stores": {},
            }

        groups[gk]["models"].append(str(row.get("model", "")))
        if screen > 0 and groups[gk]["screen_size"] == 0:
            groups[gk]["screen_size"] = screen
        store = str(row.get("store", ""))
        price = int(row.get("price", 0))
        url = str(row.get("url", ""))

        if store not in groups[gk]["stores"] or (price > 0 and (
            groups[gk]["stores"][store]["price"] == 0 or price < groups[gk]["stores"][store]["price"]
        )):
            groups[gk]["stores"][store] = {"price": price, "url": url}

    results = []
    for gk, gdata in groups.items():
        model = max(gdata["models"], key=len) if gdata["models"] else ""
        stores = gdata["stores"]
        prices = {s: d["price"] for s, d in stores.items() if d["price"] > 0}
        min_price = min(prices.values()) if prices else 0
        best_store = min(prices, key=prices.get) if prices else ""

        results.append({
            "brand": gdata["brand"],
            "name": model,
            "screen_size": gdata["screen_size"],
            "setec_price": stores.get("Setec", {}).get("price", 0),
            "setec_url": stores.get("Setec", {}).get("url", ""),
            "tehnomarket_price": stores.get("Tehnomarket", {}).get("price", 0),
            "tehnomarket_url": stores.get("Tehnomarket", {}).get("url", ""),
            "neptun_price": stores.get("Neptun", {}).get("price", 0),
            "neptun_url": stores.get("Neptun", {}).get("url", ""),
            "galerija_price": stores.get("Galerija", {}).get("price", 0),
            "galerija_url": stores.get("Galerija", {}).get("url", ""),
            "store_count": len(stores),
            "min_price": min_price,
            "best_store": best_store,
        })

    results.sort(key=lambda r: (r["brand"], -r["screen_size"], r["min_price"]))
    return results


scrape_status = {"running": False, "message": ""}


@app.route("/")
def index():
    raw_df = load_raw_data()
    data = build_comparison_table(raw_df)

    brands = sorted(set(r["brand"] for r in data if r["brand"]))
    sizes = sorted(set(r["screen_size"] for r in data if r["screen_size"] > 0))

    store_counts = {}
    for _, row in raw_df.iterrows():
        s = str(row.get("store", ""))
        store_counts[s] = store_counts.get(s, 0) + 1

    return render_template(
        "index.html",
        data=data,
        brands=brands,
        sizes=sizes,
        total=len(data),
        store_counts=store_counts,
        has_data=len(data) > 0,
        scrape_running=scrape_status["running"],
    )


@app.route("/api/search")
def api_search():
    raw_df = load_raw_data()
    data = build_comparison_table(raw_df)

    q = request.args.get("q", "").strip().lower()
    brand = request.args.get("brand", "").strip()
    size = request.args.get("size", "").strip()
    multi_only = request.args.get("multi_only", "").strip() == "1"
    sort_by = request.args.get("sort", "brand")

    filtered = data
    if q:
        filtered = [r for r in filtered if q in r["name"].lower() or q in r["brand"].lower()]
    if brand:
        filtered = [r for r in filtered if r["brand"] == brand]
    if size:
        try:
            size_int = int(size)
            filtered = [r for r in filtered if r["screen_size"] == size_int]
        except ValueError:
            pass
    if multi_only:
        filtered = [r for r in filtered if r["store_count"] >= 2]

    if sort_by == "price_asc":
        filtered.sort(key=lambda r: r["min_price"] if r["min_price"] > 0 else 999999)
    elif sort_by == "price_desc":
        filtered.sort(key=lambda r: -r["min_price"])
    elif sort_by == "size_asc":
        filtered.sort(key=lambda r: r["screen_size"] if r["screen_size"] > 0 else 999)
    elif sort_by == "size_desc":
        filtered.sort(key=lambda r: -r["screen_size"])
    elif sort_by == "stores":
        filtered.sort(key=lambda r: -r["store_count"])
    else:
        filtered.sort(key=lambda r: (r["brand"], -r["screen_size"]))

    return jsonify({"results": filtered, "count": len(filtered)})


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    if scrape_status["running"]:
        return jsonify({"status": "already_running", "message": "Scrape already in progress..."})

    def run_scrape():
        scrape_status["running"] = True
        scrape_status["message"] = "Scraping in progress..."
        try:
            env = os.environ.copy()
            env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
            result = subprocess.run(
                [sys.executable, "scraper.py"],
                capture_output=True, text=True, timeout=300,
                env=env,
            )
            if result.returncode == 0:
                scrape_status["message"] = "Scrape completed successfully!"
            else:
                scrape_status["message"] = f"Scrape failed: {result.stderr[-500:]}"
        except Exception as e:
            scrape_status["message"] = f"Scrape error: {e}"
        finally:
            scrape_status["running"] = False

    t = threading.Thread(target=run_scrape, daemon=True)
    t.start()
    return jsonify({"status": "started", "message": "Scraping started..."})


@app.route("/api/scrape_status")
def api_scrape_status():
    return jsonify({"running": scrape_status["running"], "message": scrape_status["message"]})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
