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
STORE_NAMES = ["Setec", "Tehnomarket", "Neptun", "Galerija"]
STORE_PREFIX = {
    "Setec": "setec",
    "Tehnomarket": "tehnomarket",
    "Neptun": "neptun",
    "Galerija": "galerija",
}


def parse_int_param(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def unique_non_empty_values(series: pd.Series) -> list[str]:
    vals = {str(v).strip() for v in series.fillna("")}
    vals.discard("")
    return sorted(vals)


def load_raw_data() -> pd.DataFrame:
    if not os.path.exists(RAW_CSV):
        return pd.DataFrame()
    df = pd.read_csv(RAW_CSV)
    df = df.fillna("")
    for col in ["price", "old_price", "screen_size"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def build_comparison_table(raw_df: pd.DataFrame) -> list[dict]:
    """Group by exact normalized_key — no fuzzy matching."""
    if raw_df.empty:
        return []

    groups: dict[str, dict] = {}
    for _, row in raw_df.iterrows():
        key = str(row.get("normalized_key", ""))
        brand = str(row.get("brand", ""))
        screen = int(row.get("screen_size", 0))

        if key not in groups:
            groups[key] = {
                "brand": brand,
                "screen_size": screen,
                "models": [],
                "stores": {},
            }

        groups[key]["models"].append(str(row.get("model", "")))
        if screen > 0 and groups[key]["screen_size"] == 0:
            groups[key]["screen_size"] = screen

        res = str(row.get("resolution", ""))
        if res and not groups[key].get("resolution"):
            groups[key]["resolution"] = res
        dt = str(row.get("display_tech", ""))
        if dt and not groups[key].get("display_tech"):
            groups[key]["display_tech"] = dt
        rr = int(row.get("refresh_rate", 0))
        if rr > 0 and not groups[key].get("refresh_rate"):
            groups[key]["refresh_rate"] = rr
        yr = int(row.get("year", 0))
        if yr > 0 and not groups[key].get("year"):
            groups[key]["year"] = yr

        store = str(row.get("store", ""))
        price = int(row.get("price", 0))
        url = str(row.get("url", ""))
        old_price = int(row.get("old_price", 0))

        if store not in groups[key]["stores"] or (price > 0 and (
            groups[key]["stores"][store]["price"] == 0 or price < groups[key]["stores"][store]["price"]
        )):
            groups[key]["stores"][store] = {"price": price, "old_price": old_price, "url": url}

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
            "resolution": gdata.get("resolution", ""),
            "display_tech": gdata.get("display_tech", ""),
            "refresh_rate": gdata.get("refresh_rate", 0),
            "year": gdata.get("year", 0),
            "setec_price": stores.get("Setec", {}).get("price", 0),
            "setec_old": stores.get("Setec", {}).get("old_price", 0),
            "setec_url": stores.get("Setec", {}).get("url", ""),
            "tehnomarket_price": stores.get("Tehnomarket", {}).get("price", 0),
            "tehnomarket_old": stores.get("Tehnomarket", {}).get("old_price", 0),
            "tehnomarket_url": stores.get("Tehnomarket", {}).get("url", ""),
            "neptun_price": stores.get("Neptun", {}).get("price", 0),
            "neptun_old": stores.get("Neptun", {}).get("old_price", 0),
            "neptun_url": stores.get("Neptun", {}).get("url", ""),
            "galerija_price": stores.get("Galerija", {}).get("price", 0),
            "galerija_old": stores.get("Galerija", {}).get("old_price", 0),
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


@app.route("/analytics")
def analytics():
    raw_df = load_raw_data()
    return render_template("analytics.html", has_data=len(raw_df) > 0)


@app.route("/api/analytics_data")
def api_analytics_data():
    raw_df = load_raw_data()
    if raw_df.empty:
        return jsonify({
            "filter_options": {
                "brands": [],
                "stores": STORE_NAMES,
                "techs": [],
                "resolutions": [],
                "years": [],
            },
            "active_filters": {},
            "price_by_store": {},
            "tech_counts": {},
            "resolution_counts": {},
            "brand_counts": {},
            "year_counts": {},
            "size_counts": {},
            "price_buckets": {"Under 10K": 0, "10-20K": 0, "20-40K": 0, "40-80K": 0, "80-150K": 0, "150K+": 0},
            "avg_price_by_tech": {},
            "avg_price_by_brand": {},
            "cheapest_per_tech": {},
            "biggest_savings": [],
            "store_offer_analytics": [],
            "best_average_store": {},
            "total_models": 0,
            "multi_store_count": 0,
            "filtered_listing_count": 0,
        })

    year_series = pd.to_numeric(raw_df.get("year", pd.Series(dtype=float)), errors="coerce").dropna()
    filter_options = {
        "brands": unique_non_empty_values(raw_df.get("brand", pd.Series(dtype=str))),
        "stores": STORE_NAMES,
        "techs": unique_non_empty_values(raw_df.get("display_tech", pd.Series(dtype=str))),
        "resolutions": unique_non_empty_values(raw_df.get("resolution", pd.Series(dtype=str))),
        "years": sorted({int(y) for y in year_series.tolist() if int(y) > 0}),
    }

    brand = request.args.get("brand", "").strip()
    store = request.args.get("store", "").strip()
    tech = request.args.get("tech", "").strip()
    resolution = request.args.get("resolution", "").strip()
    year_min = parse_int_param(request.args.get("year_min", ""), 0)
    year_max = parse_int_param(request.args.get("year_max", ""), 0)
    size_min = parse_int_param(request.args.get("size_min", ""), 0)
    size_max = parse_int_param(request.args.get("size_max", ""), 0)
    price_min = parse_int_param(request.args.get("price_min", ""), 0)
    price_max = parse_int_param(request.args.get("price_max", ""), 0)
    multi_store_only = request.args.get("multi_store_only", "").strip() == "1"
    offers_only = request.args.get("offers_only", "").strip() == "1"

    active_filters = {
        "brand": brand,
        "store": store,
        "tech": tech,
        "resolution": resolution,
        "year_min": year_min,
        "year_max": year_max,
        "size_min": size_min,
        "size_max": size_max,
        "price_min": price_min,
        "price_max": price_max,
        "multi_store_only": multi_store_only,
        "offers_only": offers_only,
    }

    filtered_raw = raw_df.copy()
    if brand:
        filtered_raw = filtered_raw[filtered_raw["brand"] == brand]
    if store:
        filtered_raw = filtered_raw[filtered_raw["store"] == store]
    if tech:
        filtered_raw = filtered_raw[filtered_raw["display_tech"] == tech]
    if resolution:
        filtered_raw = filtered_raw[filtered_raw["resolution"] == resolution]
    if year_min > 0:
        filtered_raw = filtered_raw[filtered_raw["year"] >= year_min]
    if year_max > 0:
        filtered_raw = filtered_raw[filtered_raw["year"] <= year_max]
    if size_min > 0:
        filtered_raw = filtered_raw[filtered_raw["screen_size"] >= size_min]
    if size_max > 0:
        filtered_raw = filtered_raw[filtered_raw["screen_size"] <= size_max]
    if price_min > 0:
        filtered_raw = filtered_raw[filtered_raw["price"] >= price_min]
    if price_max > 0:
        filtered_raw = filtered_raw[filtered_raw["price"] <= price_max]
    if offers_only:
        filtered_raw = filtered_raw[
            (filtered_raw["price"] > 0) &
            (filtered_raw["old_price"] > 0) &
            (filtered_raw["old_price"] > filtered_raw["price"])
        ]

    data = build_comparison_table(filtered_raw)
    if multi_store_only:
        data = [r for r in data if r["store_count"] >= 2]

    price_by_store = {}
    for store_name in STORE_NAMES:
        sub = filtered_raw[filtered_raw["store"] == store_name]
        priced_sub = sub[sub["price"] > 0]
        prices = priced_sub["price"].tolist()

        offers = sub[(sub["price"] > 0) & (sub["old_price"] > sub["price"])]
        discount_pcts = (
            ((offers["old_price"] - offers["price"]) * 100 / offers["old_price"]).tolist()
            if not offers.empty else []
        )

        price_by_store[store_name] = {
            "count": int(len(sub)),
            "prices": prices,
            "avg": int(priced_sub["price"].mean()) if prices else 0,
            "median": int(priced_sub["price"].median()) if prices else 0,
            "min": int(min(prices)) if prices else 0,
            "max": int(max(prices)) if prices else 0,
            "offer_count": int(len(offers)),
            "offer_share_pct": round((len(offers) * 100 / len(sub)), 2) if len(sub) > 0 else 0,
            "avg_discount_pct": round(float(sum(discount_pcts) / len(discount_pcts)), 2) if discount_pcts else 0,
            "max_discount_pct": round(float(max(discount_pcts)), 2) if discount_pcts else 0,
        }

    tech_counts = filtered_raw["display_tech"].value_counts().to_dict()
    resolution_counts = filtered_raw.get("resolution", pd.Series(dtype=str)).value_counts().to_dict()
    brand_counts = filtered_raw["brand"].value_counts().to_dict()

    year_counts = {}
    if "year" in filtered_raw.columns:
        ydf = filtered_raw[filtered_raw["year"] > 0]
        year_counts = ydf["year"].value_counts().sort_index().to_dict()
        year_counts = {str(k): int(v) for k, v in year_counts.items()}

    sdf = filtered_raw[filtered_raw["screen_size"] > 0]
    size_counts = sdf["screen_size"].value_counts().sort_index().to_dict()
    size_counts = {str(k): int(v) for k, v in size_counts.items()}

    price_buckets = {"Under 10K": 0, "10-20K": 0, "20-40K": 0, "40-80K": 0, "80-150K": 0, "150K+": 0}
    for p in filtered_raw[filtered_raw["price"] > 0]["price"]:
        if p < 10000:
            price_buckets["Under 10K"] += 1
        elif p < 20000:
            price_buckets["10-20K"] += 1
        elif p < 40000:
            price_buckets["20-40K"] += 1
        elif p < 80000:
            price_buckets["40-80K"] += 1
        elif p < 150000:
            price_buckets["80-150K"] += 1
        else:
            price_buckets["150K+"] += 1

    avg_price_by_tech = {}
    for tech_name, grp in filtered_raw[filtered_raw["price"] > 0].groupby("display_tech"):
        if tech_name:
            avg_price_by_tech[tech_name] = int(grp["price"].mean())

    avg_price_by_brand = {}
    for brand_name, grp in filtered_raw[filtered_raw["price"] > 0].groupby("brand"):
        if len(grp) >= 3:
            avg_price_by_brand[brand_name] = int(grp["price"].mean())

    cheapest_per_tech = {}
    for tech_name, grp in filtered_raw[filtered_raw["price"] > 0].groupby("display_tech"):
        if not tech_name:
            continue
        best = grp.loc[grp["price"].idxmin()]
        cheapest_per_tech[tech_name] = {
            "brand": best["brand"],
            "model": best["model"],
            "price": int(best["price"]),
            "store": best["store"],
            "size": int(best["screen_size"]),
        }

    multi_store = [r for r in data if r["store_count"] >= 2]
    biggest_savings = []
    for r in multi_store:
        store_prices = {}
        for store_name, prefix in STORE_PREFIX.items():
            p = r.get(f"{prefix}_price", 0)
            if p and p > 0:
                store_prices[store_name] = p
        if len(store_prices) >= 2:
            prices = list(store_prices.values())
            diff = max(prices) - min(prices)
            pct = round(diff * 100 / max(prices))
            if diff > 1000:
                biggest_savings.append({
                    "brand": r["brand"],
                    "model": r["name"],
                    "diff": diff,
                    "pct": pct,
                    "cheapest": min(store_prices, key=store_prices.get),
                    "cheapest_price": min(prices),
                    "most_expensive": max(store_prices, key=store_prices.get),
                    "expensive_price": max(prices),
                })
    biggest_savings.sort(key=lambda x: -x["diff"])

    store_comp = {
        store_name: {"comparable_models": 0, "win_points": 0.0, "price_index_total": 0.0, "delta_pct_total": 0.0}
        for store_name in STORE_NAMES
    }
    for row in multi_store:
        row_prices = {}
        for store_name, prefix in STORE_PREFIX.items():
            p = row.get(f"{prefix}_price", 0)
            if p and p > 0:
                row_prices[store_name] = p
        if len(row_prices) < 2:
            continue

        min_price = min(row_prices.values())
        cheapest_stores = [s for s, p in row_prices.items() if p == min_price]
        win_share = 1.0 / len(cheapest_stores)

        for store_name, price in row_prices.items():
            store_comp[store_name]["comparable_models"] += 1
            store_comp[store_name]["price_index_total"] += price / min_price
            store_comp[store_name]["delta_pct_total"] += (price - min_price) * 100 / min_price
        for store_name in cheapest_stores:
            store_comp[store_name]["win_points"] += win_share

    store_offer_analytics = []
    for store_name in STORE_NAMES:
        comp = store_comp[store_name]
        comp_models = comp["comparable_models"]
        avg_price_index = round(comp["price_index_total"] / comp_models, 4) if comp_models > 0 else 0
        avg_vs_cheapest = round(comp["delta_pct_total"] / comp_models, 2) if comp_models > 0 else 0
        win_rate = round(comp["win_points"] * 100 / comp_models, 2) if comp_models > 0 else 0

        store_offer_analytics.append({
            "store": store_name,
            "listings": price_by_store[store_name]["count"],
            "comparable_models": comp_models,
            "win_points": round(comp["win_points"], 2),
            "win_rate_pct": win_rate,
            "avg_price_index": avg_price_index,
            "avg_vs_cheapest_pct": avg_vs_cheapest,
            "avg_listed_price": price_by_store[store_name]["avg"],
            "offer_count": price_by_store[store_name]["offer_count"],
            "offer_share_pct": price_by_store[store_name]["offer_share_pct"],
            "avg_discount_pct": price_by_store[store_name]["avg_discount_pct"],
        })

    store_offer_analytics.sort(
        key=lambda x: (
            x["avg_price_index"] if x["avg_price_index"] > 0 else 9999,
            -x["win_rate_pct"],
            -x["offer_share_pct"],
        )
    )
    best_average_store = next(
        (entry for entry in store_offer_analytics if entry["comparable_models"] > 0),
        {},
    )

    return jsonify({
        "filter_options": filter_options,
        "active_filters": active_filters,
        "price_by_store": price_by_store,
        "tech_counts": tech_counts,
        "resolution_counts": resolution_counts,
        "brand_counts": brand_counts,
        "year_counts": year_counts,
        "size_counts": size_counts,
        "price_buckets": price_buckets,
        "avg_price_by_tech": avg_price_by_tech,
        "avg_price_by_brand": avg_price_by_brand,
        "cheapest_per_tech": cheapest_per_tech,
        "biggest_savings": biggest_savings[:20],
        "store_offer_analytics": store_offer_analytics,
        "best_average_store": best_average_store,
        "total_models": len(data),
        "multi_store_count": len(multi_store),
        "filtered_listing_count": int(len(filtered_raw)),
    })


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
