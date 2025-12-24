"""
Woot → eBay Sold Arbitrage Checker MVP

Run Instructions:
1. Install dependencies: pip install -r requirements.txt
2. Run: python run.py (defaults to Woot mode)
3. For watchlist mode: python run.py watchlist

Debug mode: Set DEBUG = True at the top of this file for verbose output
"""

import requests
from bs4 import BeautifulSoup
import re
import json
import os
import sys
import argparse
import random
import time
from urllib.parse import urlparse, quote, urlencode
from typing import Optional, Tuple, List, Dict, Any
from statistics import mean, median
from pathlib import Path

# ============================================================================
# CONFIGURATION
# ============================================================================

DEBUG = True  # Set to True for verbose output

# Woot settings
WOOT_MAX_ITEMS = 10  # Max items to scan from Woot (default)

# eBay search settings
EBAY_FEE_RATE = 0.15
SHIPPING_BUFFER = 10
MISC_BUFFER = 2
MIN_DELAY_SEC = 10.0
RETRY_DELAYS = [30, 90]  # seconds for rate limit retries
CACHE_TTL_SECONDS = 24 * 3600  # 24 hours (legacy, use get_cache_ttl() for status-based TTLs)
CACHE_DIR = 'cache'
CACHE_FILE = os.path.join(CACHE_DIR, 'ebay_cache.json')
CACHE_VERSION = 2  # Increment to invalidate old cache entries

# Filter thresholds
MIN_PROFIT = 20
MIN_ROI = 0.25
MIN_SOLD_COUNT = 5

# HTTP settings
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
TIMEOUT = 15
MAX_SOLD_ITEMS = 20  # Limit eBay sold items to parse

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def log_debug(message: str):
    """Print debug message if DEBUG mode is enabled."""
    if DEBUG:
        print(f"[DEBUG] {message}")

def save_debug_html(store: str, index: int, html: str):
    """Save raw HTML to debug folder for inspection."""
    if not DEBUG:
        return
    
    debug_dir = 'debug'
    if not os.path.exists(debug_dir):
        os.makedirs(debug_dir)
    
    filename = f"{store}_{index}.html"
    filepath = os.path.join(debug_dir, filename)
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)
        log_debug(f"Saved HTML to {filepath}")
    except Exception as e:
        log_debug(f"Failed to save debug HTML: {e}")

def is_blocked_page(html: str) -> bool:
    """Check if HTML contains bot/consent detection indicators."""
    html_lower = html.lower()
    blocked_indicators = [
        "captcha",
        "robot check",
        "automated access",
        "enter the characters you see",
        "consent",
        "verify you are a human",
        "blocked"
    ]
    return any(indicator in html_lower for indicator in blocked_indicators)

def fetch_page(url: str, store: str, index: int) -> Optional[Tuple[BeautifulSoup, requests.Response]]:
    """Fetch a web page and return (BeautifulSoup object, Response) or None on failure."""
    try:
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        
        # Print HTTP status and final URL
        print(f"  HTTP Status: {response.status_code}")
        print(f"  Final URL: {response.url}")
        print(f"  Response Length: {len(response.text)} chars")
        
        # Extract and print page title
        soup = BeautifulSoup(response.text, 'lxml')
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.get_text().strip()
            print(f"  Page Title: {title_text[:120]}")
        else:
            print(f"  Page Title: (not found)")
        
        # Save HTML if DEBUG mode
        save_debug_html(store, index, response.text)
        
        # Note: We don't return None for blocked pages here - let parse functions handle it
        # so they can return appropriate fail_reason
        if is_blocked_page(response.text):
            print(f"  → Blocked/Consent page detected")
        
        response.raise_for_status()
        return (soup, response)
    except requests.exceptions.RequestException as e:
        log_debug(f"Failed to fetch {url}: {e}")
        return None
    except Exception as e:
        log_debug(f"Error processing response from {url}: {e}")
        return None

def build_query_confidence(title: str) -> Dict[str, Any]:
    """
    Build query confidence score for eBay search.
    Returns dict with:
    - confidence: "high" | "med" | "low"
    - reasons: list[str] (explaining the confidence level)
    - query: str (normalized query for eBay)
    """
    title_lower = title.lower()
    reasons = []
    confidence = "low"
    
    # Check for known brands (HIGH confidence indicator)
    known_brands = [
        'milwaukee', 'dewalt', 'makita', 'bosch', 'ryobi', 'craftsman',
        'ridgid', 'kobalt', 'klein', 'fluke', 'lutron', 'husky', 'metabo',
        'delta', 'stanley', 'black+decker', 'black & decker', 'snap-on',
        'knipex', 'irwin', 'channel lock', 'channellock', 'crescent'
    ]
    found_brand = None
    for brand in known_brands:
        if brand in title_lower:
            found_brand = brand
            reasons.append(f"brand:{brand}")
            confidence = "high"
            break
    
    # Check for model number patterns (HIGH confidence indicator)
    # Patterns: sequences with digits and dashes like 48-22-9802, DCD777, 18V, M18, etc.
    model_patterns = [
        r'\b[A-Z]{2,}\d{2,}\b',  # DCD777, M18, etc.
        r'\b\d+[-/]\d+[-/]?\d*\b',  # 48-22-9802, 48/22/9802
        r'\b\d{2,}[Vv]\b',  # 18V, 20V, etc.
        r'\b[A-Z]\d{2,}[A-Z]?\b',  # M18, DCD777B, etc.
    ]
    found_model = False
    for pattern in model_patterns:
        if re.search(pattern, title, re.IGNORECASE):
            found_model = True
            reasons.append("model_pattern")
            confidence = "high"
            break
    
    # MED confidence: title length >= 25 chars AND contains 2+ strong nouns
    # Simple heuristic: count words >= 4 chars (likely nouns) excluding common stop words
    if confidence == "low" and len(title) >= 25:
        words = title.split()
        # Filter out common stop words and short words
        stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from'}
        strong_words = [w for w in words if len(w) >= 4 and w.lower() not in stop_words]
        if len(strong_words) >= 2:
            confidence = "med"
            reasons.append(f"long_title({len(title)} chars, {len(strong_words)} strong_words)")
    
    # LOW confidence indicators: generic phrases
    generic_phrases = [
        'heavy duty', 'premium', 'kit', 'set', 'storage', 'organizer',
        'outdoor lights', 'generic', 'universal', 'multi-purpose'
    ]
    
    # Check if title contains generic phrases (only matters if we don't have high confidence)
    if confidence != "high":
        found_generic = False
        for phrase in generic_phrases:
            if phrase in title_lower:
                found_generic = True
                reasons.append(f"generic:{phrase}")
                break
        
        # Check for numbers - if only quantities (e.g., 2-pack, 3-pack), that's a low confidence indicator
        # Look for quantity patterns: \d+-?pack, \d+-?piece, \d+-?count
        quantity_patterns = [r'\d+-?pack', r'\d+-?piece', r'\d+-?count', r'\d+-?pcs']
        has_quantity_only = any(re.search(pattern, title_lower) for pattern in quantity_patterns)
        
        # Check if there are any numbers that aren't just quantities
        # Look for numbers that aren't part of quantity patterns
        all_numbers = re.findall(r'\d+', title)
        non_quantity_numbers = [n for n in all_numbers if not any(re.search(f'\\b{n}\\s*-?(pack|piece|count|pcs)\\b', title_lower) for _ in [1])]
        
        if found_generic and (has_quantity_only or len(non_quantity_numbers) == 0):
            confidence = "low"
            if "generic:" not in " ".join(reasons):
                reasons.append("generic_phrases_no_model")
        elif not found_model and not found_brand and len(non_quantity_numbers) == 0:
            # No model/brand and no numbers = likely low confidence
            confidence = "low"
            if not reasons:
                reasons.append("no_brand_no_model_no_numbers")
    
    # Normalize query
    normalized_query = normalize_query(title)
    
    return {
        'confidence': confidence,
        'reasons': reasons,
        'query': normalized_query
    }

def extract_filter_size(title: str) -> Optional[Tuple[float, float, float]]:
    """
    Extract filter size from title in format AxBxC (e.g., 20x25x1, 16x25x4).
    Returns tuple (A, B, C) or None if not found.
    Handles decimal values and spaces.
    """
    # Pattern: optional decimal number, optional space, x, optional space, decimal number, optional space, x, optional space, decimal number
    pattern = r'(\d+\.?\d*)\s*[xX×]\s*(\d+\.?\d*)\s*[xX×]\s*(\d+\.?\d*)'
    match = re.search(pattern, title)
    if match:
        try:
            return (float(match.group(1)), float(match.group(2)), float(match.group(3)))
        except (ValueError, IndexError):
            return None
    return None

def is_filter_like(title: str) -> bool:
    """Check if title indicates a filter product (filter|merv|mpr|hvac|furnace)."""
    title_lower = title.lower()
    filter_keywords = ['filter', 'merv', 'mpr', 'hvac', 'furnace']
    return any(keyword in title_lower for keyword in filter_keywords)

def normalize_query(query: str) -> str:
    """
    Normalize query for cache key: lowercase, remove punctuation, collapse spaces,
    remove stop words: pack, kit, set, new, open box
    """
    # Lowercase
    normalized = query.lower()
    
    # Remove stop words
    stop_words = ['pack', 'kit', 'set', 'new', 'open box']
    for word in stop_words:
        # Use word boundaries to match whole words only
        pattern = r'\b' + re.escape(word) + r'\b'
        normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)
    
    # Remove punctuation (keep alphanumeric and spaces)
    normalized = re.sub(r'[^\w\s]', ' ', normalized)
    
    # Collapse multiple spaces to single space
    normalized = re.sub(r'\s+', ' ', normalized)
    
    # Trim
    normalized = normalized.strip()
    
    return normalized

def clean_title_for_ebay(title: str) -> str:
    """Clean product title for eBay search by removing common fluff."""
    # Remove common fluff words/phrases
    fluff_patterns = [
        r'\bnew\b', r'\bfree shipping\b', r'\bfast shipping\b',
        r'\bfree returns\b', r'\bprime\b', r'\bamazon\b', r'\bwalmart\b',
        r'\bofficial\b', r'\bauthentic\b', r'\bgenuine\b',
        r'\bwith\s+\w+\s+gift\b', r'\bbundle\b'
    ]
    cleaned = title.lower()
    for pattern in fluff_patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    # Remove extra spaces and trim
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    # Limit length for eBay search
    return cleaned[:100]

def is_excluded_listing(title: str, price_text: str = "") -> bool:
    """Check if a listing should be excluded (parts only, bundles, etc.)."""
    title_lower = title.lower()
    price_lower = price_text.lower()
    
    exclusion_terms = [
        'parts only', 'for parts', 'read description', 'not working',
        'broken', 'damaged', 'lot of', 'lots of', 'bundle of', 'set of',
        'multi pack', 'pack of', 'x ', ' x ', 'quantity:', 'qty:'
    ]
    
    for term in exclusion_terms:
        if term in title_lower or term in price_lower:
            return True
    
    # Check for obvious quantity indicators
    if re.search(r'\b\d+\s*(pack|piece|unit|item)\b', title_lower):
        return True
    
    return False

# ============================================================================
# PRODUCT PARSING
# ============================================================================

def parse_amazon_product(url: str, index: int) -> Optional[Tuple[str, float, Optional[str]]]:
    """Parse Amazon product page for title and price. Returns (title, price, fail_reason) or None."""
    result = fetch_page(url, 'Amazon', index)
    if not result:
        return None
    
    soup, response = result
    
    # Check for blocked/consent page after fetching - fail fast
    if is_blocked_page(response.text):
        return ('', 0.0, 'Amazon blocked; skip in MVP')
    
    try:
        title = None
        price = None
        fail_reason_parts = []
        
        # Parse title: try #productTitle, then meta og:title, then <title>
        title_elem = soup.find(id='productTitle')
        if title_elem:
            title = title_elem.get_text().strip()
        else:
            meta_og_title = soup.find('meta', {'property': 'og:title'})
            if meta_og_title and meta_og_title.get('content'):
                title = meta_og_title['content'].strip()
            else:
                title_tag = soup.find('title')
                if title_tag:
                    title_text = title_tag.get_text().strip()
                    # Clean up Amazon title (usually "Product Name : Amazon.com: ...")
                    title = title_text.split(':')[0].strip()
                else:
                    fail_reason_parts.append("title not found")
        
        # Parse price: try span.a-price span.a-offscreen first, then meta itemprop="price", then regex
        if not price:
            # Method 1: span.a-price span.a-offscreen
            price_elem = soup.select_one('span.a-price span.a-offscreen')
            if price_elem:
                price_text = price_elem.get_text().strip()
                price_match = re.search(r'[\d,]+\.?\d*', price_text.replace(',', ''))
                if price_match:
                    price = float(price_match.group())
        
        if not price:
            # Method 2: meta itemprop="price"
            meta_price = soup.find('meta', {'itemprop': 'price'})
            if meta_price and meta_price.get('content'):
                try:
                    price = float(meta_price['content'])
                except ValueError:
                    pass
        
        if not price:
            # Method 3: regex pattern near "price" keyword
            page_text = soup.get_text()
            price_context = re.search(r'(?:price|cost|buy|now)[:\s]*\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)', page_text, re.I)
            if price_context:
                price_str = price_context.group(1).replace(',', '')
                try:
                    price = float(price_str)
                except ValueError:
                    pass
        
        if not price or price <= 0:
            fail_reason_parts.append("price not found or invalid")
        
        if title and price and price > 0:
            return (title, price, None)
        else:
            fail_reason = "; ".join(fail_reason_parts) if fail_reason_parts else "title or price extraction failed"
            return (title or '', price or 0.0, f"Amazon parse failed: {fail_reason}")
            
    except Exception as e:
        log_debug(f"Error parsing Amazon product: {e}")
        return ('', 0.0, f"Amazon parse error: {str(e)}")

def find_in_json(obj, keys_to_try, value_type=None):
    """Recursively search JSON object for keys and return first matching value."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys_to_try:
                if value_type is None or isinstance(value, value_type):
                    return value
            # Recursively search nested objects
            result = find_in_json(value, keys_to_try, value_type)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = find_in_json(item, keys_to_try, value_type)
            if result is not None:
                return result
    return None

def extract_walmart_product_id(url: str) -> Optional[str]:
    """Extract numeric product ID from Walmart URL."""
    # Walmart URLs are like: https://www.walmart.com/ip/<slug>/<id>
    # or https://www.walmart.com/ip/<id>
    # Extract the last numeric segment
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split('/') if p]
    
    # Look for /ip/ in path
    if 'ip' in path_parts:
        ip_index = path_parts.index('ip')
        if ip_index + 1 < len(path_parts):
            # Last part after /ip/ should be the ID
            potential_id = path_parts[-1]
            # Check if it's numeric
            if potential_id.isdigit():
                return potential_id
    
    return None

def fetch_json_endpoint(url: str) -> Optional[dict]:
    """Fetch JSON endpoint and return parsed JSON, or None on failure."""
    try:
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        
        if response.status_code != 200:
            return None
        
        # Check if content is JSON
        content_type = response.headers.get('Content-Type', '').lower()
        is_json = 'application/json' in content_type or response.text.strip().startswith('{')
        
        if not is_json:
            return None
        
        try:
            return response.json()
        except json.JSONDecodeError:
            return None
    except Exception as e:
        log_debug(f"Error fetching JSON endpoint {url}: {e}")
        return None

def parse_walmart_json(json_data: dict) -> Optional[Tuple[str, float]]:
    """Extract title and price from Walmart JSON response."""
    title = None
    price = None
    
    # Extract title using recursive search
    title_candidates = find_in_json(json_data, ['name', 'productName', 'title'], str)
    if title_candidates:
        if isinstance(title_candidates, str) and 10 <= len(title_candidates) <= 200:
            title = title_candidates
        elif isinstance(title_candidates, list):
            for candidate in title_candidates:
                if isinstance(candidate, str) and 10 <= len(candidate) <= 200:
                    title = candidate
                    break
    
    # Extract price using recursive search
    # First try to find numeric values
    price_candidates = find_in_json(json_data, ['price', 'currentPrice', 'offerPrice', 'salesPrice'])
    if price_candidates is not None:
        if isinstance(price_candidates, (int, float)):
            price = float(price_candidates)
        elif isinstance(price_candidates, str):
            # Strip $ and extract numeric value
            price_str = price_candidates.replace('$', '').replace(',', '').strip()
            try:
                price = float(price_str)
            except ValueError:
                pass
        elif isinstance(price_candidates, dict):
            # Handle nested price objects like {"price": 19.99} or {"value": 19.99}
            if 'price' in price_candidates:
                try:
                    price = float(price_candidates['price'])
                except (ValueError, TypeError):
                    pass
            if not price and 'value' in price_candidates:
                try:
                    price = float(price_candidates['value'])
                except (ValueError, TypeError):
                    pass
            # Try priceString field
            if not price and 'priceString' in price_candidates:
                price_str = str(price_candidates['priceString']).replace('$', '').replace(',', '').strip()
                try:
                    price = float(price_str)
                except ValueError:
                    pass
    
    # Try priceInfo.currentPrice.price specifically (common Walmart structure)
    if not price:
        try:
            price_info = json_data.get('priceInfo', {})
            if isinstance(price_info, dict):
                current_price = price_info.get('currentPrice', {})
                if isinstance(current_price, dict):
                    if 'price' in current_price:
                        try:
                            price = float(current_price['price'])
                        except (ValueError, TypeError):
                            pass
                    if not price and 'priceString' in current_price:
                        price_str = str(current_price['priceString']).replace('$', '').replace(',', '').strip()
                        try:
                            price = float(price_str)
                        except ValueError:
                            pass
        except (AttributeError, TypeError):
            pass
    
    if title and price and price > 0:
        return (title, price)
    return None

def try_walmart_json_endpoints(product_id: str) -> Optional[Tuple[str, float, str]]:
    """Try Walmart JSON endpoints in order. Returns (title, price, endpoint_url) or None."""
    endpoints = [
        f"https://www.walmart.com/ip/{product_id}?format=json",
        f"https://www.walmart.com/ip/{product_id}?selected=true&format=json",
        f"https://www.walmart.com/terra-firma/item/{product_id}",
        f"https://www.walmart.com/product/{product_id}",
    ]
    
    for endpoint_url in endpoints:
        log_debug(f"Trying Walmart JSON endpoint: {endpoint_url}")
        json_data = fetch_json_endpoint(endpoint_url)
        
        if json_data:
            result = parse_walmart_json(json_data)
            if result:
                title, price = result
                log_debug(f"Successfully parsed from {endpoint_url}")
                return (title, price, endpoint_url)
            else:
                log_debug(f"Endpoint returned JSON but couldn't extract title/price: {endpoint_url}")
        else:
            log_debug(f"Endpoint failed or not JSON: {endpoint_url}")
    
    return None

def parse_walmart_product(url: str, index: int) -> Optional[Tuple[str, float, Optional[str]]]:
    """Parse Walmart product page for title and price. Returns (title, price, fail_reason) or None."""
    # First, try JSON endpoints (works even if HTML is blocked)
    product_id = extract_walmart_product_id(url)
    if product_id:
        if DEBUG:
            print(f"  → Extracted product ID: {product_id}")
            print(f"  → Trying Walmart JSON endpoints...")
        json_result = try_walmart_json_endpoints(product_id)
        if json_result:
            title, price, endpoint_url = json_result
            if DEBUG:
                print(f"  → Successfully parsed from JSON endpoint: {endpoint_url}")
            return (title, price, None)
        else:
            if DEBUG:
                print(f"  → All JSON endpoints failed, falling back to HTML parsing")
    else:
        if DEBUG:
            print(f"  → Could not extract product ID from URL, trying HTML parsing")
    
    # Fall back to HTML parsing
    result = fetch_page(url, 'Walmart', index)
    if not result:
        # If we couldn't fetch HTML and JSON endpoints failed, report failure
        if product_id:
            return ('', 0.0, 'Walmart HTML blocked; JSON endpoints failed')
        return None
    
    soup, response = result
    
    # Check for blocked/consent page after fetching
    html_blocked = is_blocked_page(response.text)
    if html_blocked:
        # JSON endpoints already failed (otherwise we would have returned earlier)
        return ('', 0.0, 'Walmart HTML blocked; JSON endpoints failed')
    
    try:
        title = None
        price = None
        fail_reason_parts = []
        
        # Method 1: Try __NEXT_DATA__ script tag (type="application/json")
        next_data_script = soup.find('script', id='__NEXT_DATA__', type='application/json')
        if not next_data_script:
            next_data_script = soup.find('script', id='__NEXT_DATA__')
        
        if next_data_script:
            try:
                next_data = json.loads(next_data_script.string)
                
                # Find title: look for strings 10-200 chars in keys like "name", "productName", "title"
                title_candidates = find_in_json(next_data, ['name', 'productName', 'title'], str)
                if title_candidates:
                    # Filter by length
                    if isinstance(title_candidates, str) and 10 <= len(title_candidates) <= 200:
                        title = title_candidates
                    elif isinstance(title_candidates, list):
                        for candidate in title_candidates:
                            if isinstance(candidate, str) and 10 <= len(candidate) <= 200:
                                title = candidate
                                break
                
                # Find price: look for numeric values in keys like "price", "currentPrice", "priceValue", etc.
                price_candidates = find_in_json(next_data, ['price', 'currentPrice', 'priceValue', 'basePrice', 'offerPrice'])
                if price_candidates is not None:
                    # Try to convert to float
                    if isinstance(price_candidates, (int, float)):
                        price = float(price_candidates)
                    elif isinstance(price_candidates, str):
                        # Try to extract numeric value
                        price_match = re.search(r'[\d,]+\.?\d*', price_candidates.replace(',', ''))
                        if price_match:
                            price = float(price_match.group())
                    elif isinstance(price_candidates, dict):
                        # Try common nested price keys
                        if 'price' in price_candidates:
                            try:
                                price = float(price_candidates['price'])
                            except (ValueError, TypeError):
                                pass
                        if not price and 'value' in price_candidates:
                            try:
                                price = float(price_candidates['value'])
                            except (ValueError, TypeError):
                                pass
                
            except (json.JSONDecodeError, (KeyError, ValueError, TypeError)) as e:
                fail_reason_parts.append(f"__NEXT_DATA__ parse failed: {str(e)}")
        
        # Method 2: Fallback to application/ld+json
        if not title or not price:
            json_scripts = soup.find_all('script', type='application/ld+json')
            for script in json_scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        if not title and 'name' in data:
                            title_candidate = data['name']
                            if isinstance(title_candidate, str) and 10 <= len(title_candidate) <= 200:
                                title = title_candidate
                        if not price and 'offers' in data:
                            offers = data['offers']
                            if isinstance(offers, dict) and 'price' in offers:
                                try:
                                    price = float(offers['price'])
                                except (ValueError, TypeError):
                                    pass
                            elif isinstance(offers, list) and len(offers) > 0:
                                try:
                                    price = float(offers[0].get('price', 0))
                                except (ValueError, TypeError):
                                    pass
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                if not title and 'name' in item:
                                    title_candidate = item['name']
                                    if isinstance(title_candidate, str) and 10 <= len(title_candidate) <= 200:
                                        title = title_candidate
                                if not price and 'offers' in item:
                                    offers = item['offers']
                                    if isinstance(offers, dict) and 'price' in offers:
                                        try:
                                            price = float(offers['price'])
                                            break
                                        except (ValueError, TypeError):
                                            pass
                except (json.JSONDecodeError, (KeyError, ValueError, TypeError)):
                    continue
        
        # Method 3: Fallback to meta itemprop="price"
        if not price:
            meta_price = soup.find('meta', {'itemprop': 'price'})
            if meta_price and meta_price.get('content'):
                try:
                    price = float(meta_price['content'])
                except ValueError:
                    pass
        
        if not title:
            fail_reason_parts.append("title not found")
        if not price or price <= 0:
            fail_reason_parts.append("price not found or invalid")
        
        if title and price and price > 0:
            return (title, price, None)
        else:
            fail_reason = "; ".join(fail_reason_parts) if fail_reason_parts else "title or price extraction failed"
            return (title or '', price or 0.0, f"Walmart parse failed: {fail_reason}")
            
    except Exception as e:
        log_debug(f"Error parsing Walmart product: {e}")
        return ('', 0.0, f"Walmart parse error: {str(e)}")

def parse_product(url: str, index: int) -> Optional[Tuple[str, float, str, Optional[str]]]:
    """Parse product from URL. Returns (title, price, store, fail_reason) or None."""
    parsed_url = urlparse(url)
    domain = parsed_url.netloc.lower()
    
    if 'amazon' in domain:
        result = parse_amazon_product(url, index)
        if result:
            title, price, fail_reason = result
            return (title, price, 'Amazon', fail_reason)
        return None
    elif 'walmart' in domain:
        result = parse_walmart_product(url, index)
        if result:
            title, price, fail_reason = result
            return (title, price, 'Walmart', fail_reason)
        return None
    else:
        log_debug(f"Unknown domain: {domain}")
        return None

# ============================================================================
# WOOT FEED FETCHING
# ============================================================================

def fetch_json_endpoint_simple(url: str) -> Optional[dict]:
    """Fetch JSON endpoint and return parsed JSON, or None on failure."""
    try:
        headers = {'User-Agent': USER_AGENT}
        response = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        
        if response.status_code != 200:
            log_debug(f"HTTP {response.status_code} from {url}")
            return None
        
        # Check if content is JSON
        content_type = response.headers.get('Content-Type', '').lower()
        is_json = 'application/json' in content_type or response.text.strip().startswith('{') or response.text.strip().startswith('[')
        
        if not is_json:
            log_debug(f"Not JSON content from {url}")
            return None
        
        try:
            return response.json()
        except json.JSONDecodeError as e:
            log_debug(f"JSON decode error from {url}: {e}")
            return None
    except Exception as e:
        log_debug(f"Error fetching {url}: {e}")
        return None

def fetch_woot_deals(category: str = 'All', limit: int = 100) -> List[Dict]:
    """Fetch Woot deals from official Developer API. Returns list of deal dicts."""
    # Check for API key
    api_key = os.environ.get('WOOT_API_KEY', '').strip()
    if not api_key:
        print("Missing WOOT_API_KEY. Set it in PowerShell: $env:WOOT_API_KEY='...'")
        sys.exit(1)
    
    # Build endpoint URL
    endpoint = f"https://developer.woot.com/feed/{category}"
    
    # Print endpoint being called
    print(f"  Calling endpoint: {endpoint}")
    
    try:
        headers = {
            'User-Agent': USER_AGENT,
            'Accept': 'application/json',
            'x-api-key': api_key
        }
        response = requests.get(endpoint, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        
        # Print HTTP status
        print(f"  HTTP Status: {response.status_code}")
        
        if response.status_code != 200:
            # Debug: print first 200 chars of response on non-200
            error_preview = response.text[:200] if response.text else "(empty response)"
            print(f"  Error response (first 200 chars): {error_preview}")
            return []
        
        # Parse JSON response
        try:
            json_data = response.json()
        except json.JSONDecodeError as e:
            print(f"  JSON decode error: {e}")
            return []
        
        # Extract Items list from response
        deals = []
        if isinstance(json_data, dict) and 'Items' in json_data:
            deals = json_data['Items']
        elif isinstance(json_data, list):
            deals = json_data
        else:
            print(f"  Unexpected JSON structure: {type(json_data)}")
            if DEBUG:
                print(f"  JSON keys: {list(json_data.keys()) if isinstance(json_data, dict) else 'N/A'}")
            return []
        
        if DEBUG:
            print(f"  Successfully fetched {len(deals)} items from API")
        
        # Apply limit
        return deals[:limit]
        
    except requests.exceptions.RequestException as e:
        print(f"  Request error: {e}")
        return []
    except Exception as e:
        print(f"  Unexpected error: {e}")
        return []

def parse_woot_item(item: dict) -> Optional[Dict]:
    """Parse a single Woot item from JSON. Returns dict with title, sale_price (buy_price), url, category, condition."""
    try:
        # Extract title (Title)
        title = item.get('Title') or item.get('title')
        if not title:
            return None
        title = str(title).strip()
        
        # Extract buy_price (SalePrice.Minimum if present, else SalePrice if numeric)
        buy_price = None
        sale_price_obj = item.get('SalePrice') or item.get('salePrice')
        
        if sale_price_obj:
            # Try SalePrice.Minimum first
            if isinstance(sale_price_obj, dict):
                if 'Minimum' in sale_price_obj:
                    try:
                        buy_price = float(sale_price_obj['Minimum'])
                    except (ValueError, TypeError):
                        pass
                elif 'minimum' in sale_price_obj:
                    try:
                        buy_price = float(sale_price_obj['minimum'])
                    except (ValueError, TypeError):
                        pass
            # If SalePrice is numeric directly
            elif isinstance(sale_price_obj, (int, float)):
                buy_price = float(sale_price_obj)
            # If SalePrice is a string, try to extract numeric value
            elif isinstance(sale_price_obj, str):
                price_match = re.search(r'[\d,]+\.?\d*', sale_price_obj.replace(',', ''))
                if price_match:
                    try:
                        buy_price = float(price_match.group())
                    except ValueError:
                        pass
        
        if not buy_price or buy_price <= 0:
            return None
        
        # Extract URL (Url)
        url = item.get('Url') or item.get('url')
        if url:
            url = str(url).strip()
            if not url.startswith('http'):
                url = 'https://www.woot.com' + url if url.startswith('/') else 'https://www.woot.com/' + url
        else:
            # Fallback URL construction
            item_id = item.get('Id') or item.get('id') or item.get('ItemId') or item.get('itemId')
            if item_id:
                url = f"https://www.woot.com/products/{item_id}"
            else:
                url = None
        
        # Extract condition if present
        condition = item.get('Condition') or item.get('condition') or item.get('ItemCondition') or item.get('itemCondition')
        if condition:
            condition = str(condition).strip()
        
        return {
            'title': title,
            'sale_price': buy_price,  # Keep 'sale_price' key for compatibility
            'buy_price': buy_price,   # Also add 'buy_price' for clarity
            'url': url,
            'category': None,  # Category comes from feed, not item
            'condition': condition
        }
    except Exception as e:
        log_debug(f"Error parsing Woot item: {e}")
        return None

def is_non_flippable(title: str, condition: Optional[str] = None, category: Optional[str] = None) -> bool:
    """Check if item should be filtered out (non-flippable)."""
    title_lower = title.lower()
    condition_lower = (condition or '').lower()
    category_lower = (category or '').lower()
    
    # Filter terms
    filter_terms = [
        'refurbished', 'refurb', 'open box', 'open-box', 'parts only', 'for parts',
        'parts/repair', 'broken', 'damaged', 'not working', 'accessories only',
        'accessory', 'bundle', 'lot of', 'multi pack', 'pack of', 'set of'
    ]
    
    combined_text = f"{title_lower} {condition_lower} {category_lower}"
    
    for term in filter_terms:
        if term in combined_text:
            return True
    
    return False

# ============================================================================
# EBAY OAUTH
# ============================================================================

# Token cache
_ebay_token_cache = None
_ebay_token_expires_at = 0

# eBay Finding API cache and rate limiting
LAST_EBAY_CALL_TS = 0.0
EBAY_CALLS_MADE = 0  # Track API calls made in this run
_cache_hit_count = 0  # Track cache hits in this run
_cache_miss_count = 0  # Track cache misses in this run

def ebay_env() -> str:
    """
    Normalize EBAY_ENV environment variable to "SBX" or "PRD".
    Accepts: SBX, SANDBOX, PRD, PROD, PRODUCTION (case-insensitive).
    Defaults to "SBX" if not set or unrecognized.
    """
    env = os.getenv("EBAY_ENV", "SBX").strip().upper()
    
    # Production variants
    if env in ("PRD", "PROD", "PRODUCTION"):
        return "PRD"
    
    # Sandbox variants (default)
    if env in ("SBX", "SANDBOX"):
        return "SBX"
    
    # Default to SBX for unrecognized values
    return "SBX"

def load_ebay_cache() -> Dict[str, Dict]:
    """Load eBay cache from disk."""
    if not os.path.exists(CACHE_FILE):
        return {}
    
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log_debug(f"Error loading cache: {e}")
        return {}

def save_ebay_cache(cache: Dict[str, Dict]):
    """Save eBay cache to disk."""
    # Create cache directory if it doesn't exist
    os.makedirs(CACHE_DIR, exist_ok=True)
    
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=2)
    except IOError as e:
        log_debug(f"Error saving cache: {e}")

def get_cache_ttl(status: str) -> int:
    """
    Get cache TTL in seconds based on status.
    OK (sold_count>0): 7 days
    NO_SOLD_COMPS: 24 hours
    THROTTLED/API_FAIL: 60 minutes
    """
    if status == 'OK':
        return 7 * 24 * 3600  # 7 days
    elif status == 'NO_SOLD_COMPS':
        return 24 * 3600  # 24 hours
    else:  # EBAY_THROTTLED, API_FAIL, BUDGET_EXHAUSTED
        return 60 * 60  # 60 minutes

def compute_percentile(data: List[float], percentile: float) -> float:
    """Compute percentile of a list of numbers. Returns 0.0 if empty."""
    if not data:
        return 0.0
    if len(data) == 1:
        return data[0]
    sorted_data = sorted(data)
    index = percentile / 100.0 * (len(sorted_data) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_data) - 1)
    if lower == upper:
        return sorted_data[lower]
    weight = index - lower
    return sorted_data[lower] * (1 - weight) + sorted_data[upper] * weight

def print_ebay_diagnostics():
    """Print eBay API configuration diagnostics for --one mode."""
    def redact_value(value: str, keep_start: int = 6, keep_end: int = 4) -> str:
        """Redact a value, keeping first keep_start and last keep_end chars."""
        if not value:
            return "(not set)"
        if len(value) <= keep_start + keep_end:
            return value  # Too short to redact
        return f"{value[:keep_start]}...{value[-keep_end:]}"
    
    print("=" * 80)
    print("eBay API Diagnostics")
    print("=" * 80)
    print()
    
    # EBAY_ENV
    env = ebay_env()
    print(f"EBAY_ENV: {env}")
    
    # Finding API base URL (for reference, even though we use Browse API)
    if env == "PRD":
        finding_api_url = "https://svcs.ebay.com/services/search/FindingService/v1"
    else:
        finding_api_url = "https://svcs.sandbox.ebay.com/services/search/FindingService/v1"
    print(f"Finding API base URL: {finding_api_url}")
    
    # Browse API base URL
    if env == "PRD":
        browse_api_url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    else:
        browse_api_url = "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"
    print(f"Browse API base URL: {browse_api_url}")
    
    # Token URL (OAuth)
    if env == "SBX":
        token_url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    else:
        token_url = "https://api.ebay.com/identity/v1/oauth2/token"
    print(f"OAuth Token URL: {token_url}")
    print()
    
    # EBAY_APP_ID (for Finding API, not used now but shown for reference)
    ebay_app_id = os.environ.get("EBAY_APP_ID")
    print(f"EBAY_APP_ID: {redact_value(ebay_app_id)}")
    
    # EBAY_CLIENT_ID (for OAuth)
    ebay_client_id = os.environ.get("EBAY_CLIENT_ID")
    print(f"EBAY_CLIENT_ID: {redact_value(ebay_client_id)}")
    
    # EBAY_CLIENT_SECRET (redacted, just show if set)
    ebay_client_secret = os.environ.get("EBAY_CLIENT_SECRET")
    if ebay_client_secret:
        print(f"EBAY_CLIENT_SECRET: {redact_value(ebay_client_secret)}")
    else:
        print("EBAY_CLIENT_SECRET: (not set)")
    
    # EBAY_MARKETPLACE_ID
    marketplace_id = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
    print(f"EBAY_MARKETPLACE_ID: {marketplace_id}")
    print()
    
    # OAuth token status
    global _ebay_token_cache, _ebay_token_expires_at
    if _ebay_token_cache and _ebay_token_expires_at > 0:
        current_time = time.time()
        if current_time < _ebay_token_expires_at:
            time_until_expiry = _ebay_token_expires_at - current_time
            expires_at_readable = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_ebay_token_expires_at))
            print(f"OAuth Token: Loaded (expires at: {expires_at_readable}, {int(time_until_expiry)}s remaining)")
        else:
            expires_at_readable = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(_ebay_token_expires_at))
            print(f"OAuth Token: Expired (expired at: {expires_at_readable})")
    else:
        print("OAuth Token: Not loaded")
    print()
    print("=" * 80)
    print()

def get_ebay_app_token() -> Optional[str]:
    """
    Get eBay OAuth app token using client credentials grant.
    Caches token in memory until expiry.
    Returns access_token string, or None on failure.
    """
    global _ebay_token_cache, _ebay_token_expires_at
    
    # Return cached token if still valid (with 60 second buffer)
    current_time = time.time()
    if _ebay_token_cache and current_time < (_ebay_token_expires_at - 60):
        return _ebay_token_cache
    
    client_id = os.environ.get('EBAY_CLIENT_ID', '').strip()
    client_secret = os.environ.get('EBAY_CLIENT_SECRET', '').strip()
    env = ebay_env()
    
    if not client_id or not client_secret:
        log_debug("Missing EBAY_CLIENT_ID or EBAY_CLIENT_SECRET environment variables")
        return None
    
    # Choose token URL based on environment
    if env == "SBX":
        token_url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
    else:
        token_url = "https://api.ebay.com/identity/v1/oauth2/token"
    
    # Prepare form data
    data = {
        'grant_type': 'client_credentials',
        'scope': 'https://api.ebay.com/oauth/api_scope'
    }
    
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    try:
        # Use HTTP Basic Auth: requests automatically encodes client_id:client_secret
        response = requests.post(
            token_url,
            data=urlencode(data),
            headers=headers,
            auth=(client_id, client_secret),
            timeout=TIMEOUT
        )
        
        if response.status_code != 200:
            error_preview = response.text[:300] if response.text else "(empty response)"
            log_debug(f"HTTP {response.status_code}: {error_preview}")
            return None
        
        try:
            json_data = response.json()
            access_token = json_data.get('access_token')
            expires_in = json_data.get('expires_in', 7200)  # Default 2 hours
            
            if access_token:
                # Cache token and expiration time
                _ebay_token_cache = access_token
                _ebay_token_expires_at = current_time + expires_in
                return access_token
            else:
                return None
        except json.JSONDecodeError as e:
            log_debug(f"JSON decode error: {e}")
            return None
            
    except requests.exceptions.RequestException as e:
        log_debug(f"Request error: {e}")
        return None
    except Exception as e:
        log_debug(f"Unexpected error: {e}")
        return None

# ============================================================================
# EBAY SEARCH
# ============================================================================

def search_ebay_sold_browse(query: str, no_retry: bool = False, original_title: Optional[str] = None) -> Dict[str, Any]:
    """
    Search eBay for sold listings using Browse API (item_summary/search with OAuth).
    Returns dict with keys: sold_count, avg_price, median_price, status.
    Status can be: 'SUCCESS', 'NO_SOLD_COMPS', 'API_FAIL', 'EBAY_THROTTLED', 'BUDGET_EXHAUSTED'
    
    Args:
        query: Search query string
        no_retry: If True, do not retry on rate limit errors (for safe testing)
    """
    global LAST_EBAY_CALL_TS, EBAY_CALLS_MADE, _cache_hit_count, _cache_miss_count
    
    def _ret_api_error(reason):
        print(f"[EBAY_RETURN] {reason}")
        return {
            'sold_count': 0,
            'avg_price': 0.0,
            'median_price': 0.0,
            'min_price': 0.0,
            'max_price': 0.0,
            'p25_price': 0.0,
            'p75_price': 0.0,
            'sample_items': [],
            'last_sold_date': None,
            'status': 'API_FAIL'
        }
    
    # Normalize query key for caching (include version to invalidate old cache)
    qkey = f"v{CACHE_VERSION}:{normalize_query(query)}"
    
    # Check disk cache first
    cache = load_ebay_cache()
    cache_hit = False
    if qkey in cache:
        cache_entry = cache[qkey]
        cache_ts = cache_entry.get('ts', 0)
        current_ts = time.time()
        age = current_ts - cache_ts
        
        cache_status = cache_entry.get('status', 'API_FAIL')
        ttl = get_cache_ttl(cache_status)
        
        if age < ttl:
            # Only treat as cache HIT for valid successful responses
            # Allow: OK (success), NO_SOLD_COMPS (valid search with no results)
            # Reject: API_FAIL, EBAY_THROTTLED, BUDGET_EXHAUSTED (treat as stale/invalid)
            valid_cache_statuses = {'OK', 'NO_SOLD_COMPS'}
            
            if cache_status in valid_cache_statuses:
                # Cache hit - return cached values
                print("[CACHE] hit")
                _cache_hit_count += 1
                status_map = {
                    'OK': 'SUCCESS',
                    'NO_SOLD_COMPS': 'NO_SOLD_COMPS',
                    'EBAY_THROTTLED': 'EBAY_THROTTLED',
                    'API_FAIL': 'API_FAIL',
                    'BUDGET_EXHAUSTED': 'BUDGET_EXHAUSTED',
                    'LOW_CONFIDENCE_COMPS': 'LOW_CONFIDENCE_COMPS'
                }
                # Build result dict with all fields (backward compatible)
                result = {
                    'sold_count': cache_entry.get('sold_count', 0),
                    'avg_price': cache_entry.get('avg', 0.0),
                    'median_price': cache_entry.get('median', 0.0),
                    'status': status_map.get(cache_status, 'API_FAIL')
                }
                # Add new fields if present
                if 'min' in cache_entry:
                    result['min_price'] = cache_entry.get('min', 0.0)
                if 'max' in cache_entry:
                    result['max_price'] = cache_entry.get('max', 0.0)
                if 'p25' in cache_entry:
                    result['p25_price'] = cache_entry.get('p25', 0.0)
                if 'p75' in cache_entry:
                    result['p75_price'] = cache_entry.get('p75', 0.0)
                if 'trimmed_count' in cache_entry:
                    result['trimmed_count'] = cache_entry.get('trimmed_count', 0)
                if 'expected_sale_price' in cache_entry:
                    result['expected_sale_price'] = cache_entry.get('expected_sale_price', 0.0)
                else:
                    # Fallback: use median if expected_sale_price not in cache
                    result['expected_sale_price'] = result.get('median_price', 0.0)
                if 'confidence_reason' in cache_entry:
                    result['confidence_reason'] = cache_entry.get('confidence_reason')
                if 'sample_items' in cache_entry:
                    result['sample_items'] = cache_entry.get('sample_items', [])
                if 'last_sold_date' in cache_entry:
                    result['last_sold_date'] = cache_entry.get('last_sold_date')
                cache_hit = True
                return result
            else:
                # Cache contains stale/invalid status - treat as miss and retry
                print(f"[CACHE] stale/invalid (status: {cache_status})")
                _cache_miss_count += 1
                # Fall through to network call path
    else:
        _cache_miss_count += 1
    
    # Check budget
    max_calls = int(os.environ.get('EBAY_MAX_CALLS', '8'))
    if EBAY_CALLS_MADE >= max_calls:
        result = {
            'sold_count': 0,
            'avg_price': 0.0,
            'median_price': 0.0,
            'min_price': 0.0,
            'max_price': 0.0,
            'p25_price': 0.0,
            'p75_price': 0.0,
            'sample_items': [],
            'last_sold_date': None,
            'status': 'BUDGET_EXHAUSTED'
        }
        # Save to cache with current timestamp
        cache[qkey] = {
            'ts': time.time(),
            'sold_count': 0,
            'avg': 0.0,
            'median': 0.0,
            'status': 'BUDGET_EXHAUSTED'
        }
        save_ebay_cache(cache)
        return result
    
    # Get OAuth token
    token = get_ebay_app_token()
    if not token:
        return _ret_api_error("OAuth token fetch failed")
    
    # Normalize environment and get base URLs
    env = ebay_env()
    
    # Choose API endpoint based on environment
    if env == "PRD":
        browse_base = "https://api.ebay.com"
        finding_base = "https://svcs.ebay.com/services/search/FindingService/v1"
        api_url = f"{browse_base}/buy/browse/v1/item_summary/search"
    else:
        browse_base = "https://api.sandbox.ebay.com"
        finding_base = "https://svcs.sandbox.ebay.com/services/search/FindingService/v1"
        api_url = f"{browse_base}/buy/browse/v1/item_summary/search"
    
    # Get marketplace ID (default to EBAY_US)
    marketplace_id = os.getenv("EBAY_MARKETPLACE_ID", "EBAY_US")
    
    # Get App ID (redacted for debug)
    ebay_app_id = os.environ.get("EBAY_APP_ID", "")
    def redact_app_id(value: str) -> str:
        if not value or len(value) <= 10:
            return "(not set)" if not value else "***"
        return f"{value[:6]}...{value[-4:]}"
    
    # Print debug line before request
    print(f"[EBAY_ENV] {env} [FINDING_BASE] {finding_base} [BROWSE_BASE] {browse_base} [APP_ID] {redact_app_id(ebay_app_id)}")
    
    log_debug(f"Searching eBay Browse API: {query}")
    
    # Prepare query parameters
    params = {
        'q': query,
        'limit': '20',
        'filter': 'soldItems'
    }
    
    # Prepare headers
    headers = {
        'Authorization': f'Bearer {token}',
        'X-EBAY-C-MARKETPLACE-ID': marketplace_id
    }
    
    # Increment call counter before making request
    EBAY_CALLS_MADE += 1
    
    # Retry logic for rate limiting (2 retries)
    max_retries = 2
    resp = None
    
    for attempt in range(max_retries + 1):  # initial attempt + 2 retries = 3 total
        # Enforce delay before request (only for actual network calls, not cache hits)
        # Only enforce on first attempt to avoid delaying retries
        if attempt == 0:
            current_time = time.time()
            elapsed = current_time - LAST_EBAY_CALL_TS
            
            # In --one mode (no_retry=True), use EBAY_MIN_DELAY_SEC if set, otherwise use MIN_DELAY_SEC
            min_delay_to_use = MIN_DELAY_SEC
            if no_retry:
                ebay_min_delay_str = os.getenv("EBAY_MIN_DELAY_SEC", "15")
                try:
                    min_delay_to_use = float(ebay_min_delay_str)
                except (ValueError, TypeError):
                    min_delay_to_use = 15.0  # fallback to default
            
            if elapsed < min_delay_to_use:
                sleep_time = min_delay_to_use - elapsed
                delay_source = "EBAY_MIN_DELAY_SEC" if no_retry else "MIN_DELAY_SEC"
                print(f"eBay: sleeping {sleep_time:.1f}s before request ({delay_source})")
                time.sleep(sleep_time)
        
        # Update timestamp right before sending request
        LAST_EBAY_CALL_TS = time.time()
        
        try:
            resp = requests.get(api_url, params=params, headers=headers, timeout=30)
        except Exception as e:
            print(f"[EBAY_API_ERROR] EXCEPTION: {type(e).__name__}: {e}")
            result = _ret_api_error(f"request exception: {e}")
            # Save API_FAIL to cache
            cache[qkey] = {
                'ts': time.time(),
                'sold_count': 0,
                'avg': 0.0,
                'median': 0.0,
                'status': 'API_FAIL'
            }
            save_ebay_cache(cache)
            return result
        
        # Check for rate limit errors (HTTP 429 or body indicates rate limit)
        rate_limit_detected = False
        
        if resp.status_code == 429:
            rate_limit_detected = True
        elif resp.status_code != 200:
            # Check response body for rate limit indicators
            try:
                body_text = resp.text.lower()
                if 'ratelimiter' in body_text or 'rate limit' in body_text or 'exceeded the number of times' in body_text:
                    rate_limit_detected = True
            except:
                pass
        
        # If rate limit detected, retry with backoff (unless no_retry is True)
        if rate_limit_detected:
            if no_retry:
                # No retry mode - exit immediately
                print("THROTTLED (cooldown). Exiting without retry.")
                result = {
                    'sold_count': 0,
                    'avg_price': 0.0,
                    'median_price': 0.0,
                    'min_price': 0.0,
                    'max_price': 0.0,
                    'p25_price': 0.0,
                    'p75_price': 0.0,
                    'sample_items': [],
                    'last_sold_date': None,
                    'status': 'EBAY_THROTTLED'
                }
                # Don't save to cache in no_retry mode (test mode)
                return result
            elif attempt < max_retries:
                backoff = RETRY_DELAYS[attempt]
                print(f"[EBAY_THROTTLED] backing off {backoff}s (attempt {attempt + 1}/{max_retries + 1})")
                time.sleep(backoff)
                continue
            else:
                # Final attempt failed - save throttled status to cache
                result = {
                    'sold_count': 0,
                    'avg_price': 0.0,
                    'median_price': 0.0,
                    'min_price': 0.0,
                    'max_price': 0.0,
                    'p25_price': 0.0,
                    'p75_price': 0.0,
                    'sample_items': [],
                    'last_sold_date': None,
                    'status': 'EBAY_THROTTLED'
                }
                cache[qkey] = {
                    'ts': time.time(),
                    'sold_count': 0,
                    'avg': 0.0,
                    'median': 0.0,
                    'status': 'EBAY_THROTTLED'
                }
                save_ebay_cache(cache)
                return result
        
        # If not rate limited, check for other HTTP errors
        if resp.status_code != 200:
            error_preview = resp.text[:300] if resp.text else "(empty response)"
            print(f"[EBAY_API_ERROR] HTTP {resp.status_code}")
            print(f"[EBAY_API_ERROR] BODY: {error_preview}")
            result = _ret_api_error(f"HTTP {resp.status_code}")
            # Save API_FAIL to cache
            cache[qkey] = {
                'ts': time.time(),
                'sold_count': 0,
                'avg': 0.0,
                'median': 0.0,
                'status': 'API_FAIL'
            }
            save_ebay_cache(cache)
            return result
        
        # If we get here, response is valid (not rate limited, HTTP 200) - break out of retry loop
        break
    
    # Parse JSON response (resp should be set at this point)
    try:
        json_data = resp.json()
        
        # Browse API response structure: itemSummaries[]
        item_summaries = json_data.get('itemSummaries', [])
        
        if not item_summaries or len(item_summaries) == 0:
            # No items found - valid response with no sold comps
            result = {
                'sold_count': 0,
                'avg_price': 0.0,
                'median_price': 0.0,
                'min_price': 0.0,
                'max_price': 0.0,
                'p25_price': 0.0,
                'p75_price': 0.0,
                'sample_items': [],
                'last_sold_date': None,
                'status': 'NO_SOLD_COMPS'
            }
            # Store in cache before returning
            cache[qkey] = {
                'ts': time.time(),
                'sold_count': 0,
                'avg': 0.0,
                'median': 0.0,
                'status': 'NO_SOLD_COMPS'
            }
            save_ebay_cache(cache)
            return result
        
        # Extract size from original title if filter-like (for size matching)
        woot_size = None
        if original_title and is_filter_like(original_title):
            woot_size = extract_filter_size(original_title)
        
        # Extract prices, titles, and dates from items (with size filtering for filters)
        sold_prices = []
        sample_items = []  # Store up to 3 sample items with title and price
        last_sold_date = None
        
        for item in item_summaries:
            try:
                # Size matching for filters: if Woot has size, only include eBay items with matching size
                item_title = item.get('title', '')
                if woot_size is not None:
                    # Woot has a size - only include items with matching size
                    item_size = extract_filter_size(item_title)
                    if item_size is None or item_size != woot_size:
                        continue  # Skip this item - size doesn't match
                
                price_obj = item.get('price')
                if price_obj and isinstance(price_obj, dict):
                    price_value = price_obj.get('value')
                    if price_value is not None:
                        # Convert to float (handle both string and number)
                        if isinstance(price_value, str):
                            price = float(price_value.replace(',', ''))
                        else:
                            price = float(price_value)
                        if price > 0:
                            sold_prices.append(price)
                            
                            # Extract title if available (for samples)
                            title = item.get('title', '')
                            if title and len(sample_items) < 3:
                                sample_items.append({
                                    'title': title,
                                    'price': price
                                })
                            
                            # Try to extract sold date (look for various date fields)
                            # Browse API may have: endDate, soldDate, availabilityDate
                            item_date = None
                            for date_field in ['endDate', 'soldDate', 'availabilityDate']:
                                date_val = item.get(date_field)
                                if date_val:
                                    item_date = date_val
                                    break
                            
                            # Track most recent date
                            if item_date:
                                if last_sold_date is None or item_date > last_sold_date:
                                    last_sold_date = item_date
            except (KeyError, ValueError, TypeError):
                continue
        
        if sold_prices:
            sold_count = len(sold_prices)
            
            # Outlier trimming using IQR method
            p25 = compute_percentile(sold_prices, 25)
            p75 = compute_percentile(sold_prices, 75)
            iqr = p75 - p25
            lower_bound = p25 - 1.5 * iqr
            upper_bound = p75 + 1.5 * iqr
            
            # Filter prices outside IQR bounds
            trimmed_prices = [p for p in sold_prices if lower_bound <= p <= upper_bound]
            trimmed_count = len(trimmed_prices)
            
            # Recompute statistics from trimmed prices
            if trimmed_prices:
                trimmed_avg = mean(trimmed_prices)
                trimmed_median = median(trimmed_prices) if trimmed_count > 1 else trimmed_prices[0]
                min_price = min(trimmed_prices)
                max_price = max(trimmed_prices)
            else:
                # All prices were outliers - use original values as fallback
                trimmed_avg = mean(sold_prices)
                trimmed_median = median(sold_prices) if sold_count > 1 else sold_prices[0]
                min_price = min(sold_prices)
                max_price = max(sold_prices)
                trimmed_count = sold_count
            
            # Original stats (before trimming) for reference
            avg_price = mean(sold_prices)
            median_price = median(sold_prices) if sold_count > 1 else sold_prices[0]
            
            # Use trimmed median as expected sale price (fallback to trimmed avg if needed)
            expected_sale_price = trimmed_median if trimmed_median else trimmed_avg
            
            # Confidence check: if trimmed_count < 5, mark as low confidence
            confidence_reason = None
            status = 'SUCCESS'
            if trimmed_count < 5:
                confidence_reason = f"LOW_CONFIDENCE_COMPS (trimmed_count={trimmed_count})"
                status = 'LOW_CONFIDENCE_COMPS'
            
            log_debug(f"Found {sold_count} sold items (trimmed: {trimmed_count}), expected price: ${expected_sale_price:.2f}")
            result = {
                'sold_count': sold_count,
                'avg_price': avg_price,
                'median_price': median_price,
                'min_price': min_price,
                'max_price': max_price,
                'p25_price': p25,
                'p75_price': p75,
                'trimmed_count': trimmed_count,
                'expected_sale_price': expected_sale_price,
                'confidence_reason': confidence_reason,
                'sample_items': sample_items[:3],  # Up to 3 samples
                'last_sold_date': last_sold_date,
                'status': status
            }
            # Store in cache before returning (full payload)
            cache[qkey] = {
                'ts': time.time(),
                'sold_count': sold_count,
                'avg': avg_price,
                'median': median_price,
                'min': min_price,
                'max': max_price,
                'p25': p25,
                'p75': p75,
                'trimmed_count': trimmed_count,
                'expected_sale_price': expected_sale_price,
                'confidence_reason': confidence_reason,
                'sample_items': sample_items[:3],
                'last_sold_date': last_sold_date,
                'status': 'OK' if status == 'SUCCESS' else status
            }
            save_ebay_cache(cache)
            return result
        else:
            # No prices extracted - treat as no sold comps
            result = {
                'sold_count': 0,
                'avg_price': 0.0,
                'median_price': 0.0,
                'min_price': 0.0,
                'max_price': 0.0,
                'p25_price': 0.0,
                'p75_price': 0.0,
                'sample_items': [],
                'last_sold_date': None,
                'status': 'NO_SOLD_COMPS'
            }
            # Store in cache before returning
            cache[qkey] = {
                'ts': time.time(),
                'sold_count': 0,
                'avg': 0.0,
                'median': 0.0,
                'min': 0.0,
                'max': 0.0,
                'p25': 0.0,
                'p75': 0.0,
                'sample_items': [],
                'last_sold_date': None,
                'status': 'NO_SOLD_COMPS'
            }
            save_ebay_cache(cache)
            return result
            
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        error_preview = resp.text[:300] if resp.text else "(empty response)"
        print(f"[EBAY_API_ERROR] Parse error: {e}")
        print(f"[EBAY_API_ERROR] BODY: {error_preview}")
        result = _ret_api_error(f"parse error: {e}")
        # Save API_FAIL to cache
        cache[qkey] = {
            'ts': time.time(),
            'sold_count': 0,
            'avg': 0.0,
            'median': 0.0,
            'status': 'API_FAIL'
        }
        save_ebay_cache(cache)
        return result

def search_ebay_sold(query: str, no_retry: bool = False, original_title: Optional[str] = None) -> Dict[str, Any]:
    """
    Search eBay for sold listings using Browse API (OAuth).
    Returns dict with keys: sold_count, avg_price, median_price, status.
    Status can be: 'SUCCESS', 'NO_SOLD_COMPS', 'API_FAIL', 'EBAY_THROTTLED', 'BUDGET_EXHAUSTED', 'LOW_CONFIDENCE_COMPS'
    
    Args:
        query: Search query string
        no_retry: If True, do not retry on rate limit errors (for safe testing)
        original_title: Original product title (for size matching with filters)
    """
    # Use Browse API by default (OAuth-based)
    return search_ebay_sold_browse(query, no_retry, original_title)

# ============================================================================
# CALCULATIONS AND FILTERING
# ============================================================================

def calculate_metrics(buy_price: float, expected_sale_price: float, trimmed_count: int) -> Dict:
    """Calculate arbitrage metrics using expected sale price (median) and determine PASS/FAIL."""
    # Calculate fees and costs based on expected sale price
    ebay_fees = expected_sale_price * EBAY_FEE_RATE
    total_costs = buy_price + SHIPPING_BUFFER + MISC_BUFFER + ebay_fees
    net_sale = expected_sale_price - ebay_fees - SHIPPING_BUFFER - MISC_BUFFER
    profit = net_sale - buy_price
    roi = profit / buy_price if buy_price > 0 else 0
    
    # Apply filters
    fails = []
    if profit < MIN_PROFIT:
        fails.append(f"Profit ${profit:.2f} < ${MIN_PROFIT}")
    if roi < MIN_ROI:
        fails.append(f"ROI {roi:.2%} < {MIN_ROI:.0%}")
    if trimmed_count < MIN_SOLD_COUNT:
        fails.append(f"Trimmed count {trimmed_count} < {MIN_SOLD_COUNT}")
    
    passed = len(fails) == 0
    fail_reason = "; ".join(fails) if fails else None
    
    return {
        'net_sale': net_sale,
        'profit': profit,
        'roi': roi,
        'passed': passed,
        'fail_reason': fail_reason
    }

# ============================================================================
# MAIN EXECUTION
# ============================================================================

def process_woot_mode(category: str = 'Tools', limit: int = 10, resume: bool = False, stream: bool = False) -> List[Dict]:
    """
    Process Woot deals (default mode). Returns list of result dictionaries.
    
    Args:
        category: Woot category to fetch
        limit: Max items to fetch
        resume: If True, only process items from deals.json where status='pending' and ebay_* is None
    """
    print("=" * 80)
    print("Woot → eBay Sold Arbitrage Checker")
    print("=" * 80)
    print()
    
    # Resume mode: load pending items from deals.json
    if resume:
        existing_deals = load_deals_from_file()
        pending_items = []
        for deal in existing_deals:
            if deal.get('status') == 'pending' and deal.get('ebay_sold_count') is None:
                # Convert deal dict back to format that can be processed
                pending_items.append({
                    'title': deal.get('title'),
                    'sale_price': deal.get('buy_price'),
                    'url': deal.get('url'),
                    'category': deal.get('category'),
                    'condition': None  # May not be in saved deals
                })
        if not pending_items:
            print("No pending items found in deals.json")
            return []
        print(f"Resume mode: Processing {len(pending_items)} pending items from deals.json")
        print()
        woot_items = pending_items
        fetched_count = len(woot_items)
    else:
        # Normal mode: Fetch Woot deals
        print(f"Fetching Woot deals (category: {category}, limit: {limit})...")
        woot_items = fetch_woot_deals(category=category, limit=limit)
        
        if not woot_items:
            print("ERROR: Could not fetch Woot deals from API!")
            return []
        
        fetched_count = len(woot_items)
        print(f"Fetched {fetched_count} items from Woot")
        print()
    
    # Initialize counters
    filtered_nonflippable_count = 0
    skipped_low_asp_count = 0
    skipped_keyword_count = 0
    analyzed_count = 0
    ebay_ok_count = 0
    ebay_no_sold_comps_count = 0
    ebay_throttled_count = 0
    ebay_budget_exhausted_count = 0
    ebay_api_fail_count = 0
    failed_criteria_count = 0
    passed_count = 0
    cache_hit_count = 0
    cache_miss_count = 0
    ebay_calls_made = 0
    
    # Reset global cache counters for this run
    global _cache_hit_count, _cache_miss_count, EBAY_CALLS_MADE
    _cache_hit_count = 0
    _cache_miss_count = 0
    EBAY_CALLS_MADE = 0
    
    results = []
    analyzed_index = 0
    
    # Print stream header if in stream mode
    if stream:
        print("  Title" + " " * 54 + "| Buy      | Profit    | ROI    | Status")
        print("-" * 92)
    
    # Process each Woot item
    for idx, item in enumerate(woot_items, 1):
        # In resume mode, items are already parsed
        if resume:
            title = item['title']
            sale_price = item['sale_price']
            url = item['url']
            item_category = item.get('category')
            condition = item.get('condition')
        else:
            # Parse Woot item
            parsed_item = parse_woot_item(item)
            if not parsed_item:
                log_debug(f"Skipping malformed item {idx}")
                continue
            
            title = parsed_item['title']
            sale_price = parsed_item['sale_price']
            url = parsed_item['url']
            item_category = parsed_item.get('category')
            condition = parsed_item.get('condition')
        
        # Filter out non-flippable items
        if is_non_flippable(title, condition, item_category):
            log_debug(f"Filtered out non-flippable: {title[:50]}")
            filtered_nonflippable_count += 1
            continue
        
        # Filter out low ASP items (buy_price < $20)
        if sale_price < 20.00:
            log_debug(f"Skipped low ASP item (<$20): {title}")
            skipped_low_asp_count += 1
            continue
        
        # Filter out non-arbitrage categories (keyword denylist)
        title_lower = title.lower()
        denylist_keywords = ['baby', 'kids', 'toddler', 'infant', 'socks', 'clothing', 'shirt', 'bodysuit', 'underwear']
        if any(keyword in title_lower for keyword in denylist_keywords):
            log_debug(f"Skipped non-arbitrage category: {title}")
            skipped_keyword_count += 1
            continue
        
        # Build query confidence score
        confidence_info = build_query_confidence(title)
        query_confidence = confidence_info['confidence']
        confidence_reasons = confidence_info['reasons']
        normalized_query = confidence_info['query']
        
        # Print confidence info (suppress in stream mode to reduce noise)
        if not stream:
            reasons_str = ", ".join(confidence_reasons) if confidence_reasons else "none"
            print(f" [CONF] {query_confidence} ({reasons_str}) query='{normalized_query[:50]}'")
        
        # Skip LOW confidence items only if buy_price < 30
        LOW_CONFIDENCE_PRICE_THRESHOLD = 30.0
        if query_confidence == "low" and sale_price < LOW_CONFIDENCE_PRICE_THRESHOLD:
            results.append({
                'title': title,
                'buy_price': sale_price,
                'url': url,
                'category': item_category,
                'confidence': query_confidence,
                'confidence_reasons': confidence_reasons,
                'ebay_sold_count': None,
                'ebay_avg_sold_price': None,
                'ebay_median_sold_price': None,
                'ebay_trimmed_count': None,
                'ebay_expected_sale_price': None,
                'ebay_min_price': None,
                'ebay_max_price': None,
                'ebay_p25_price': None,
                'ebay_p75_price': None,
                'ebay_sample_items': None,
                'ebay_last_sold_date': None,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'status': 'failed',
                'reason': 'LOW_CONFIDENCE',
                'fail_reason': 'Low confidence query + low price'
            })
            if stream:
                print(f"✗ {title[:60]:<60} | ${sale_price:>7.2f} | SKIPPED (low confidence)")
            else:
                print(f" → Skipped (low confidence + low price)")
            continue
        
        # Size check for filters: if filter-like but no size, mark as NEEDS_SIZE
        if is_filter_like(title):
            woot_size = extract_filter_size(title)
            if woot_size is None:
                results.append({
                    'title': title,
                    'buy_price': sale_price,
                    'url': url,
                    'category': item_category,
                    'ebay_sold_count': None,
                    'ebay_avg_sold_price': None,
                    'ebay_median_sold_price': None,
                    'ebay_trimmed_count': None,
                    'ebay_expected_sale_price': None,
                    'ebay_min_price': None,
                    'ebay_max_price': None,
                    'ebay_p25_price': None,
                    'ebay_p75_price': None,
                    'ebay_sample_items': None,
                    'ebay_last_sold_date': None,
                    'confidence_reason': None,
                    'net_sale': 0,
                    'profit': 0,
                    'roi': 0,
                    'passed': False,
                    'status': 'failed',
                    'reason': 'NEEDS_SIZE',
                    'fail_reason': 'Filter product requires size specification'
                })
                if stream:
                    print(f"✗ {title[:60]:<60} | ${sale_price:>7.2f} | FAIL (needs size)")
                else:
                    print(f" → FAIL: Needs size specification")
                continue
        
        # Item passed all filters - analyze it
        analyzed_count += 1
        analyzed_index += 1
        if not stream:
            print(f"[{analyzed_index}] {title[:60]}... | ${sale_price:.2f}", end='')
        
        # Search eBay sold listings using API (pass original title for size matching)
        # Use normalized query from confidence_info for consistency
        search_query = clean_title_for_ebay(title)
        ebay_result = search_ebay_sold(search_query, original_title=title)
        
        # Handle different statuses
        if ebay_result['status'] == 'SUCCESS':
            ebay_ok_count += 1
            # Continue to metrics calculation below
        elif ebay_result['status'] == 'NO_SOLD_COMPS':
            ebay_no_sold_comps_count += 1
            results.append({
                'title': title,
                'buy_price': sale_price,
                'url': url,
                'category': item_category,
                'ebay_sold_count': 0,
                'ebay_avg_sold_price': 0,
                'ebay_median_sold_price': 0,
                'ebay_min_price': None,
                'ebay_max_price': None,
                'ebay_p25_price': None,
                'ebay_p75_price': None,
                'ebay_sample_items': [],
                'ebay_last_sold_date': None,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'status': 'failed',
                'reason': 'NO_SOLD_COMPS',
                'fail_reason': 'No sold comps found (valid search)'
            })
            if stream:
                print(f"✗ {title[:60]:<60} | ${sale_price:>7.2f} | NO_SOLD_COMPS")
            else:
                print(f" → No sold comps found (valid search)")
            continue
        elif ebay_result['status'] == 'EBAY_THROTTLED':
            ebay_throttled_count += 1
            results.append({
                'title': title,
                'buy_price': sale_price,
                'url': url,
                'category': item_category,
                'ebay_sold_count': None,
                'ebay_avg_sold_price': None,
                'ebay_median_sold_price': None,
                'ebay_min_price': None,
                'ebay_max_price': None,
                'ebay_p25_price': None,
                'ebay_p75_price': None,
                'ebay_sample_items': None,
                'ebay_last_sold_date': None,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'status': 'pending',
                'reason': 'EBAY_THROTTLED',
                'fail_reason': 'eBay throttled; try again in a few minutes'
            })
            if stream:
                print(f"✗ {title[:60]:<60} | ${sale_price:>7.2f} | THROTTLED (stopping)")
            else:
                print(f" → eBay throttled; stopping scan early (cooldown). Run again later.")
            # Break immediately - do not process remaining items
            break
        elif ebay_result['status'] == 'BUDGET_EXHAUSTED':
            ebay_budget_exhausted_count += 1
            results.append({
                'title': title,
                'buy_price': sale_price,
                'url': url,
                'category': item_category,
                'ebay_sold_count': None,
                'ebay_avg_sold_price': None,
                'ebay_median_sold_price': None,
                'ebay_min_price': None,
                'ebay_max_price': None,
                'ebay_p25_price': None,
                'ebay_p75_price': None,
                'ebay_sample_items': None,
                'ebay_last_sold_date': None,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'status': 'pending',
                'reason': 'BUDGET_EXHAUSTED',
                'fail_reason': 'eBay budget exhausted; run again later'
            })
            if stream:
                print(f"✗ {title[:60]:<60} | ${sale_price:>7.2f} | BUDGET_EXHAUSTED")
            else:
                print(f" → eBay budget exhausted; run again later")
            continue
        elif ebay_result['status'] == 'API_FAIL':
            ebay_api_fail_count += 1
            results.append({
                'title': title,
                'buy_price': sale_price,
                'url': url,
                'category': item_category,
                'ebay_sold_count': 0,
                'ebay_avg_sold_price': 0,
                'ebay_median_sold_price': 0,
                'ebay_min_price': None,
                'ebay_max_price': None,
                'ebay_p25_price': None,
                'ebay_p75_price': None,
                'ebay_sample_items': [],
                'ebay_last_sold_date': None,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'status': 'failed',
                'reason': 'API_FAIL',
                'fail_reason': 'eBay API lookup failed'
            })
            if stream:
                print(f"✗ {title[:60]:<60} | ${sale_price:>7.2f} | API_FAIL")
            else:
                print(f" → FAIL: eBay API lookup failed")
            continue
        
        # Check for LOW_CONFIDENCE_COMPS status
        if ebay_result['status'] == 'LOW_CONFIDENCE_COMPS':
            results.append({
                'title': title,
                'buy_price': sale_price,
                'url': url,
                'category': item_category,
                'ebay_sold_count': ebay_result.get('sold_count', 0),
                'ebay_avg_sold_price': ebay_result.get('avg_price', 0.0),
                'ebay_median_sold_price': ebay_result.get('median_price', 0.0),
                'ebay_trimmed_count': ebay_result.get('trimmed_count', 0),
                'ebay_expected_sale_price': ebay_result.get('expected_sale_price', 0.0),
                'ebay_min_price': ebay_result.get('min_price'),
                'ebay_max_price': ebay_result.get('max_price'),
                'ebay_p25_price': ebay_result.get('p25_price'),
                'ebay_p75_price': ebay_result.get('p75_price'),
                'ebay_sample_items': ebay_result.get('sample_items', []),
                'ebay_last_sold_date': ebay_result.get('last_sold_date'),
                'confidence_reason': ebay_result.get('confidence_reason'),
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'status': 'failed',
                'reason': 'LOW_CONFIDENCE_COMPS',
                'fail_reason': ebay_result.get('confidence_reason', 'Low confidence comps')
            })
            if stream:
                print(f"✗ {title[:60]:<60} | ${sale_price:>7.2f} | LOW_CONFIDENCE_COMPS")
            else:
                print(f" → FAIL: {ebay_result.get('confidence_reason', 'Low confidence comps')}")
            continue
        
        # SUCCESS status - proceed with metrics calculation
        sold_count = ebay_result['sold_count']
        expected_sale_price = ebay_result.get('expected_sale_price', ebay_result.get('median_price', 0.0))
        trimmed_count = ebay_result.get('trimmed_count', sold_count)
        
        if not stream:
            print(f" → eBay: {trimmed_count} trimmed from {sold_count} @ ${expected_sale_price:.2f} expected")
        
        # Calculate metrics using expected_sale_price (median)
        metrics = calculate_metrics(sale_price, expected_sale_price, trimmed_count)
        
        result = {
            'title': title,
            'buy_price': sale_price,
            'url': url,
            'category': item_category,
            'ebay_sold_count': sold_count,
            'ebay_avg_sold_price': ebay_result.get('avg_price', 0.0),
            'ebay_median_sold_price': ebay_result.get('median_price', 0.0),
            'ebay_trimmed_count': trimmed_count,
            'ebay_expected_sale_price': expected_sale_price,
            'ebay_min_price': ebay_result.get('min_price'),
            'ebay_max_price': ebay_result.get('max_price'),
            'ebay_p25_price': ebay_result.get('p25_price'),
            'ebay_p75_price': ebay_result.get('p75_price'),
            'ebay_sample_items': ebay_result.get('sample_items', []),
            'ebay_last_sold_date': ebay_result.get('last_sold_date'),
            'confidence_reason': ebay_result.get('confidence_reason'),
            **metrics,
            'status': 'passed' if metrics['passed'] else 'failed',
            'reason': None  # No specific reason for items that reached metrics calculation
        }
        results.append(result)
        
        if metrics['passed']:
            passed_count += 1
        else:
            failed_criteria_count += 1
        
        # Print result immediately in stream mode
        if stream:
            status_symbol = "✓" if metrics['passed'] else "✗"
            status_text = "PASS" if metrics['passed'] else "FAIL"
            print(f"{status_symbol} {title[:60]:<60} | ${sale_price:>7.2f} | ${metrics['profit']:>7.2f} | {metrics['roi']:>6.1%} | {status_text}")
        else:
            status = "PASS" if metrics['passed'] else "FAIL"
            print(f" → {status}: Profit ${metrics['profit']:.2f}, ROI {metrics['roi']:.2%}")
    
    # Check if any items reached eBay analysis
    if analyzed_count == 0:
        print("=" * 80)
        print("RESULTS")
        print("=" * 80)
        print()
        print("No items reached eBay analysis. Try raising limit or changing category.")
        print()
        print("Summary:")
        print(f"  Total Scanned: {fetched_count}")
        print(f"  Filtered (non-flippable): {filtered_nonflippable_count}")
        print(f"  Skipped (<$20): {skipped_low_asp_count}")
        print(f"  Skipped (keyword denylist): {skipped_keyword_count}")
        print(f"  Analyzed: {analyzed_count}")
        return []
    
    # Output results (skip detailed output in stream mode since deals were already printed)
    if not stream:
        print("=" * 80)
        print("RESULTS")
        print("=" * 80)
        print()
        
        # Separate PASS and FAIL
        passed_results = [r for r in results if r['passed']]
        failed_results = [r for r in results if not r['passed']]
        
        # Sort PASS by ROI descending
        passed_results.sort(key=lambda x: x['roi'], reverse=True)
        
        # Print PASS items
        if passed_results:
            print(f"✓ PASSED ({len(passed_results)} items):")
            print("-" * 80)
            for result in passed_results:
                print(f"Title: {result['title']}")
                print(f"  Buy Price (Woot): ${result['buy_price']:.2f}")
                print(f"  Avg Sold Price: ${result['ebay_avg_sold_price']:.2f}")
                print(f"  Profit: ${result['profit']:.2f}")
                print(f"  ROI: {result['roi']:.2%}")
                print(f"  Sold Count: {result['ebay_sold_count']}")
                print(f"  URL: {result['url']}")
                print()
        else:
            print("No items passed the arbitrage criteria.")
            print()
    else:
        # In stream mode, just print a separator before summary
        print()
    
    # Capture cache and call counters before returning
    cache_hit_count = _cache_hit_count
    cache_miss_count = _cache_miss_count
    ebay_calls_made = EBAY_CALLS_MADE
    
    # Print summary with all counters
    print("Summary:")
    print(f"  Total Scanned: {fetched_count}")
    print(f"  Filtered (non-flippable): {filtered_nonflippable_count}")
    print(f"  Skipped (<$20): {skipped_low_asp_count}")
    print(f"  Skipped (keyword denylist): {skipped_keyword_count}")
    print(f"  Analyzed: {analyzed_count}")
    print(f"  Cache Hit: {cache_hit_count}")
    print(f"  Cache Miss: {cache_miss_count}")
    print(f"  eBay Calls Made: {ebay_calls_made}")
    print(f"  eBay OK: {ebay_ok_count}")
    print(f"  No Sold Comps: {ebay_no_sold_comps_count}")
    print(f"  eBay Throttled: {ebay_throttled_count}")
    print(f"  Budget Exhausted: {ebay_budget_exhausted_count}")
    print(f"  eBay API Failed: {ebay_api_fail_count}")
    print(f"  Failed Criteria: {failed_criteria_count}")
    print(f"  Passed: {passed_count}")
    
    return results

def process_woot_mode_with_save(category: str = 'Tools', limit: int = 10, resume: bool = False, stream: bool = False):
    """Wrapper that runs process_woot_mode and saves results to file."""
    results = process_woot_mode(category=category, limit=limit, resume=resume, stream=stream)
    save_deals_to_file(results)

def process_watchlist_mode():
    """Process watchlist.txt (legacy mode)."""
    print("=" * 80)
    print("Amazon/Walmart → eBay Sold Arbitrage Checker MVP")
    print("=" * 80)
    print()
    
    # Read watchlist
    try:
        with open('watchlist.txt', 'r', encoding='utf-8') as f:
            urls = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print("ERROR: watchlist.txt not found!")
        print("Please create watchlist.txt with one product URL per line.")
        return
    except Exception as e:
        print(f"ERROR reading watchlist.txt: {e}")
        return
    
    if not urls:
        print("ERROR: watchlist.txt is empty!")
        return
    
    print(f"Loaded {len(urls)} URLs from watchlist.txt")
    
    # Check for ONLY_WALMART env var
    only_walmart = os.environ.get('ONLY_WALMART', '0') == '1'
    if only_walmart:
        print("ONLY_WALMART=1: Skipping non-Walmart URLs")
        urls = [url for url in urls if 'walmart' in urlparse(url).netloc.lower()]
        print(f"Processing {len(urls)} Walmart URLs\n")
    else:
        print()
    
    results = []
    
    # Process each URL
    for idx, url in enumerate(urls, 1):
        print(f"[{idx}/{len(urls)}] Processing: {url}")
        print()
        
        # Parse product
        product_data = parse_product(url, idx)
        if not product_data:
            results.append({
                'url': url,
                'title': None,
                'buy_price': 0,
                'store': 'Unknown',
                'ebay_sold_count': 0,
                'ebay_avg_sold_price': 0,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'fail_reason': 'Failed to fetch page or unknown domain'
            })
            print(f"  → FAIL: Failed to fetch page or unknown domain\n")
            continue
        
        title, buy_price, store, parse_fail_reason = product_data
        
        # Print RAW PARSE line even if parse failed
        if title and buy_price > 0:
            print(f"  RAW PARSE: store={store} title=\"{title}\" buy_price={buy_price:.2f}")
        else:
            print(f"  RAW PARSE: store={store} title=\"{title or 'N/A'}\" buy_price={buy_price:.2f}")
            if parse_fail_reason:
                print(f"  → Parse failed: {parse_fail_reason}")
        
        # If blocked/consent or parse failed, add result and continue
        if parse_fail_reason:
            results.append({
                'url': url,
                'title': title or None,
                'buy_price': buy_price,
                'store': store,
                'ebay_sold_count': 0,
                'ebay_avg_sold_price': 0,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'fail_reason': parse_fail_reason
            })
            print()
            continue
        
        # If title and price are present, proceed to eBay search
        if title and buy_price > 0:
            # Search eBay
            search_query = clean_title_for_ebay(title)
            ebay_result = search_ebay_sold(search_query)
            
            # Handle different statuses
            if ebay_result['status'] == 'SUCCESS':
                # Continue to metrics calculation below
                pass
            elif ebay_result['status'] == 'NO_SOLD_COMPS':
                results.append({
                    'url': url,
                    'title': title,
                    'buy_price': buy_price,
                    'store': store,
                    'ebay_sold_count': 0,
                    'ebay_avg_sold_price': 0,
                    'net_sale': 0,
                    'profit': 0,
                    'roi': 0,
                    'passed': False,
                    'fail_reason': 'No sold comps found (valid search)'
                })
                print(f"  → No sold comps found (valid search)\n")
                continue
            elif ebay_result['status'] == 'EBAY_THROTTLED':
                results.append({
                    'url': url,
                    'title': title,
                    'buy_price': buy_price,
                    'store': store,
                    'ebay_sold_count': 0,
                    'ebay_avg_sold_price': 0,
                    'net_sale': 0,
                    'profit': 0,
                    'roi': 0,
                    'passed': False,
                    'fail_reason': 'eBay throttled; try again in a few minutes'
                })
                print(f"  → eBay throttled; try again in a few minutes\n")
                continue
            elif ebay_result['status'] == 'BUDGET_EXHAUSTED':
                results.append({
                    'url': url,
                    'title': title,
                    'buy_price': buy_price,
                    'store': store,
                    'ebay_sold_count': 0,
                    'ebay_avg_sold_price': 0,
                    'net_sale': 0,
                    'profit': 0,
                    'roi': 0,
                    'passed': False,
                    'fail_reason': 'eBay budget exhausted; run again later'
                })
                print(f"  → eBay budget exhausted; run again later\n")
                continue
            elif ebay_result['status'] == 'API_FAIL':
                results.append({
                    'url': url,
                    'title': title,
                    'buy_price': buy_price,
                    'store': store,
                    'ebay_sold_count': 0,
                    'ebay_avg_sold_price': 0,
                    'net_sale': 0,
                    'profit': 0,
                    'roi': 0,
                    'passed': False,
                    'fail_reason': 'eBay API lookup failed'
                })
                print(f"  → FAIL: eBay API lookup failed\n")
                continue
            
            # SUCCESS status - proceed with metrics calculation
            sold_count = ebay_result['sold_count']
            avg_sold_price = ebay_result['avg_price']
            median_sold_price = ebay_result['median_price']
            
            print(f"  → eBay: {sold_count} sold items, avg price: ${avg_sold_price:.2f}")
            
            # Calculate metrics (watchlist mode - use avg for backward compatibility, or expected_sale_price if available)
            expected_price = ebay_result.get('expected_sale_price', avg_sold_price)
            trimmed_count = ebay_result.get('trimmed_count', sold_count)
            metrics = calculate_metrics(buy_price, expected_price, trimmed_count)
            
            result = {
                'url': url,
                'title': title,
                'buy_price': buy_price,
                'store': store,
                'ebay_sold_count': sold_count,
                'ebay_avg_sold_price': avg_sold_price,
                **metrics
            }
            results.append(result)
            
            status = "PASS" if metrics['passed'] else "FAIL"
            print(f"  → {status}: Profit ${metrics['profit']:.2f}, ROI {metrics['roi']:.2%}")
            if metrics['fail_reason']:
                print(f"    Reason: {metrics['fail_reason']}")
        else:
            # Title or price missing
            results.append({
                'url': url,
                'title': title or None,
                'buy_price': buy_price,
                'store': store,
                'ebay_sold_count': 0,
                'ebay_avg_sold_price': 0,
                'net_sale': 0,
                'profit': 0,
                'roi': 0,
                'passed': False,
                'fail_reason': parse_fail_reason or 'Title or price missing'
            })
            print(f"  → FAIL: Title or price missing\n")
        
        print()
    
    # Sort results: PASS items by ROI descending, then FAIL items
    passed_results = [r for r in results if r['passed']]
    failed_results = [r for r in results if not r['passed']]
    
    passed_results.sort(key=lambda x: x['roi'], reverse=True)
    failed_results.sort(key=lambda x: x['roi'], reverse=True)
    
    sorted_results = passed_results + failed_results
    
    # Print summary
    print("=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)
    print()
    
    if passed_results:
        print(f"✓ PASSED ({len(passed_results)} items):")
        print("-" * 80)
        for result in passed_results:
            print(f"Title: {result['title'][:70]}")
            print(f"  Buy: ${result['buy_price']:.2f} @ {result['store']} | URL: {result['url']}")
            print(f"  eBay: {result['ebay_sold_count']} sold @ ${result['ebay_avg_sold_price']:.2f} avg")
            print(f"  Net Sale: ${result['net_sale']:.2f} | Profit: ${result['profit']:.2f} | ROI: {result['roi']:.2%}")
            print()
    
    if failed_results:
        print(f"✗ FAILED ({len(failed_results)} items):")
        print("-" * 80)
        for result in failed_results:
            print(f"Title: {result['title'] or 'N/A'}")
            print(f"  Buy: ${result['buy_price']:.2f} @ {result['store']} | URL: {result['url']}")
            if result['ebay_sold_count'] > 0:
                print(f"  eBay: {result['ebay_sold_count']} sold @ ${result['ebay_avg_sold_price']:.2f} avg")
                print(f"  Net Sale: ${result['net_sale']:.2f} | Profit: ${result['profit']:.2f} | ROI: {result['roi']:.2%}")
            print(f"  Reason: {result['fail_reason']}")
            print()

def save_deals_to_file(results: List[Dict], output_file: str = 'data/deals.json', merge: bool = True):
    """
    Save scan results to JSON file.
    If merge=True, update existing deals by matching on url and merge new results.
    Includes cache version to invalidate old results.
    """
    os.makedirs('data', exist_ok=True)
    
    # Add cache version metadata to results (for validation)
    # Store as a wrapper dict to maintain backward compatibility
    output_data = {
        'version': CACHE_VERSION,
        'deals': results
    }
    
    if merge and os.path.exists(output_file):
        # Load existing deals (will return empty list if version mismatch)
        existing_deals = load_deals_from_file(output_file)
        if existing_deals:
            # Create lookup by url
            existing_by_url = {deal.get('url'): deal for deal in existing_deals if deal.get('url')}
            
            # Update or add results
            for result in results:
                url = result.get('url')
                if url and url in existing_by_url:
                    # Update existing deal with new data
                    existing_by_url[url].update(result)
                else:
                    # Add new deal
                    existing_by_url[url] = result
            
            # Convert back to list and update output_data
            results = list(existing_by_url.values())
            output_data['deals'] = results
    
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=2)
        log_debug(f"Saved {len(results)} results to {output_file} (version {CACHE_VERSION})")
    except IOError as e:
        print(f"Error saving results to {output_file}: {e}")

def load_deals_from_file(input_file: str = 'data/deals.json') -> List[Dict]:
    """
    Load scan results from JSON file.
    Validates cache version and returns empty list if version mismatch.
    """
    if not os.path.exists(input_file):
        print(f"No deals file found at {input_file}")
        return []
    
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Handle both old format (direct list) and new format (dict with version)
        deals = []
        if isinstance(data, list):
            # Old format - return as-is (backward compatibility)
            deals = data
            print(f"Loaded {len(deals)} deals from {input_file} (legacy format)")
        elif isinstance(data, dict):
            file_version = data.get('version', 0)
            if file_version != CACHE_VERSION:
                print(f"WARNING: {input_file} version ({file_version}) != current ({CACHE_VERSION}). Cache invalidated.")
                print("Please run 'scan' again to regenerate results with current logic.")
                return []
            deals = data.get('deals', [])
            print(f"Loaded {len(deals)} deals from {input_file}")
        else:
            print(f"ERROR: Invalid format in {input_file}")
            return []
        
        if len(deals) == 0:
            print(f"0 deals in file")
        return deals
    except (json.JSONDecodeError, IOError) as e:
        print(f"ERROR reading {input_file}: {e}")
        return []

def view_deals(top: int = 20, only_status: Optional[str] = None, show_failed: bool = False, show_throttled: bool = False, raw: bool = False):
    """View saved deals from JSON file. Shows PASSED, FAILED, and PENDING sections."""
    print("=" * 80)
    print("View Saved Deals")
    print("=" * 80)
    print()
    
    deals = load_deals_from_file()
    if not deals:
        if os.path.exists('data/deals.json'):
            print("File exists but contains no deals.")
        sys.exit(1)
        return
    
    # Handle --raw mode: bypass all filters and show first 10 deals
    if raw:
        print(f"[RAW MODE] Showing first 10 deals (no filters):")
        print("-" * 80)
        for i, deal in enumerate(deals[:10], 1):
            print(f"{i}. {deal.get('title', 'N/A')[:70]}")
            print(f"   Buy: ${deal.get('buy_price', 0):.2f} | Profit: ${deal.get('profit', 0):.2f} | ROI: {deal.get('roi', 0):.2%}")
            print(f"   Status: {deal.get('status', 'unknown')} | URL: {deal.get('url', 'N/A')}")
            print()
        return
    
    # Separate deals into PASSED, FAILED, PENDING
    passed_deals = []
    failed_deals = []
    pending_deals = []
    
    for deal in deals:
        status = deal.get('status')
        if status == 'pending':
            pending_deals.append(deal)
        elif status == 'passed' or (status is None and deal.get('passed', False)):
            passed_deals.append(deal)
        else:
            failed_deals.append(deal)
    
    # Sort each section by ROI descending
    passed_deals.sort(key=lambda x: x.get('roi', 0), reverse=True)
    failed_deals.sort(key=lambda x: x.get('roi', 0), reverse=True)
    pending_deals.sort(key=lambda x: x.get('buy_price', 0), reverse=True)
    
    # Limit to top N for each section
    if top > 0:
        passed_deals = passed_deals[:top]
        failed_deals = failed_deals[:top]
        pending_deals = pending_deals[:top]
    
    # Filter sections based on flags
    if only_status:
        if only_status.lower() == 'passed':
            failed_deals = []
            pending_deals = []
        elif only_status.lower() == 'failed':
            passed_deals = []
            pending_deals = []
        elif only_status.lower() == 'pending':
            passed_deals = []
            failed_deals = []
        else:
            print(f"ERROR: Invalid status '{only_status}'. Use 'passed', 'failed', or 'pending'.")
            return
    
    # Print PASSED section
    if passed_deals:
        print(f"✓ PASSED ({len(passed_deals)} items):")
        print("-" * 80)
        for deal in passed_deals:
            print(f"Title: {deal.get('title', 'N/A')[:70]}")
            print(f"  Buy: ${deal.get('buy_price', 0):.2f} | URL: {deal.get('url', 'N/A')}")
            ebay_count = deal.get('ebay_sold_count')
            if ebay_count is not None and ebay_count > 0:
                print(f"  eBay: {ebay_count} sold @ ${deal.get('ebay_avg_sold_price', 0):.2f} avg")
                print(f"  Net Sale: ${deal.get('net_sale', 0):.2f} | Profit: ${deal.get('profit', 0):.2f} | ROI: {deal.get('roi', 0):.2%}")
            print()
    
    # Print FAILED section (only if explicitly requested)
    if failed_deals and (show_failed or (only_status and only_status.lower() == 'failed')):
        print(f"✗ FAILED ({len(failed_deals)} items):")
        print("-" * 80)
        for deal in failed_deals:
            print(f"Title: {deal.get('title', 'N/A')[:70]}")
            print(f"  Buy: ${deal.get('buy_price', 0):.2f} | URL: {deal.get('url', 'N/A')}")
            fail_reason = deal.get('fail_reason') or deal.get('reason', 'Unknown')
            print(f"  Reason: {fail_reason}")
            print()
    
    # Print PENDING section (only if explicitly requested)
    if pending_deals and (show_throttled or (only_status and only_status.lower() == 'pending')):
        print(f"⏳ PENDING ({len(pending_deals)} items):")
        print("-" * 80)
        for deal in pending_deals:
            print(f"Title: {deal.get('title', 'N/A')[:70]}")
            print(f"  Buy: ${deal.get('buy_price', 0):.2f} | URL: {deal.get('url', 'N/A')}")
            reason = deal.get('reason', 'Unknown')
            print(f"  Reason: {reason}")
            print()
    
    # Print summary if nothing shown
    if not passed_deals and not failed_deals and not pending_deals:
        print("0 deals after filters")
        print()
        # Show active filters
        filters = []
        if top > 0:
            filters.append(f"top={top}")
        if only_status:
            filters.append(f"only_status={only_status}")
        if not show_failed:
            filters.append("show_failed=False (default)")
        if not show_throttled:
            filters.append("show_throttled=False (default)")
        if filters:
            print(f"Active filters: {', '.join(filters)}")
            print()
        # Count all deals by status from original list
        total_passed = sum(1 for d in deals if d.get('status') == 'passed' or (d.get('status') is None and d.get('passed', False)))
        total_failed = sum(1 for d in deals if d.get('status') == 'failed')
        total_pending = sum(1 for d in deals if d.get('status') == 'pending')
        print(f"Total in file: PASSED={total_passed}, FAILED={total_failed}, PENDING={total_pending}")
        print()
        print("Tip: Use --show-failed or --show-throttled to see more, or --raw to bypass filters")
        print()

def main():
    """Main execution function."""
    parser = argparse.ArgumentParser(description='Woot → eBay Sold Arbitrage Checker')
    
    # Global arguments (work with any subcommand)
    parser.add_argument('--test-ebay-auth', action='store_true',
                       help='Test eBay OAuth authentication and exit')
    parser.add_argument('--one', type=str, metavar='QUERY',
                       help='Test single eBay query and exit')
    parser.add_argument('--no-retry', action='store_true',
                       help='In --one mode: do not retry on rate limit (safe test mode)')
    
    # Backward compatibility: support old "woot" and "watchlist" as positional args
    # Note: This must come before subparsers to avoid conflicts
    parser.add_argument('mode', nargs='?', default=None, choices=['woot', 'watchlist'],
                       help='[DEPRECATED] Use "scan" or "view" subcommands instead')
    parser.add_argument('--category', default='Tools',
                       help='Woot feed category (default: Tools) - for backward compatibility')
    parser.add_argument('--limit', type=int, default=10,
                       help='Maximum number of items to scan (default: 10) - for backward compatibility')
    
    # Create subparsers
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # scan subcommand
    scan_parser = subparsers.add_parser('scan', help='Scan Woot deals and analyze eBay sold listings')
    scan_parser.add_argument('--category', default='Tools',
                            help='Woot feed category (default: Tools)')
    scan_parser.add_argument('--limit', type=int, default=10,
                            help='Maximum number of items to scan (default: 10)')
    scan_parser.add_argument('--resume', action='store_true',
                            help='Resume scan: only process pending items from deals.json')
    scan_parser.add_argument('--stream', action='store_true',
                            help='Stream results live as they are evaluated (prints each deal immediately)')
    
    # view subcommand
    view_parser = subparsers.add_parser('view', help='View saved scan results')
    view_parser.add_argument('--top', type=int, default=20,
                            help='Number of top results to show (default: 20)')
    view_parser.add_argument('--only-status', type=str, choices=['passed', 'failed', 'pending'],
                            help='Filter by status: passed, failed, or pending')
    view_parser.add_argument('--show-failed', action='store_true',
                            help='Include failed items (default: show only opportunities)')
    view_parser.add_argument('--show-throttled', action='store_true',
                            help='Include throttled items')
    view_parser.add_argument('--raw', action='store_true',
                            help='Bypass filters and show first 10 deals (title, profit, ROI)')
    
    args = parser.parse_args()
    
    # Handle --one single query test (global flag)
    if args.one:
        print("=" * 80)
        print("One-item eBay test")
        print("=" * 80)
        print()
        
        # Print diagnostics
        print_ebay_diagnostics()
        
        # Call with no_retry flag if specified
        no_retry = args.no_retry
        if no_retry:
            print("[TEST] No-retry mode enabled (will exit immediately on throttle)")
            print()
        
        result = search_ebay_sold(args.one, no_retry=no_retry)
        
        # Check if throttled and exit with non-zero code if in no_retry mode
        if no_retry and result['status'] == 'EBAY_THROTTLED':
            print(f"sold_count: {result['sold_count']}")
            print(f"avg_sold_price: {result['avg_price']:.2f}")
            print(f"median_sold_price: {result['median_price']:.2f}")
            sys.exit(1)
        
        # Print compact block with all stats
        print(f"sold_count: {result['sold_count']}")
        print(f"avg: ${result['avg_price']:.2f}")
        print(f"median: ${result['median_price']:.2f}")
        
        # Print additional stats if available
        if result.get('p25_price') is not None and result.get('p75_price') is not None:
            print(f"p25/p75: ${result['p25_price']:.2f} / ${result['p75_price']:.2f}")
        if result.get('min_price') is not None and result.get('max_price') is not None:
            print(f"min/max: ${result['min_price']:.2f} / ${result['max_price']:.2f}")
        if result.get('last_sold_date'):
            print(f"last_sold_date: {result['last_sold_date']}")
        
        # Print sample comps
        sample_items = result.get('sample_items', [])
        if sample_items:
            print(f"sample_comps: {len(sample_items)} items")
            for idx, item in enumerate(sample_items, 1):
                title = item.get('title', '')[:60]  # Truncate long titles
                price = item.get('price', 0.0)
                print(f"  {idx}. ${price:.2f} - {title}")
        
        sys.exit(0)
    
    # Handle eBay auth test (global flag)
    if args.test_ebay_auth:
        client_id = os.environ.get('EBAY_CLIENT_ID', '').strip()
        client_secret = os.environ.get('EBAY_CLIENT_SECRET', '').strip()
        env = ebay_env()
        
        # Determine token URL
        if env == "SBX":
            token_url = "https://api.sandbox.ebay.com/identity/v1/oauth2/token"
        else:
            token_url = "https://api.ebay.com/identity/v1/oauth2/token"
        
        # Print redacted credentials
        def redact_value(value: str) -> str:
            """Redact a value: first 6 chars + '...' + last 4 chars."""
            if not value or len(value) <= 10:
                return "***" if value else "(empty)"
            return f"{value[:6]}...{value[-4:]}"
        
        print(f"EBAY_CLIENT_ID: {redact_value(client_id)}")
        print(f"EBAY_CLIENT_SECRET: {redact_value(client_secret)}")
        print(f"EBAY_ENV: {env}")
        print(f"Token URL: {token_url}")
        print()
        
        access_token = get_ebay_app_token()
        if access_token:
            print("EBAY AUTH OK")
            sys.exit(0)
        else:
            print("EBAY AUTH FAILED")
            sys.exit(1)
    
    # Handle subcommands or backward compatibility
    if args.command == 'scan':
        # New scan command
        resume_flag = getattr(args, 'resume', False)
        stream_flag = getattr(args, 'stream', False)
        process_woot_mode_with_save(category=args.category, limit=args.limit, resume=resume_flag, stream=stream_flag)
    elif args.command == 'view':
        # New view command
        view_deals(top=args.top, only_status=args.only_status, 
                   show_failed=args.show_failed, show_throttled=args.show_throttled,
                   raw=getattr(args, 'raw', False))
    elif args.mode == 'woot':
        # Backward compatibility: map "woot" to scan
        resume_flag = getattr(args, 'resume', False)
        stream_flag = getattr(args, 'stream', False)  # Backward compat doesn't have --stream, defaults to False
        process_woot_mode_with_save(category=args.category, limit=args.limit, resume=resume_flag, stream=stream_flag)
    elif args.mode == 'watchlist':
        # Backward compatibility: watchlist mode
        process_watchlist_mode()
    elif args.command is None and args.mode is None:
        # No command specified - default to scan (backward compatibility)
        resume_flag = getattr(args, 'resume', False)
        process_woot_mode_with_save(category=args.category, limit=args.limit, resume=resume_flag)
    else:
        # Default: show help
        parser.print_help()
        sys.exit(1)

if __name__ == '__main__':
    main()

