#!/usr/bin/env python3
"""
Imot.bg Scraper & Map Exporter CLI
"""

import argparse
import json
import math
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
        "osm_region": "Перник",
        "fallback_coords": (42.6036, 23.0365),
    },
    "sofia": {
        "slug": "oblast-sofiya",
        "osm_region": "Софийска",
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
    r'област\s+[А-Яа-яA-Za-z]+,?',
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
        self.region_info = REGION_CONFIGS.get(region_key, REGION_CONFIGS["sofia"])
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

    def extract_clean_settlement(self, soup):
        loc_node = soup.find('div', class_='location') or soup.find('span', class_='advLocation') or soup.find('span', class_='location')
        if not loc_node or not loc_node.contents:
            return ""

        text = str(loc_node.contents[0]).strip()
        for pattern in UI_NOISE_PATTERNS:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)

        # Strip street/micro-district noise
        text = re.sub(r'\s+(?:ул\.?|кв\.?|квартал|м-т|м\.|местност|пром\.?\s*зона|стопански\s+двор).*$', '', text, flags=re.IGNORECASE)
        # Extract pure settlement name
        clean_name = re.sub(r'^(?:v|t|с|гр|гара|село|град)\b\.?\s*', '', text, flags=re.IGNORECASE).strip(' ,.')
        return clean_name

    def parse_listing(self, url):
        html = self.fetch_html(url)
        if not html:
            return None

        soup = BeautifulSoup(html, 'html.parser')
        clean_settlement = self.extract_clean_settlement(soup)
        loc_node = soup.find('div', class_='location') or soup.find('span', class_='advLocation') or soup.find('span', class_='location')
        raw_loc = loc_node.get_text(separator=' ', strip=True) if loc_node else clean_settlement

        title = "N/A"
        title_node = soup.find('div', class_='adTitle') or soup.find('h1') or soup.find('span', class_='advTitle')
        if title_node:
            title = re.sub(r'\s+', ' ', title_node.get_text(separator=' ', strip=True))

        price = "N/A"
        cena_node = soup.find('div', class_='cena') or soup.find('span', id='advPrice')
        if cena_node:
            price = cena_node.get_text(strip=True)
        else:
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
            "settlement": clean_settlement,
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

def get_village_coordinates(settlement_name, osm_region):
    if not settlement_name:
        return None

    settlement_name = re.sub(r'^(?:v|t|с|гр|гара|село|град)\b\.?\s*', '', settlement_name, flags=re.IGNORECASE)

    cache_key = f"{settlement_name}, {osm_region}"
    if cache_key in COORDINATE_CACHE and COORDINATE_CACHE[cache_key] is not None:
        return tuple(COORDINATE_CACHE[cache_key])

    url = "https://nominatim.openstreetmap.org/search"
    query_str = f"{settlement_name}, {osm_region}, България"
    
    params = {
        'q': query_str,
        'format': 'json',
        'addressdetails': 1,
        'limit': 10
    }

    try:
        print(f" [API Request] Geocoding: '{query_str}'...")
        time.sleep(1.1)

        res = requests.get(url, params=params, headers=GEO_HEADERS, timeout=5)
        if res.status_code == 200:
            results = res.json()
            coords = None

            EXCLUDED_CLASSES = {'railway', 'highway', 'building', 'amenity', 'shop', 'landuse'}
            EXCLUDED_TYPES = {'railway', 'road', 'street', 'bus_stop', 'station', 'stop', 'house', 'residential', 'commercial'}

            # --- PASS 1: Settlement Node / Place Matching ---
            for item in results:
                addr = item.get('address', {})
                addresstype = item.get('addresstype', '')
                i_class = item.get('class', '').lower()
                i_type = item.get('type', '').lower()
                display_name = item.get('display_name', '')

                if i_class in EXCLUDED_CLASSES or addresstype in EXCLUDED_TYPES or 'ж.к.' in display_name:
                    continue

                if addresstype in ['village', 'town', 'city', 'hamlet', 'municipality'] or \
                   (i_class == 'place' and i_type in ['village', 'town', 'city', 'hamlet']):
                    coords = [float(item['lat']), float(item['lon'])]
                    print(f" [✓] Matched Settlement: {display_name}")
                    break

            # --- PASS 2: Settlement Boundary Relation (admin_level 8 / землище) ---
            if not coords:
                for item in results:
                    addr = item.get('address', {})
                    i_class = item.get('class', '')
                    i_type = item.get('type', '')
                    display_name = item.get('display_name', '')

                    if i_class in EXCLUDED_CLASSES or 'ж.к.' in display_name:
                        continue

                    addr_str = " ".join(str(v) for v in addr.values())

                    if i_class == 'boundary' and i_type == 'administrative':
                        if osm_region.lower() in addr_str.lower():
                            coords = [float(item['lat']), float(item['lon'])]
                            print(f" [✓] Matched Regional Boundary: {display_name}")
                            break

            if coords:
                COORDINATE_CACHE[cache_key] = coords
                save_coordinate_cache()
                return tuple(coords)

    except Exception as e:
        print(f"[!] Geocoding error for '{settlement_name}': {e}")

    print(f" [!] No valid settlement match for '{query_str}'.")
    COORDINATE_CACHE[cache_key] = None
    save_coordinate_cache()
    return None

def apply_gps_jitter(lat, lon, index, total_items_in_group, radius=0.00045):
    if total_items_in_group <= 1:
        return lat, lon

    angle = index * (2 * math.pi / total_items_in_group)
    return round(lat + (radius * math.cos(angle)), 6), round(lon + (radius * math.sin(angle)), 6)

def export_map_kml(valid_results, output_kml, region_key):
    region_info = REGION_CONFIGS.get(region_key, REGION_CONFIGS["sofia"])
    osm_region = region_info["osm_region"]
    fallback_coords = region_info["fallback_coords"]

    prepared_items, location_counts = [], {}

    print(f"\n[+] Geocoding {len(valid_results)} items for KML export...")
    for item in valid_results:
        settlement = item.get('settlement', '')
        coords = get_village_coordinates(settlement, osm_region)
        group_key = settlement if coords is not None else "FALLBACK"
        
        prepared_items.append({
            "raw_item": item,
            "settlement": settlement,
            "coords": coords,
            "group_key": group_key
        })
        location_counts[group_key] = location_counts.get(group_key, 0) + 1

    # Build KML
    kml = ET.Element('kml', xmlns="http://www.opengis.net/kml/2.2")
    document = ET.SubElement(kml, 'Document')

    schema = ET.SubElement(document, 'Schema', id="PropertySchema", name="PropertySchema")
    ET.SubElement(schema, 'SimpleField', name="Score", type="int")
    ET.SubElement(schema, 'SimpleField', name="Price", type="string")
    ET.SubElement(schema, 'SimpleField', name="Size", type="string")
    ET.SubElement(schema, 'SimpleField', name="URL", type="string")

    for style_id, color in [("style_red", "red"), ("style_yellow", "yellow"), ("style_green", "green")]:
        style = ET.SubElement(document, 'Style', id=style_id)
        icon = ET.SubElement(ET.SubElement(style, 'IconStyle'), 'Icon')
        ET.SubElement(icon, 'href').text = f"http://maps.google.com/mapfiles/ms/icons/{color}-dot.png"

    group_indices = {}

    for entry in prepared_items:
        item = entry["raw_item"]
        g_key = entry["group_key"]

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
        ET.SubElement(pm, 'name').text = f"{item.get('title', 'N/A')} ({item.get('price', 'N/A')})"

        ext_data = ET.SubElement(pm, 'ExtendedData')
        schema_data = ET.SubElement(ext_data, 'SchemaData', schemaUrl="#PropertySchema")

        score = int(item.get('score', 0))
        style_ref = "#style_red" if score <= 10 else ("#style_yellow" if score <= 30 else "#style_green")
        ET.SubElement(pm, 'styleUrl').text = style_ref

        ET.SubElement(schema_data, 'SimpleData', name="Price").text = str(item.get('price', 'N/A'))
        ET.SubElement(schema_data, 'SimpleData', name="Size").text = str(item.get('size', 'N/A'))
        ET.SubElement(schema_data, 'SimpleData', name="URL").text = item.get('url', '')

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
        
        ET.SubElement(pm, 'description').text = html_desc
        point = ET.SubElement(pm, 'Point')
        ET.SubElement(point, 'coordinates').text = f"{jit_lon},{jit_lat},0"

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
    scrape_parser.add_argument("--export-map", action="store_true", help="Automatically generate map after scraping")
    scrape_parser.add_argument("--workers", type=int, default=6, help="Concurrent threads (default: 6)")

    # Export Subcommand
    export_parser = subparsers.add_parser("export", help="Export existing JSON to Map KML")
    export_parser.add_argument("--region", choices=["sofia", "pernik"], default="sofia", help="Region key (default: sofia)")
    export_parser.add_argument("--in-json", default=None, help="Input JSON report file (default: imot_{region}.json)")

    args = parser.parse_args()

    out_json = f"imot_{args.region}.json"
    out_kml = f"map_{args.region}.kml"

    if args.command == "scrape":
        target_url = args.url if args.url else build_search_url(args.region, args.type, args.price_max)
        
        print(f"[+] Using Target URL: {target_url}")
        scraper = ImotScraper(base_url=target_url, preset_name=args.type, region_key=args.region, max_workers=args.workers)
        results = scraper.run()

        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Saved Full JSON Data: {out_json}")

        if args.export_map:
            export_map_kml(results, out_kml, args.region)

    elif args.command == "export":
        in_json = args.in_json or f"imot_{args.region}.json"
        try:
            with open(in_json, "r", encoding="utf-8") as f:
                results = json.load(f)
            export_map_kml(results, out_kml, args.region)
        except FileNotFoundError:
            print(f"[!] Error: File '{in_json}' not found.")
            sys.exit(1)

if __name__ == "__main__":
    load_coordinate_cache()
    main()