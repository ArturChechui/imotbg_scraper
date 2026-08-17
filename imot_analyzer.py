#!/usr/bin/env python3
"""
Imot.bg Scraper & Map Exporter CLI
"""

import argparse
import csv
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

# ==============================================================================
# CONFIGURATION PRESETS & REGIONAL METADATA
# ==============================================================================

REGION_CONFIGS = {
    "pernik": {
        "default_region": "област Перник",
        "fallback_coords": (42.6036, 23.0365),
    },
    "sofia": {
        "default_region": "област София",
        "fallback_coords": (42.6977, 23.3219),
    },
}

PROPERTY_PRESETS = {
    "land": {
        "positive_patterns": {
            r'\bупи\b': 20,
            r'\bв регулация\b': 20,
            r'\bурегулиран\b': 20,
            r'\bток\b': 12,
            r'\bвода\b': 12,
            r'\bканализация\b': 10,
            r'\bасфалт\b': 8,
            r'\bсонда\b': 6,
            r'\bдостъп\b': 5,
            r'\bравен\b': 5,
            r'\bпанорама\b': 3,
        },
        "negative_keywords": {
            "нерегулиран": -20,
            "земеделска земя": -15,
            "денивелация": -10,
            "проект": -5,
        },
        "unregulated_red_flags": [
            "не са упи", "не е упи", "извън регулация",
            "годни за вкарване в регулация", "възможност за регулация",
            "близо до регулация", "в близост до урегулиран",
            "в близост до регулация", "възможност за смяна на статута",
            "земеделска земя", "вид територия земеделска", "нтп ниви"
        ],
        "hard_reject_words": [
            "обезщетение", "об.обезщетение", "срещу обезщетение"
        ],
    },
    "houses": {
        "positive_patterns": {
            r'\bакт 16\b': 25,
            r'\bобзаведен\b': 15,
            r'\bнова\b': 12,
            r'\bреновиран\b': 12,
            r'\bгараж\b': 10,
            r'\bпаркомясто\b': 8,
            r'\bток\b': 10,
            r'\bвода\b': 10,
            r'\bканализация\b': 10,
            r'\bцелогодишен достъп\b': 12,
            r'\bасфалт\b': 8,
            r'\bкладенец\b': 5,
            r'\bсонда\b': 5,
            r'\bотопление\b': 5,
            r'\bпанорама\b': 4,
            r'\bдвор\b': 4,
        },
        "negative_keywords": {
            "гредоред": -20,
            "за ремонт": -15,
            "недовършен": -15,
            "груб строеж": -15,
            "панел": -10,
            "без ток": -20,
            "без вода": -20,
            "лош достъп": -15,
        },
        "unregulated_red_flags": [],
        "hard_reject_words": [],
    }
}

UI_NOISE_PATTERNS = [
    r'Виж\s+карта.*',
    r'област\s+София,?',
    r'област\s+Варна,?',
    r'област\s+Перник,?',
    r'област\s+Бургас,?',
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
}

GEO_HEADERS = {
    "User-Agent": "ImotPropertyGeocoder/1.0 (production_exporter)"
}

COORDINATE_CACHE = {}

# ==============================================================================
# SCRAPER ENGINE
# ==============================================================================

class ImotScraper:
    def __init__(self, base_url, preset_name, region_key, max_workers=6):
        self.base_url = base_url
        self.preset = PROPERTY_PRESETS[preset_name]
        self.region_info = REGION_CONFIGS.get(region_key, {
            "default_region": f"област {region_key.capitalize()}",
            "fallback_coords": (42.6977, 23.3219)
        })
        self.max_workers = max_workers

    def build_page_url(self, page_num):
        if "?" in self.base_url:
            path, query = self.base_url.split("?", 1)
            query = "?" + query
        else:
            path, query = self.base_url, ""

        path = re.sub(r'/p-\d+', '', path.rstrip('/'))
        return f"{path}/p-{page_num}{query}"

    def fetch_html(self, url):
        try:
            res = requests.get(url, headers=HEADERS, timeout=10)
            res.raise_for_status()
            return res.content.decode('cp1251', errors='ignore')
        except Exception as e:
            print(f"[!] Request error for {url}: {e}")
            return None

    def extract_listing_urls(self, search_html):
        soup = BeautifulSoup(search_html, 'html.parser')
        valid_links = set()
        black_list = ["investitsionen", "kompleks", "agenziya", "promo", "obiava-8113", "obiava-6578"]

        for a in soup.find_all('a', href=True):
            href = a['href']
            if ("obiava-" in href or "act=5&adv=" in href) and not any(bad in href for bad in black_list):
                if href.startswith("//"):
                    href = "https:" + href
                elif href.startswith("/"):
                    href = "https://www.imot.bg" + href
                elif href.startswith("./"):
                    href = "https://www.imot.bg/" + href[2:]
                elif not href.startswith("http"):
                    href = "https://www.imot.bg/pcgi/" + href
                valid_links.add(href)

        return list(valid_links)

    def extract_clean_location(self, soup):
        loc_node = soup.find('div', class_='location') or soup.find('span', class_='advLocation') or soup.find('span', class_='location')
        default_reg = self.region_info["default_region"]

        if not loc_node:
            return default_reg

        clean = loc_node.get_text(separator=' ', strip=True)
        for pattern in UI_NOISE_PATTERNS:
            clean = re.sub(pattern, '', clean, flags=re.IGNORECASE)

        clean = re.sub(r'\s+(?:м\.|местност|мах\.|махала|център)\s*[\'"]?[\w\s-]+[\'"]?', '', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\s+', ' ', clean).strip(' ,')

        if not clean:
            return default_reg

        clean = re.sub(r'\bс\.\s*', 'v. ', clean, flags=re.IGNORECASE)
        clean = re.sub(r'\bгр\.\s*', 't. ', clean, flags=re.IGNORECASE)

        return f"{clean}, {default_reg}"

    def parse_listing(self, url):
        html = self.fetch_html(url)
        if not html:
            return None

        soup = BeautifulSoup(html, 'html.parser')
        clean_loc = self.extract_clean_location(soup)
        loc_node = soup.find('div', class_='location') or soup.find('span', class_='advLocation') or soup.find('span', class_='location')
        raw_loc = loc_node.get_text(separator=' ', strip=True) if loc_node else clean_loc

        title = "N/A"
        title_node = soup.find('div', class_='adTitle') or soup.find('h1') or soup.find('span', class_='advTitle')
        if title_node:
            title = re.sub(r'\s+', ' ', title_node.get_text(separator=' ', strip=True))

        price = "N/A"
        price_node = soup.find('div', id='tmes2') or soup.find('span', id='advPrice') or soup.find('div', class_='price')
        if price_node:
            price = price_node.get_text(separator=' ', strip=True)
        else:
            m = re.search(r'(\d[\d\s]*\d\s*(?:EUR|BGN|лв\.|евро))', html, re.IGNORECASE)
            if m:
                price = m.group(1).strip()

        size = "N/A"
        size_match = re.search(r'(\d[\d\s]*\d?\s*(?:кв\.м|кв\. м|кв\.м\.|m²))', html, re.IGNORECASE)
        if size_match:
            size = size_match.group(1).strip()

        desc_node = soup.find('div', id='ad_description') or soup.find('td', class_='description') or soup.find('div', class_='text')
        if desc_node:
            description = desc_node.get_text(separator=' ', strip=True)
        else:
            text_tds = [td.get_text(separator=' ', strip=True) for td in soup.find_all('td') if len(td.get_text(strip=True)) > 80]
            description = " ".join(text_tds)

        item = {
            "url": url,
            "title": title,
            "location": clean_loc,
            "raw_location": raw_loc,
            "price": price,
            "size": size,
            "description": description
        }

        text_lower = f"{item['title']} {item['raw_location']} {item['description']}".lower()

        for bad_word in self.preset["hard_reject_words"]:
            if bad_word in text_lower:
                item['rejected'] = True
                item['reject_reason'] = f"Blacklisted term: '{bad_word}'"
                return item

        for fake_flag in self.preset["unregulated_red_flags"]:
            if fake_flag in text_lower:
                item['rejected'] = True
                item['reject_reason'] = f"Unregulated land indicator: '{fake_flag}'"
                return item

        item['rejected'] = False
        score = 0
        pos_matches, neg_matches = [], []

        for pattern, weight in self.preset["positive_patterns"].items():
            clean_name = pattern.replace(r'\b', '')
            if re.search(pattern, text_lower):
                score += weight
                pos_matches.append(f"+{clean_name}(+{weight})")

        for word, weight in self.preset["negative_keywords"].items():
            if word in text_lower:
                score += weight
                neg_matches.append(f"{word}({weight})")

        item['score'] = score
        item['pos_matches'] = pos_matches
        item['neg_matches'] = neg_matches
        return item

    def run(self):
        print(f"[+] Discovering listing URLs...")
        all_urls, seen_urls = [], set()
        page = 1

        while True:
            page_url = self.build_page_url(page)
            print(f"    Page {page}: {page_url}")
            html = self.fetch_html(page_url)
            if not html:
                break

            urls = self.extract_listing_urls(html)
            new_urls = [u for u in urls if u not in seen_urls]
            if not new_urls:
                break

            for u in new_urls:
                seen_urls.add(u)
                all_urls.append(u)

            page += 1
            time.sleep(1.0)

        print(f"\n[+] Processing {len(all_urls)} unique listings with {self.max_workers} threads...")
        valid_results, rejected_count, completed = [], 0, 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_map = {executor.submit(self.parse_listing, url): url for url in all_urls}
            for future in as_completed(future_map):
                completed += 1
                item = future.result()
                if not item or not item.get('description'):
                    continue

                if item.get('rejected'):
                    rejected_count += 1
                    print(f" [{completed}/{len(all_urls)}] 🚫 REJECTED: {item['reject_reason']}")
                else:
                    valid_results.append(item)
                    print(f" [{completed}/{len(all_urls)}] ✅ SCORED #{item['score']} | {item['title'][:40]}...")

        valid_results.sort(key=lambda x: x['score'], reverse=True)
        return valid_results

# ==============================================================================
# GEOCODING & MAP EXPORT ENGINE
# ==============================================================================

def clean_imot_location_string(raw_location_str, default_region):
    text = raw_location_str.replace(f", {default_region}", "").strip()
    text = re.sub(r'\s+(?:ул\.?|кв\.?|квартал|м-т|м\.|местност|пром\.?\s*зона|стопански\s+двор).*$', '', text, flags=re.IGNORECASE)

    village_match = re.search(r'\b(?:с\.|v\.)\s*([А-Яа-яA-Za-z\s-]+)$', text, re.IGNORECASE)
    if village_match:
        clean_v = village_match.group(1).strip()
        return f"v. {clean_v}, {default_region}"

    return f"{text}, {default_region}"

def get_village_coordinates(clean_location_str, default_region):
    cache_key = f"{clean_location_str}, {default_region}"
    if cache_key in COORDINATE_CACHE:
        return COORDINATE_CACHE[cache_key]

    clean_name = re.sub(r'^(?:v|t|с|гр|гара)\b\.?\s*', '', clean_location_str, flags=re.IGNORECASE)
    clean_name = clean_name.replace(f", {default_region}", "").strip(' ,')

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        'q': f"{clean_name}, {default_region}, Bulgaria",
        'layer': 'address',
        'featureType': 'settlement',
        'format': 'json',
        'addressdetails': 1,
        'limit': 5
    }

    try:
        print(f" [API Request] Geocoding: '{clean_name}'...")
        time.sleep(1.1)

        res = requests.get(url, params=params, headers=GEO_HEADERS, timeout=5)
        if res.status_code == 200:
            results = res.json()
            if results:
                for item in results:
                    i_type = item.get('type', '').lower()
                    i_class = item.get('class', '').lower()
                    if i_class == 'place' or i_type in ['village', 'town', 'city', 'hamlet', 'locality']:
                        coords = (float(item['lat']), float(item['lon']))
                        COORDINATE_CACHE[cache_key] = coords
                        return coords

                coords = (float(results[0]['lat']), float(results[0]['lon']))
                COORDINATE_CACHE[cache_key] = coords
                return coords

    except Exception as e:
        print(f"[!] Geocoding error for '{clean_name}': {e}")

    COORDINATE_CACHE[cache_key] = None
    return None

def apply_gps_jitter(lat, lon, index, total_items_in_group, radius=0.00045):
    if total_items_in_group <= 1:
        return lat, lon

    angle = index * (2 * math.pi / total_items_in_group)
    return round(lat + (radius * math.cos(angle)), 6), round(lon + (radius * math.sin(angle)), 6)

def export_map_csv(valid_results, output_csv, region_key):
    region_info = REGION_CONFIGS.get(region_key, {
        "default_region": f"област {region_key.capitalize()}",
        "fallback_coords": (42.6977, 23.3219)
    })
    default_reg = region_info["default_region"]
    fallback_coords = region_info["fallback_coords"]

    fieldnames = [
        "Score", "Latitude", "Longitude", "Location", "Price", "Size", "Title", 
        "Matched Criteria", "Penalties", "URL", "Description"
    ]

    prepared_items, location_counts = [], {}

    for item in valid_results:
        raw_loc = item['location']
        sanitized_loc = clean_imot_location_string(raw_loc, default_reg)
        prepared_items.append({"raw_item": item, "raw_loc": raw_loc, "sanitized_loc": sanitized_loc})

    print(f"\n[+] Geocoding {len(prepared_items)} items for map export...")
    for entry in prepared_items:
        coords = get_village_coordinates(entry["sanitized_loc"], default_reg)
        group_key = entry["sanitized_loc"] if coords is not None else "FALLBACK"
        entry["coords"] = coords
        entry["group_key"] = group_key
        location_counts[group_key] = location_counts.get(group_key, 0) + 1

    group_indices = {}
    with open(output_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for idx, entry in enumerate(prepared_items, 1):
            item = entry["raw_item"]
            g_key = entry["group_key"]
            raw_loc = entry["raw_loc"]

            if entry["coords"] is not None:
                b_lat, b_lon = entry["coords"]
                r = 0.00045
            else:
                b_lat, b_lon = fallback_coords
                r = 0.00100

            c_idx = group_indices.get(g_key, 0)
            jit_lat, jit_lon = apply_gps_jitter(b_lat, b_lon, c_idx, location_counts[g_key], radius=r)
            group_indices[g_key] = c_idx + 1

            writer.writerow({
                "Score": item.get('score', 0),
                "Latitude": jit_lat,
                "Longitude": jit_lon,
                "Location": f"{raw_loc}, Bulgaria",
                "Price": item.get('price', 'N/A'),
                "Size": item.get('size', 'N/A'),
                "Title": item.get('title', 'N/A'),
                "Matched Criteria": ", ".join(item.get('pos_matches', [])),
                "Penalties": ", ".join(item.get('neg_matches', [])),
                "URL": item.get('url', ''),
                "Description": item.get('description', '')[:250] + "..."
            })
            print(f" [{idx}/{len(prepared_items)}] {'GEOCODED' if g_key != 'FALLBACK' else 'FALLBACK'}: {raw_loc} -> ({jit_lat}, {jit_lon})")

    print(f" 📍 Saved Map CSV File: {output_csv}")

# ==============================================================================
# CLI INTERFACE
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Imot.bg Unified Scraper & Geocoder CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Scrape Subcommand
    scrape_parser = subparsers.add_parser("scrape", help="Run web scraper on Imot.bg")
    scrape_parser.add_argument("--url", default="https://www.imot.bg/obiavi/prodazhbi/oblast-sofiya/kashta?type_home=11~&price_max=50000", help="Base search URL (default: https://www.imot.bg/obiavi/prodazhbi/oblast-sofiya/kashta?type_home=11~&price_max=50000)")
    scrape_parser.add_argument("--type", choices=["land", "houses"], default="houses", help="Property type preset (default: houses)")
    scrape_parser.add_argument("--region", choices=["sofia", "pernik"], default="sofia", help="Region key (default: sofia)")
    scrape_parser.add_argument("--out-json", default="imot.json", help="Output JSON filename (default: imot.json)")
    scrape_parser.add_argument("--export-map", action="store_true", help="Automatically generate CSV map after scraping")
    scrape_parser.add_argument("--out-csv", default="map.csv", help="Output CSV map filename (default: map.csv)")
    scrape_parser.add_argument("--workers", type=int, default=6, help="Concurrent threads (default: 6)")

    # Export Subcommand
    export_parser = subparsers.add_parser("export", help="Export existing JSON to Map CSV")
    export_parser.add_argument("--in-json", default="imot.json", help="Input JSON report file (default: imot.json)")
    export_parser.add_argument("--out-csv",  default="map.csv", help="Output CSV map file (default: map.csv)")
    export_parser.add_argument("--region", choices=["sofia", "pernik"], default="sofia", help="Region key (default: sofia)")

    args = parser.parse_args()

    if args.command == "scrape":
        scraper = ImotScraper(base_url=args.url, preset_name=args.type, region_key=args.region, max_workers=args.workers)
        results = scraper.run()

        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f" 📁 Saved Full JSON Data: {args.out_json}")

        if args.export_map:
            if not args.out_csv:
                print("[!] Error: --out-csv is required when using --export-map.")
                sys.exit(1)
            export_map_csv(results, args.out_csv, args.region)

    elif args.command == "export":
        try:
            with open(args.in_json, "r", encoding="utf-8") as f:
                results = json.load(f)
            export_map_csv(results, args.out_csv, args.region)
        except FileNotFoundError:
            print(f"[!] Error: File '{args.in_json}' not found.")
            sys.exit(1)

if __name__ == "__main__":
    main()