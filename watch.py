#!/usr/bin/env python3
"""Property watch - monitors Heckmondwike area property listings.

Checks OnTheMarket for 3-bed terraced/semi-detached houses in Heckmondwike
and 2 miles radius, £120k-£220k. Emails alerts for new listings and price drops.
Generates an HTML dashboard sorted by house size (sq ft).
"""

import json
import re
import shutil
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path

from curl_cffi import requests as cffi_requests

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / "state.json"
STATE_BAK = SCRIPT_DIR / "state.json.bak"
LOG_FILE = SCRIPT_DIR / "alerts.log"
HTML_FILE = SCRIPT_DIR / "index.html"

SESSION = cffi_requests.Session(impersonate="chrome")


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen": {}, "last_run": None}


def save_state(state):
    if STATE_FILE.exists():
        shutil.copy2(STATE_FILE, STATE_BAK)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def fetch_ontemarket(config):
    """Fetch listings from OnTheMarket search results."""
    filters = config["filters"]
    search = config["search"]

    centre = search["centre"].split(",")[0].strip().lower().replace(" ", "-")
    url = f"https://www.onthemarket.com/for-sale/property/{centre}/"

    params = {
        "max-price": str(filters["max_price"]),
        "min-price": str(filters["min_price"]),
        "bedrooms-min": str(filters["bedrooms"]),
    }

    log(f"Fetching OTM: {url} {params}")
    r = SESSION.get(url, params=params, timeout=20)
    r.raise_for_status()

    listings = []
    for pid, html in re.findall(
        r'<li id="result-(\d+)"[^>]*>(.*?)</li>', r.text, re.DOTALL
    ):
        listing = _parse_otm_listing(pid, html)
        if listing:
            listings.append(listing)

    log(f"OTM: {len(listings)} raw listings parsed")
    return listings


def _parse_otm_listing(pid, html):
    """Parse a single OTM listing from its HTML snippet."""
    # Price
    price_match = re.search(r'>([\u00a3\xa3][\d,]+)</a>', html)
    if not price_match:
        return None
    price_str = price_match.group(1).replace("\xa3", "").replace("\u00a3", "").replace(",", "")
    try:
        price = int(price_str)
    except ValueError:
        return None

    # Address
    addr_match = re.search(r'<address[^>]*><span>([^<]+)</span>', html)
    address = addr_match.group(1).strip() if addr_match else ""

    # Bedrooms and property type from alt text on image
    alt_match = re.search(r'alt="(\d+)\s+bedroom\s+([^"]+?)\s+for sale', html, re.IGNORECASE)
    if alt_match:
        beds = int(alt_match.group(1))
        prop_type = alt_match.group(2).strip()
    else:
        beds_match = re.search(r'numberOfBedrooms[^>]*>.*?</svg>\s*(\d+)', html, re.DOTALL)
        beds = int(beds_match.group(1)) if beds_match else 0
        prop_type = "unknown"

    # Agent name
    agent_match = re.search(r'font-bold text-white font-normal">\s*([^<]+)', html)
    agent = agent_match.group(1).strip() if agent_match else "Unknown"

    # First image URL from srcSet or src
    img_match = re.search(
        r'srcSet="(https://media\.onthemarket\.com/properties/[^"]+\.webp)', html
    )
    if not img_match:
        img_match = re.search(
            r'src="(https://media\.onthemarket\.com/properties/[^"]+\.jpg)', html
        )
    image = img_match.group(1) if img_match else ""

    return {
        "id": f"otm-{pid}",
        "source": "OnTheMarket",
        "address": address,
        "price": price,
        "bedrooms": beds,
        "type": prop_type,
        "url": f"https://www.onthemarket.com/details/{pid}/",
        "agent": agent,
        "image": image,
        "sqft": None,
    }


def fetch_barkers(config):
    """Fetch listings from Barkers Estate Agents.

    Barkers' search doesn't filter reliably via URL params, so we scrape
    all listings and filter client-side for our target area.
    """
    listings = []
    start = 0
    max_pages = 10

    while start < max_pages * 12:
        url = "https://www.barkersestateagents.co.uk/properties-for-sale"
        params = {"filter_cat": "1"}
        if start > 0:
            params["start"] = str(start)

        log(f"Fetching Barkers page (start={start})")
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log(f"Barkers fetch error: {e}")
            break

        page_listings = _parse_barkers_page(r.text)
        listings.extend(page_listings)

        if f"start={start + 12}" not in r.text:
            break
        start += 12

    log(f"Barkers: {len(listings)} raw listings parsed")
    return listings


def _parse_barkers_page(html):
    """Parse property listings from a Barkers search results page."""
    listings = []

    listing_blocks = re.split(r'<div[^>]*id="eapow-listing-(\d+)"', html)

    for i in range(1, len(listing_blocks), 2):
        lid = listing_blocks[i]
        block = listing_blocks[i + 1] if i + 1 < len(listing_blocks) else ""

        next_listing = block.find('id="eapow-listing-')
        if next_listing > 0:
            block = block[:next_listing]

        link_match = re.search(
            r'href="(/properties-for-sale/property/[^"]+)"', block
        )
        if not link_match:
            continue
        link = link_match.group(1)

        addr_match = re.search(
            r'<h3>\s*<a[^>]*>\s*(.*?)\s*</a>', block, re.DOTALL
        )
        if addr_match:
            address = re.sub(r'<[^>]+>', ' ', addr_match.group(1)).strip()
            address = re.sub(r'\s+', ' ', address)
        else:
            address = "Unknown"

        price_match = re.search(
            r'eapow-overview-price[^>]*>[^£]*£([\d,]+)', block
        )
        if not price_match:
            continue
        try:
            price = int(price_match.group(1).replace(",", ""))
        except ValueError:
            continue

        beds_match = re.search(
            r'flaticon-bed[^>]*>.*?<span class="IconNum">\s*(\d+)', block, re.DOTALL
        )
        beds = int(beds_match.group(1)) if beds_match else 0

        type_match = re.search(
            r'alt="(\d+)\s+bed\s+(\w[^"]*?)\s+in\s+', block, re.IGNORECASE
        )
        prop_type = type_match.group(2).strip() if type_match else "unknown"

        listings.append({
            "id": f"barkers-{lid}",
            "source": "Barkers",
            "address": address,
            "price": price,
            "bedrooms": beds,
            "type": prop_type,
            "url": f"https://www.barkersestateagents.co.uk{link}",
            "agent": "Barkers Estate Agents",
            "image": "",
            "sqft": None,
        })

    return listings


def filter_listings(listings, config):
    """Apply client-side filters for property type, beds, price."""
    filters = config["filters"]
    target_types = [t.lower() for t in filters["property_types"]]
    allowed_beds = filters["bedrooms"]
    min_price = filters["min_price"]
    max_price = filters["max_price"]

    filtered = []
    for listing in listings:
        if listing["bedrooms"] != allowed_beds:
            continue
        if listing["price"] < min_price or listing["price"] > max_price:
            continue
        ptype = listing["type"].lower()
        if not any(t in ptype for t in target_types):
            continue
        filtered.append(listing)

    return filtered


def enrich_with_sqft(listings):
    """Fetch sq ft from OTM detail pages for each listing."""
    for listing in listings:
        if listing["source"] != "OnTheMarket":
            continue
        # Extract OTM property ID from listing ID (format: "otm-12345678")
        pid = listing["id"].replace("otm-", "")
        try:
            r = SESSION.get(
                f"https://www.onthemarket.com/details/{pid}/", timeout=15
            )
            if r.status_code == 200:
                sqft_match = re.search(r'"minimumAreaSqFt":(\d+)', r.text)
                if sqft_match:
                    listing["sqft"] = int(sqft_match.group(1))
                # Also try to grab sqm
                sqm_match = re.search(r'"minimumAreaSqM":(\d+)', r.text)
                if sqm_match:
                    listing["sqm"] = int(sqm_match.group(1))
        except Exception as e:
            log(f"  sqft fetch failed for {pid}: {e}")

    log(f"Enriched {sum(1 for l in listings if l.get('sqft'))} listings with sq ft")
    return listings


def find_alerts(current_listings, state):
    """Compare current listings against state to find new and price-dropped."""
    new_listings = []
    price_drops = []

    for listing in current_listings:
        lid = listing["id"]
        if lid not in state["seen"]:
            new_listings.append(listing)
        elif listing["price"] < state["seen"][lid]["price"]:
            price_drops.append({**listing, "old_price": state["seen"][lid]["price"]})

    return new_listings, price_drops


def send_email(config, new_listings, price_drops):
    """Send a single email containing all alerts."""
    email_cfg = config["email"]

    if not email_cfg.get("sender") or email_cfg["sender"] == "YOUR_GMAIL@gmail.com":
        log("Email not configured - skipping send (update config.json)")
        return None

    lines = []
    total = len(new_listings) + len(price_drops)
    lines.append(f"PROPERTY ALERT - {total} item(s)")
    lines.append(f"Heckmondwike + 2mi | 3-bed terraced/semi-detached")
    lines.append("")

    if new_listings:
        lines.append(f"--- {len(new_listings)} NEW LISTING(S) ---")
        for i, l in enumerate(new_listings, 1):
            lines.append("")
            size = f" | {l['sqft']} sq ft" if l.get("sqft") else ""
            lines.append(f"{i}. \xa3{l['price']:,} | {l['bedrooms']}-bed {l['type'].title()}{size}")
            lines.append(f"   {l['address']}")
            lines.append(f"   Agent: {l['agent']}")
            lines.append(f"   {l['url']}")

    if price_drops:
        if new_listings:
            lines.append("")
        lines.append(f"--- {len(price_drops)} PRICE DROP(S) ---")
        for i, l in enumerate(price_drops, 1):
            lines.append("")
            size = f" | {l['sqft']} sq ft" if l.get("sqft") else ""
            lines.append(
                f"{i}. \xa3{l['old_price']:,} -> \xa3{l['price']:,} "
                f"| {l['bedrooms']}-bed {l['type'].title()}{size}"
            )
            lines.append(f"   {l['address']}")
            lines.append(f"   Agent: {l['agent']}")
            lines.append(f"   {l['url']}")

    body = "\n".join(lines)
    subject = f"Property Alert: {len(new_listings)} new, {len(price_drops)} price drops"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = email_cfg["recipient"]

    with smtplib.SMTP(email_cfg["smtp_server"], email_cfg["smtp_port"]) as server:
        server.starttls()
        server.login(email_cfg["sender"], email_cfg["password_app"])
        server.send_message(msg)

    return subject


def generate_html(listings):
    """Generate a self-contained HTML dashboard sorted by sq ft."""
    # Sort: sqft first (biggest first), then no-sqft at bottom
    sorted_listings = sorted(
        listings,
        key=lambda l: (l.get("sqft") is not None, l.get("sqft") or 0),
        reverse=True,
    )

    now = datetime.now().strftime("%d %b %Y, %H:%M")
    count = len(sorted_listings)

    cards_html = ""
    for i, l in enumerate(sorted_listings):
        sqft = l.get("sqft")
        sqm = l.get("sqm")
        if sqft:
            size_badge = f'<span class="size">{sqft} sq ft ({sqm} m&sup2;)</span>'
        else:
            size_badge = '<span class="size unknown">Size unknown</span>'

        img_html = ""
        if l.get("image"):
            img_html = f'<img src="{l["image"]}" alt="{l["address"]}" loading="lazy" />'

        price_drop_html = ""
        if "old_price" in l:
            price_drop_html = (
                f'<span class="old-price">&pound;{l["old_price"]:,}</span> '
                f'<span class="drop-arrow">&darr;</span> '
            )

        tag = ""
        if "old_price" in l:
            tag = '<span class="tag drop">PRICE DROP</span>'
        elif l["id"] not in _seen_before:
            tag = '<span class="tag new">NEW</span>'

        cards_html += f"""
        <a href="{l['url']}" target="_blank" rel="noopener" class="card">
            {tag}
            <div class="img-wrap">
                {img_html}
            </div>
            <div class="info">
                <div class="price-row">
                    {price_drop_html}<span class="price">&pound;{l['price']:,}</span>
                </div>
                <div class="meta">
                    {l['bedrooms']} bed &middot; {l['type'].title()} &middot; {l['agent']}
                </div>
                <div class="address">{l['address']}</div>
                <div class="details">
                    {size_badge}
                </div>
            </div>
        </a>
"""

    if not cards_html:
        cards_html = '<div class="empty">No matching properties found right now. Next check in 15 minutes.</div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="refresh" content="900" />
<title>Property Watch - Heckmondwike</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }}
.header {{ padding: 24px 20px 16px; text-align: center; border-bottom: 1px solid #1e293b; }}
.header h1 {{ font-size: 20px; font-weight: 600; color: #f8fafc; }}
.header .sub {{ font-size: 13px; color: #64748b; margin-top: 4px; }}
.header .count {{ font-size: 14px; color: #38bdf8; margin-top: 6px; font-weight: 500; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; padding: 20px; max-width: 1200px; margin: 0 auto; }}
.card {{ display: block; background: #1e293b; border-radius: 12px; overflow: hidden; text-decoration: none; color: inherit; transition: transform 0.15s, box-shadow 0.15s; position: relative; }}
.card:hover {{ transform: translateY(-3px); box-shadow: 0 8px 25px rgba(0,0,0,0.4); }}
.img-wrap {{ width: 100%; height: 200px; overflow: hidden; background: #334155; }}
.img-wrap img {{ width: 100%; height: 100%; object-fit: cover; }}
.info {{ padding: 16px; }}
.price-row {{ display: flex; align-items: center; gap: 8px; }}
.price {{ font-size: 22px; font-weight: 700; color: #f8fafc; }}
.old-price {{ font-size: 14px; color: #94a3b8; text-decoration: line-through; }}
.drop-arrow {{ color: #22c55e; font-size: 14px; }}
.meta {{ font-size: 13px; color: #94a3b8; margin-top: 6px; }}
.address {{ font-size: 14px; color: #cbd5e1; margin-top: 6px; line-height: 1.4; }}
.details {{ margin-top: 10px; }}
.size {{ display: inline-block; background: #334155; color: #38bdf8; padding: 4px 10px; border-radius: 6px; font-size: 13px; font-weight: 500; }}
.size.unknown {{ color: #64748b; }}
.tag {{ position: absolute; top: 12px; right: 12px; padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; z-index: 2; }}
.tag.new {{ background: #22c55e; color: #fff; }}
.tag.drop {{ background: #f59e0b; color: #fff; }}
.empty {{ text-align: center; padding: 60px 20px; color: #64748b; font-size: 15px; }}
.footer {{ text-align: center; padding: 20px; color: #475569; font-size: 12px; border-top: 1px solid #1e293b; }}
@media (max-width: 480px) {{
    .grid {{ grid-template-columns: 1fr; padding: 12px; gap: 12px; }}
    .img-wrap {{ height: 180px; }}
}}
</style>
</head>
<body>
<div class="header">
    <h1>Property Watch &mdash; Heckmondwike</h1>
    <div class="sub">3-bed terraced/semi-detached &middot; &pound;120k&ndash;&pound;220k &middot; 2mi radius</div>
    <div class="count">{count} matching {properties(count)}</div>
</div>
<div class="grid">
{cards_html}
</div>
<div class="footer">
    Updated {now} &middot; Auto-refreshes every 15 minutes &middot; Sorted by size (largest first)
</div>
</body>
</html>"""

    HTML_FILE.write_text(html)
    log(f"HTML dashboard written to {HTML_FILE}")


def properties(n):
    return "property" if n == 1 else "properties"


_seen_before = set()


def main():
    global _seen_before
    log("=== Run started ===")
    config = load_config()
    state = load_state()

    # Track what was seen before this run
    _seen_before = set(state["seen"].keys())

    # Fetch from all configured sources
    all_listings = []
    for source in config.get("sources", []):
        try:
            if source == "ontemarket":
                all_listings.extend(fetch_ontemarket(config))
            elif source == "barkers":
                all_listings.extend(fetch_barkers(config))
        except Exception as e:
            log(f"ERROR fetching {source}: {e}")

    log(f"Total raw listings: {len(all_listings)}")

    # Apply filters
    filtered = filter_listings(all_listings, config)
    log(f"After filtering: {len(filtered)} listings")

    # Enrich with sq ft from detail pages
    filtered = enrich_with_sqft(filtered)

    # Find new and price-dropped
    new_listings, price_drops = find_alerts(filtered, state)

    if new_listings or price_drops:
        log(f"ALERTS: {len(new_listings)} new, {len(price_drops)} price drops")
        for l in new_listings:
            log(f"  NEW: \xa3{l['price']:,} {l['bedrooms']}-bed {l['type']} - {l['address']} [{l['source']}]")
        for l in price_drops:
            log(
                f"  DROP: \xa3{l['old_price']:,}->\xa3{l['price']:,} "
                f"{l['bedrooms']}-bed {l['type']} - {l['address']} [{l['source']}]"
            )

        try:
            subject = send_email(config, new_listings, price_drops)
            if subject:
                log(f"Email sent: {subject}")
        except Exception as e:
            log(f"EMAIL ERROR: {e}")
    else:
        log(f"No alerts ({len(filtered)} listings in range)")

    # Update state
    for listing in filtered:
        state["seen"][listing["id"]] = {
            "price": listing["price"],
            "address": listing["address"],
            "sqft": listing.get("sqft"),
            "last_seen": datetime.now().isoformat(),
        }
    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    # Generate HTML dashboard (pass all filtered listings + price drops for display)
    display_listings = filtered + [
        {**l, "price": l["old_price"], "_is_drop": True}
        for l in price_drops
    ]
    # For display, use the current price version of drops
    display_listings = filtered[:]
    for l in price_drops:
        display_listings.append(l)
    generate_html(display_listings)

    log("=== Run complete ===\n")


if __name__ == "__main__":
    main()
