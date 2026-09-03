#!/usr/bin/env python3
"""
Imot.bg Scraper & Map Exporter CLI
"""

import argparse
import json
import math
from pdb import pm
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

# ==============================================================================
# CONFIGURATION PRESETS & REGIONAL METADATA
# ==============================================================================

REGION_CONFIGS = {
    "pernik": {
        "slug": "oblast-pernik",
        "default_region": "област Перник",
        "fallback_coords": (42.6036, 23.0365),
    },
    "sofia": {
        "slug": "oblast-sofiya",
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

CACHE_FILE = "geo_cache.json"

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

        direct_text = str(loc_node.contents[0]).strip()
        if not direct_text:
            return default_reg

        # Clean noise patterns
        clean = direct_text
        for pattern in UI_NOISE_PATTERNS:
            clean = re.sub(pattern, '', clean, flags=re.IGNORECASE)

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
        cena_node = soup.find('div', class_='cena') or soup.find('span', id='advPrice')
        if cena_node:
            price = cena_node.get_text(strip=True)
        else:
            # Fallback: search raw HTML or parent price container for the number + currency
            m = re.search(r'(\d[\d\s\.]*\d\s*(?:EUR|BGN|лв\.|евро|€))', html, re.IGNORECASE)
            if m:
                price = m.group(1).strip()

        size = "N/A"
        size_match = re.search(r'(\d[\d\s]*\d?\s*(?:кв\.м|кв\. м|кв\.м\.|m²))', html, re.IGNORECASE)
        if size_match:
            size = size_match.group(1).strip()

        image_url = ""
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
            if image_url.startswith('//'):
                image_url = 'https:' + image_url

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
            "image_url": image_url,
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
                    print(f" [{completed}/{len(all_urls)}] REJECTED: {item['reject_reason']}")
                else:
                    valid_results.append(item)
                    print(f" [{completed}/{len(all_urls)}] SCORED #{item['score']} | {item['title'][:40]}...")

        valid_results.sort(key=lambda x: x['score'], reverse=True)
        return valid_results

# ==============================================================================
# URL BUILDER HELPER
# ==============================================================================

def build_search_url(region_key, property_type, price_max=50000):
    region_info = REGION_CONFIGS.get(region_key, REGION_CONFIGS["sofia"])
    category = "kashta" if property_type == "houses" else "parcel"
    return f"https://www.imot.bg/obiavi/prodazhbi/{region_info['slug']}/{category}?type_home=11~&price_max={price_max}"

# ==============================================================================
# GEOCODING & MAP EXPORT ENGINE
# ==============================================================================

def load_coordinate_cache():
    global COORDINATE_CACHE
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            COORDINATE_CACHE = json.load(f)
            print(f"[+] Loaded {len(COORDINATE_CACHE)} cached locations from {CACHE_FILE}")
    except FileNotFoundError:
        COORDINATE_CACHE = {}

def save_coordinate_cache():
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(COORDINATE_CACHE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[!] Failed to save coordinate cache: {e}")

def clean_imot_location_string(raw_location_str, default_region):
    text = raw_location_str.replace(f", {default_region}", "").strip()
    text = re.sub(r'\s+(?:ул\.?|кв\.?|квартал|м-т|м\.|местност|пром\.?\s*зона|стопански\s+двор).*$', '', text, flags=re.IGNORECASE)

    village_match = re.search(r'\b(?:с\.|v\.)\s*([А-Яа-яA-Za-z\s-]+)$', text, re.IGNORECASE)
    if village_match:
        clean_v = village_match.group(1).strip()
        return f"v. {clean_v}, {default_region}"

    return f"{text}, {default_region}"

def get_village_coordinates(clean_location_str, default_region):
    # Standardize cache key
    clean_name = re.sub(r'^(?:v|t|с|гр|гара)\b\.?\s*', '', clean_location_str, flags=re.IGNORECASE)
    clean_name = clean_name.replace(f", {default_region}", "").strip(' ,')
    cache_key = f"{clean_name}, {default_region}"

    # 1. Check in-memory/file cache
    if cache_key in COORDINATE_CACHE and COORDINATE_CACHE[cache_key] is not None:
        coords = COORDINATE_CACHE[cache_key]
        return tuple(coords)  # Return as (lat, lon) tuple

    # 2. Query Nominatim API if not cached
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
        print(f" [API Request] Geocoding: '{clean_name}, {default_region}'...")
        time.sleep(1.1)

        res = requests.get(url, params=params, headers=GEO_HEADERS, timeout=5)
        if res.status_code == 200:
            results = res.json()
            if results:
                coords = None
                for item in results:
                    i_type = item.get('type', '').lower()
                    i_class = item.get('class', '').lower()
                    if i_class == 'place' or i_type in ['village', 'town', 'city', 'hamlet', 'locality']:
                        coords = [float(item['lat']), float(item['lon'])]
                        break
                
                if not coords:
                    coords = [float(results[0]['lat']), float(results[0]['lon'])]

                # Cache result and save to disk
                COORDINATE_CACHE[cache_key] = coords
                save_coordinate_cache()
                return tuple(coords)

    except Exception as e:
        print(f"[!] Geocoding error for '{clean_name}': {e}")

    COORDINATE_CACHE[cache_key] = None
    save_coordinate_cache()
    return None

def apply_gps_jitter(lat, lon, index, total_items_in_group, radius=0.00045):
    if total_items_in_group <= 1:
        return lat, lon

    angle = index * (2 * math.pi / total_items_in_group)
    return round(lat + (radius * math.cos(angle)), 6), round(lon + (radius * math.sin(angle)), 6)

def export_map_kml(valid_results, output_kml, region_key):
    region_info = REGION_CONFIGS.get(region_key, {
        "default_region": f"област {region_key.capitalize()}",
        "fallback_coords": (42.6977, 23.3219)
    })
    default_reg = region_info["default_region"]
    fallback_coords = region_info["fallback_coords"]

    # 1. Prepare items & compute Geocoding + GPS Jitter
    prepared_items, location_counts = [], {}

    for item in valid_results:
        raw_loc = item['location']
        sanitized_loc = clean_imot_location_string(raw_loc, default_reg)
        prepared_items.append({"raw_item": item, "raw_loc": raw_loc, "sanitized_loc": sanitized_loc})

    print(f"\n[+] Geocoding {len(prepared_items)} items for KML export...")
    for entry in prepared_items:
        coords = get_village_coordinates(entry["sanitized_loc"], default_reg)
        group_key = entry["sanitized_loc"] if coords is not None else "FALLBACK"
        entry["coords"] = coords
        entry["group_key"] = group_key
        location_counts[group_key] = location_counts.get(group_key, 0) + 1

    # 2. Build KML Tree Structure
    kml = ET.Element('kml', xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml, 'Document')
    # Define Schema ONCE before entering the item loop
    schema = ET.SubElement(document, 'Schema', id="PropertySchema", name="PropertySchema")
    ET.SubElement(schema, 'SimpleField', name="Score", type="int")
    ET.SubElement(schema, 'SimpleField', name="Price", type="string")
    ET.SubElement(schema, 'SimpleField', name="Size", type="string")
    ET.SubElement(schema, 'SimpleField', name="URL", type="string")
    # Red: Score <= 10 (AABBGGRR format: ff0000ff)
    style_red = ET.SubElement(document, 'Style', id="style_red")
    icon_red = ET.SubElement(ET.SubElement(style_red, 'IconStyle'), 'Icon')
    ET.SubElement(icon_red, 'href').text = "http://maps.google.com/mapfiles/ms/icons/red-dot.png"

    # Yellow: Score 11-30 (AABBGGRR format: ff00ffff)
    style_yellow = ET.SubElement(document, 'Style', id="style_yellow")
    icon_yellow = ET.SubElement(ET.SubElement(style_yellow, 'IconStyle'), 'Icon')
    ET.SubElement(icon_yellow, 'href').text = "http://maps.google.com/mapfiles/ms/icons/yellow-dot.png"

    # Green: Score >= 31 (AABBGGRR format: ff0000ff)
    style_green = ET.SubElement(document, 'Style', id="style_green")
    icon_green = ET.SubElement(ET.SubElement(style_green, 'IconStyle'), 'Icon')
    ET.SubElement(icon_green, 'href').text = "http://maps.google.com/mapfiles/ms/icons/green-dot.png"

    group_indices = {}

    # 2. Process Placemarks
    for idx, entry in enumerate(prepared_items, 1):
        item = entry["raw_item"]
        g_key = entry["group_key"]

        # Calculate Jittered Coordinates
        if entry["coords"] is not None:
            b_lat, b_lon = entry["coords"]
            r = 0.00045
        else:
            b_lat, b_lon = fallback_coords
            r = 0.00100

        c_idx = group_indices.get(g_key, 0)
        jit_lat, jit_lon = apply_gps_jitter(b_lat, b_lon, c_idx, location_counts[g_key], radius=r)
        group_indices[g_key] = c_idx + 1

        pm = ET.SubElement(document, 'Placemark')

        # Name / Title
        ET.SubElement(pm, 'name').text = f"{item.get('title', 'N/A')} ({item.get('price', 'N/A')})"

        # ExtendedData (References #PropertySchema created above)
        ext_data = ET.SubElement(pm, 'ExtendedData')
        schema_data = ET.SubElement(ext_data, 'SchemaData', schemaUrl="#PropertySchema")

        score = int(item.get('score', 0))
        if score <= 10:
            ET.SubElement(pm, 'styleUrl').text = "#style_red"
        elif 11 <= score <= 30:
            ET.SubElement(pm, 'styleUrl').text = "#style_yellow"
        else:
            ET.SubElement(pm, 'styleUrl').text = "#style_green"

        p_val = ET.SubElement(schema_data, 'SimpleData', name="Price")
        p_val.text = str(item.get('price', 'N/A'))

        z_val = ET.SubElement(schema_data, 'SimpleData', name="Size")
        z_val.text = str(item.get('size', 'N/A'))

        u_val = ET.SubElement(schema_data, 'SimpleData', name="URL")
        u_val.text = item.get('url', '')

        # HTML Popup Description
        img_url = item.get('image_url', '')
        html_desc = f"""<![CDATA[
            <div style="font-family: sans-serif; max-width: 320px;">
                {'<img src="' + img_url + '" style="width:100%; height:auto; max-height:220px; object-fit:cover; border-radius:6px; margin-bottom:12px;"/>' if img_url else ''}

                <hr style="border:0; border-top:1px solid #ccc; margin:10px 0;"/>

                <p style="margin:4px 0;"><b>Matched:</b> {', '.join(item.get('pos_matches', [])) or 'None'}</p>
                <p style="margin:4px 0;"><b>Penalties:</b> {', '.join(item.get('neg_matches', [])) or 'None'}</p>

                <hr style="border:0; border-top:1px solid #ccc; margin:10px 0;"/>

                <p style="margin-top:6px; color:#444;"><b>Description:</b></p>
                <p style="color:#555; font-size:13px; line-height:1.4;">{item.get('description', '')[:250]}...</p>
            </div>
        ]]>"""
        
        desc_elem = ET.SubElement(pm, 'description')
        desc_elem.text = html_desc

        # Coordinates
        point = ET.SubElement(pm, 'Point')
        ET.SubElement(point, 'coordinates').text = f"{jit_lon},{jit_lat},0"

    # Write file
    tree = ET.ElementTree(kml)
    ET.indent(tree, space="  ")
    tree.write(output_kml, encoding='utf-8', xml_declaration=True)
    print(f" 📍 Saved Map KML File: {output_kml}")

# ==============================================================================
# CLI INTERFACE
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Imot.bg Unified Scraper & Geocoder CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Scrape Subcommand
    scrape_parser = subparsers.add_parser("scrape", help="Run web scraper on Imot.bg")
    scrape_parser.add_argument("--url", default=None, help="Optional: Override generated search URL")
    scrape_parser.add_argument("--type", choices=["land", "houses"], default="houses", help="Property type preset (default: houses)")
    scrape_parser.add_argument("--region", choices=["sofia", "pernik"], default="sofia", help="Region key (default: sofia)")
    scrape_parser.add_argument("--price-max", type=int, default=50000, help="Max price cap (default: 50000)")
    scrape_parser.add_argument("--out-json", default="imot.json", help="Output JSON filename (default: imot.json)")
    scrape_parser.add_argument("--export-map", action="store_true", help="Automatically generate map after scraping")
    scrape_parser.add_argument("--out-kml",  default="map.kml", help="Output KML map file (default: map.kml)")
    scrape_parser.add_argument("--workers", type=int, default=6, help="Concurrent threads (default: 6)")

    # Export Subcommand
    export_parser = subparsers.add_parser("export", help="Export existing JSON to Map KML")
    export_parser.add_argument("--in-json", default="imot.json", help="Input JSON report file (default: imot.json)")
    export_parser.add_argument("--out-kml",  default="map.kml", help="Output KML map file (default: map.kml)")
    export_parser.add_argument("--region", choices=["sofia", "pernik"], default="sofia", help="Region key (default: sofia)")

    args = parser.parse_args()

    if args.command == "scrape":
        target_url = args.url if args.url else build_search_url(args.region, args.type, args.price_max)
        
        print(f"[+] Using Target URL: {target_url}")
        scraper = ImotScraper(base_url=target_url, preset_name=args.type, region_key=args.region, max_workers=args.workers)
        results = scraper.run()

        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f" 📁 Saved Full JSON Data: {args.out_json}")

        if args.export_map:
            if not args.out_kml:
                print("[!] Error: --out-kml is required when using --export-map.")
                sys.exit(1)
            export_map_kml(results, args.out_kml, args.region)

    elif args.command == "export":
        try:
            with open(args.in_json, "r", encoding="utf-8") as f:
                results = json.load(f)
            export_map_kml(results, args.out_kml, args.region)
        except FileNotFoundError:
            print(f"[!] Error: File '{args.in_json}' not found.")
            sys.exit(1)

if __name__ == "__main__":
    load_coordinate_cache()
    main()