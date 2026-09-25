import re
import os
import json
import asyncio
import base64
import requests
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram import Bot

# ─── CONFIG ───────────────────────────────────────────────
# Nothing personal is stored in this file. Every value comes from
# GitHub Secrets / Variables (see the workflow file).

def require_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Missing required setting: {name}. "
            "Add it in the repo under Settings > Secrets and variables > Actions."
        )
    return value

def parse_list(value):
    return [item.strip() for item in (value or "").split(",") if item.strip()]

def normalize_channel(name):
    if name.startswith("@") or name.lstrip("-").isdigit():
        return name
    return "@" + name

try:
    API_ID = int(require_env("API_ID"))
except ValueError:
    raise SystemExit("API_ID must be a number.")
API_HASH             = require_env("API_HASH")
BOT_TOKEN            = require_env("BOT_TOKEN")
SESSION_STRING       = require_env("SESSION_STRING")
SOURCE_CHANNEL       = normalize_channel(require_env("SOURCE_CHANNEL"))
TARGET_CHANNELS      = [normalize_channel(c) for c in parse_list(require_env("TARGET_CHANNELS"))]
AFFILIATE_TAG        = require_env("AFFILIATE_TAG")
BLOCKED_MENTIONS     = parse_list(os.environ.get("BLOCKED_MENTIONS", ""))
GITHUB_TOKEN         = os.environ.get("GH_TOKEN", "")
GITHUB_REPO          = os.environ.get("GH_REPO", "")
GEMINI_API_KEY       = os.environ.get("GEMINI_API_KEY", "")
AMAZON_CLIENT_ID     = os.environ.get("AMAZON_CLIENT_ID", "")
AMAZON_CLIENT_SECRET = os.environ.get("AMAZON_CLIENT_SECRET", "")
USE_SHORT_LINKS      = os.environ.get("USE_SHORT_LINKS", "false").lower() == "true"  # "true" for amzn.to links
DEALS_FILE           = "deals.json"
MAX_DEALS            = 100
IST                  = timezone(timedelta(hours=5, minutes=30))
if not TARGET_CHANNELS:
    raise SystemExit("TARGET_CHANNELS must list at least one channel, separated by commas.")
# ──────────────────────────────────────────────────────────

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
bot = Bot(token=BOT_TOKEN)

AMAZON_PATTERN = re.compile(
    r'https?://(?:www\.)?(?:amazon\.in|amzn\.to|a\.co)/[\w/?=&%#.+\-@]*'
)
ALL_URL_PATTERN = re.compile(r'https?://[^\s]+')

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}

# Login-with-Amazon token endpoints. Amazon issues credentials per region group;
# we try the North America one first, then Europe (which includes India).
TOKEN_URLS = [
    "https://api.amazon.com/auth/o2/token",
    "https://api.amazon.co.uk/auth/o2/token",
]
_amazon_tokens = {}  # token URL -> (token, expiry timestamp)


# ── AMAZON CREATORS API ──────────────────────────────────

def get_amazon_token(url_index=0):
    """Get an OAuth token for the Amazon Creators API (cached until near expiry)."""
    url = TOKEN_URLS[url_index]
    now = datetime.now().timestamp()
    cached = _amazon_tokens.get(url)
    if cached and now < cached[1]:
        return cached[0]
    try:
        r = requests.post(
            url,
            json={
                "grant_type": "client_credentials",
                "client_id": AMAZON_CLIENT_ID,
                "client_secret": AMAZON_CLIENT_SECRET,
                "scope": "creatorsapi::default"
            },
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            token = data["access_token"]
            _amazon_tokens[url] = (token, now + data.get("expires_in", 3600) - 60)
            print("Amazon token obtained!")
            return token
        print(f"Amazon token error: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"Amazon token exception: {type(e).__name__}")
    return None


def get_short_link(asin):
    """Generate official amzn.to short link via Amazon Creators API."""
    if not USE_SHORT_LINKS:
        return None
    token = get_amazon_token()
    if not token:
        return None
    try:
        r = requests.post(
            "https://creatorsapi.amazon/links/v1/createShortLink",
            json={
                "asin": asin,
                "marketplace": "www.amazon.in",
                "partnerTag": AFFILIATE_TAG,
                "partnerType": "Associates"
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            timeout=10
        )
        print(f"Short link API status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            short_url = data.get("shortLink") or data.get("shortUrl") or data.get("url")
            if short_url:
                print("Short link generated.")
                return short_url
        else:
            print(f"Short link error: {r.text[:200]}")
    except Exception as e:
        print(f"Short link exception: {e}")
    return None


def get_product_from_creators_api(asin):
    """Fetch product title + image from the Amazon Creators API (GetItems).

    Request format follows Amazon's official docs: marketplace is sent as an
    x-marketplace header, and resource names are lowerCamelCase.
    """
    body = {
        "itemIds": [asin],
        "itemIdType": "ASIN",
        "marketplace": "www.amazon.in",
        "partnerTag": AFFILIATE_TAG,
        "resources": ["itemInfo.title", "images.primary.large"],
    }
    for idx in range(len(TOKEN_URLS)):
        token = get_amazon_token(idx)
        if not token:
            continue
        try:
            r = requests.post(
                "https://creatorsapi.amazon/catalog/v1/getItems",
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "x-marketplace": "www.amazon.in",
                },
                timeout=10
            )
        except Exception as e:
            print(f"Creators API exception: {type(e).__name__}")
            return None
        print(f"Creators API status: {r.status_code}")
        if r.status_code in (401, 403):
            # Token may belong to the wrong region group; try the next endpoint.
            print(f"Creators API auth error: {r.text[:200]}")
            _amazon_tokens.pop(TOKEN_URLS[idx], None)
            continue
        if r.status_code != 200:
            print(f"Creators API error: {r.text[:200]}")
            return None
        data = r.json()
        # Amazon's docs use both spellings for the result container.
        result = data.get("itemsResult") or data.get("itemResults") or {}
        items = result.get("items") or []
        if not items:
            print(f"Creators API returned no item. Errors: {str(data.get('errors'))[:200]}")
            return None
        item = items[0]
        title = ((item.get("itemInfo") or {}).get("title") or {}).get("displayValue")
        image = (((item.get("images") or {}).get("primary") or {}).get("large") or {}).get("url")
        print(f"Creators API: title={'yes' if title else 'no'}, image={'yes' if image else 'no'}")
        return {
            "title": title,
            "image": image,
            "rating": None,  # star ratings are not offered by GetItems
            "affiliate_url": f"https://www.amazon.in/dp/{asin}?tag={AFFILIATE_TAG}",
        }
    return None


# ── FILTERS ──────────────────────────────────────────────

def has_amazon_link(text):
    return bool(AMAZON_PATTERN.search(text or ""))

def has_blocked_mention(text):
    if not text:
        return False
    return any(m.lower() in text.lower() for m in BLOCKED_MENTIONS)


# ── TELEGRAM PREVIEW EXPANDER ────────────────────────────

def get_expanded_urls_from_preview(message):
    expanded = []
    try:
        media = message.media
        if media and hasattr(media, 'webpage'):
            wp = media.webpage
            if hasattr(wp, 'url') and wp.url and 'amazon' in wp.url:
                print("Found link preview data.")
                expanded.append(wp.url)
    except Exception as e:
        print(f"Preview extract error: {e}")
    return expanded


# ── URL PROCESSING ───────────────────────────────────────

def add_affiliate_tag(url):
    url = re.sub(r'[?&]tag=[^&\s]+', '', url)
    url = url.rstrip('?&')
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={AFFILIATE_TAG}"


def build_clean_url(url):
    asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    if asin_match and 'amazon.in' in url:
        asin = asin_match.group(1)
        return f"https://www.amazon.in/dp/{asin}?tag={AFFILIATE_TAG}"
    return add_affiliate_tag(url)


def process_url(url, preview_urls):
    original = url
    # Try expanding short links
    if 'amzn.to' in url or 'a.co' in url:
        for pu in preview_urls:
            if 'amazon.in' in pu:
                print("Expanded short link via preview.")
                url = pu
                break
        if 'amzn.to' in url or 'a.co' in url:
            try:
                r = requests.get(url, allow_redirects=True, timeout=8, headers=HEADERS)
                if 'amazon.in' in r.url:
                    print("Expanded short link via HTTP.")
                    url = r.url
            except Exception as e:
                print(f"HTTP expand failed: {type(e).__name__}")

    # If short links enabled, generate official amzn.to link
    if USE_SHORT_LINKS:
        asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
        if asin_match:
            short = get_short_link(asin_match.group(1))
            if short:
                print("Using generated short link.")
                return short

    final = build_clean_url(url)
    return final


def replace_all_amazon_links(text, preview_urls):
    if not text:
        return text
    def handle_match(match):
        return process_url(match.group(0), preview_urls)
    return AMAZON_PATTERN.sub(handle_match, text)


def remove_non_amazon_links(text):
    if not text:
        return text
    def handle_url(match):
        url = match.group(0)
        if AMAZON_PATTERN.match(url):
            return url
        print("Removed a non-Amazon link.")
        return ''
    cleaned = ALL_URL_PATTERN.sub(handle_url, text)
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


# ── TITLE EXTRACTOR ──────────────────────────────────────

def extract_title_from_text(text):
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    for line in lines:
        if line.startswith('http') or 'amzn' in line or 'amazon' in line.lower():
            continue
        if re.match(r'^[@₹\d\s%offOFFCashbackcashback:]+$', line):
            continue
        if len(line) < 8:
            continue
        cleaned = re.sub(r'[@₹]\s*\d+[\d,]*', '', line).strip()
        cleaned = re.sub(r'\b\d+%\s*[Oo]ff\b', '', cleaned).strip()
        if len(cleaned) > 8:
            return cleaned[:120]
    return None


# ── AMAZON SCRAPER (fallback) ─────────────────────────────

def scrape_amazon_product(url):
    try:
        if not re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url):
            return {}
        if 'amzn.to' in url or 'a.co' in url:
            return {}
        html = None
        for ua in [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
        ]:
            try:
                r = requests.get(url, headers={**HEADERS, "User-Agent": ua}, timeout=12)
                if r.status_code == 200:
                    html = r.text
                    break
            except Exception:
                continue
        if not html:
            return {}
        if any(x in html[:2000] for x in ["503", "Service Unavailable", "Robot Check"]):
            return {}
        title = None
        for pattern in [r'id="productTitle"[^>]*>\s*([^<]+?)\s*<']:
            m = re.search(pattern, html)
            if m:
                candidate = m.group(1).strip()
                if len(candidate) > 10 and 'amazon' not in candidate.lower():
                    title = candidate
                    break
        rating = None
        for pattern in [r'"ratingScore"\s*:\s*"([\d.]+)"', r'([\d.]+) out of 5 stars']:
            m = re.search(pattern, html)
            if m:
                try:
                    v = float(m.group(1))
                    if 0 < v <= 5:
                        rating = v
                        break
                except Exception:
                    pass
        image = None
        for pattern in [
            r'"large"\s*:\s*"(https://m\.media-amazon\.com/images/[^"]+)"',
            r'id="landingImage"[^>]*src="([^"]+)"',
        ]:
            m = re.search(pattern, html)
            if m:
                image = m.group(1)
                break
        raw_category = "General"
        m = re.search(r'wayfinding-breadcrumbs_feature_div.*?<li[^>]*>\s*<a[^>]*>([^<]{3,40})<', html, re.DOTALL)
        if m:
            raw_category = m.group(1).strip()
        return {"title": title, "rating": rating, "image": image, "raw_category": raw_category}
    except Exception as e:
        print(f"Scrape error: {e}")
        return {}


# ── AI CATEGORISER ───────────────────────────────────────

def categorise_product(title, raw_category):
    categories = ["Electronics", "Fashion", "Home", "Beauty", "Sports", "General"]
    try:
        prompt = (
            f"Categorise this Amazon product into exactly one of: Electronics, Fashion, Home, Beauty, Sports, General.\n"
            f"Product: {title}\nBreadcrumb: {raw_category}\nReply with ONLY the category name."
        )
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 10, "temperature": 0}},
            timeout=10
        )
        data = r.json()
        if "candidates" in data:
            cat = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if cat in categories:
                return cat
    except Exception as e:
        print(f"Category error: {e}")
    combined = f"{title or ''} {raw_category or ''}".lower()
    if any(k in combined for k in ["phone", "laptop", "camera", "tv", "speaker", "headphone", "tablet", "computer", "monitor", "bulb", "fan", "refrigerator"]):
        return "Electronics"
    if any(k in combined for k in ["shirt", "dress", "saree", "kurta", "jeans", "shoes", "sandal", "watch", "jewel", "bag", "wallet"]):
        return "Fashion"
    if any(k in combined for k in ["kitchen", "cookware", "bedsheet", "pillow", "curtain", "furniture", "sofa", "cleaning"]):
        return "Home"
    if any(k in combined for k in ["cream", "serum", "shampoo", "lotion", "sunscreen", "face wash", "lipstick", "supplement"]):
        return "Beauty"
    if any(k in combined for k in ["dumbbell", "yoga", "treadmill", "cycle", "fitness", "protein", "sport", "gym"]):
        return "Sports"
    return "General"


# ── AI DESCRIPTION ───────────────────────────────────────

def generate_description(title, category, message_text):
    try:
        clean_msg = AMAZON_PATTERN.sub('', message_text or '')
        clean_msg = re.sub(r'[@₹]\s*\d+[\d,]*', '', clean_msg).strip()[:300]
        prompt = (
            f"Write a 2-3 sentence product description for an Amazon India deals website.\n"
            f"Product: {title or 'Amazon Product'}\nCategory: {category}\nContext: {clean_msg}\n"
            f"Rules: highlight features, helpful for Indian shoppers, NO price/discount/urgency, follow Amazon Associates guidelines, be specific.\n"
            f"Return only the description."
        )
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 200, "temperature": 0.7}},
            timeout=15
        )
        data = r.json()
        if "candidates" in data:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Description error: {e}")
    fallbacks = {
        "Electronics": f"The {title or 'device'} is a reliable choice packed with features for Indian consumers. Check full specs and reviews on Amazon India.",
        "Fashion": f"This {title or 'item'} combines style and comfort for everyday wear. See size options and customer photos on Amazon.",
        "Home": f"Upgrade your home with the {title or 'product'}, designed for functionality and modern aesthetics. View all features on Amazon India.",
        "Beauty": f"The {title or 'product'} delivers effective care suitable for Indian skin types. Check ingredients and reviews on Amazon India.",
        "Sports": f"Designed for performance, the {title or 'product'} is ideal for fitness enthusiasts. See full specs on Amazon India.",
    }
    return fallbacks.get(category, f"The {title or 'product'} is a top-rated pick on Amazon India. Check complete details and genuine reviews before purchasing.")


# ── GITHUB UPDATER ───────────────────────────────────────

def get_deals_from_github():
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DEALS_FILE}",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content), data["sha"]
    except Exception as e:
        print(f"GitHub fetch error: {e}")
    return [], None


def push_deals_to_github(deals, sha, title="update"):
    try:
        content = base64.b64encode(json.dumps(deals, ensure_ascii=False, indent=2).encode()).decode()
        payload = {"message": f"Add: {title[:60]}", "content": content}
        if sha:
            payload["sha"] = sha
        r = requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DEALS_FILE}",
            headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"},
            json=payload, timeout=15
        )
        if r.status_code in (200, 201):
            print("GitHub updated!")
            return True
        print(f"GitHub error: {r.status_code}")
    except Exception as e:
        print(f"GitHub push error: {e}")
    return False


def add_deal_to_website(title, description, image, rating, category, link):
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    deals, sha = get_deals_from_github()
    now = datetime.now(IST)
    deals.insert(0, {
        "id": int(now.timestamp()),
        "title": title or "Amazon Deal",
        "description": description,
        "image": image,
        "rating": rating,
        "category": category,
        "link": link,
        "date": now.isoformat(),
        "time": now.strftime("%I:%M %p IST"),
    })
    push_deals_to_github(deals[:MAX_DEALS], sha, title or "deal")


# ── MESSAGE SENDER ───────────────────────────────────────

async def send_to_all(new_text, message):
    sent = 0
    for i, channel in enumerate(TARGET_CHANNELS, start=1):
        try:
            if message.photo:
                await bot.send_photo(chat_id=channel, photo=await message.download_media(bytes), caption=new_text)
                sent += 1
            elif message.video:
                await bot.send_video(chat_id=channel, video=await message.download_media(bytes), caption=new_text)
                sent += 1
            elif message.document:
                await bot.send_document(chat_id=channel, document=await message.download_media(bytes), caption=new_text)
                sent += 1
            else:
                if new_text:
                    await bot.send_message(chat_id=channel, text=new_text, disable_web_page_preview=True)
                    sent += 1
        except Exception as e:
            print(f"Send error to target #{i}: {type(e).__name__}: {e}")
    print(f"Posted to {sent}/{len(TARGET_CHANNELS)} channels.")


# ── MAIN HANDLER ─────────────────────────────────────────

@client.on(events.NewMessage(chats=SOURCE_CHANNEL))
async def handler(event):
    message = event.message
    original_text = message.text or message.caption or ""

    if not has_amazon_link(original_text):
        return
    if has_blocked_mention(original_text):
        return

    preview_urls = get_expanded_urls_from_preview(message)
    cleaned_text = remove_non_amazon_links(original_text)
    new_text = replace_all_amazon_links(cleaned_text, preview_urls)

    print("New deal message received.")

    await send_to_all(new_text, message)

    # Update website
    try:
        match = AMAZON_PATTERN.search(original_text)
        if not match:
            return

        raw_url = match.group(0)

        # Expand short URLs
        if 'amzn.to' in raw_url or 'a.co' in raw_url:
            for pu in preview_urls:
                if 'amazon.in' in pu:
                    raw_url = pu
                    break
            if 'amzn.to' in raw_url or 'a.co' in raw_url:
                try:
                    r = requests.get(raw_url, allow_redirects=True, timeout=8, headers=HEADERS)
                    if 'amazon.in' in r.url:
                        raw_url = r.url
                except Exception:
                    pass

        asin_match = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', raw_url)
        if not asin_match:
            return

        asin = asin_match.group(1)

        # Try Creators API first for best data
        product_data = get_product_from_creators_api(asin)

        if product_data:
            title = product_data.get("title")
            image = product_data.get("image")
            rating = product_data.get("rating")
            affiliate_url = product_data.get("affiliate_url") or f"https://www.amazon.in/dp/{asin}?tag={AFFILIATE_TAG}"
            raw_category = ""
            if rating is None:
                # The API has no star rating; try the product page (best effort).
                rating = scrape_amazon_product(f"https://www.amazon.in/dp/{asin}").get("rating")
        else:
            # Fallback to scraping (clean link, so no one else's tag is involved)
            product = scrape_amazon_product(f"https://www.amazon.in/dp/{asin}")
            title = product.get("title")
            image = product.get("image")
            rating = product.get("rating")
            raw_category = product.get("raw_category", "")
            affiliate_url = f"https://www.amazon.in/dp/{asin}?tag={AFFILIATE_TAG}"

        title = title or extract_title_from_text(original_text) or "Amazon Deal"
        category = categorise_product(title, raw_category if not product_data else "")
        description = generate_description(title, category, original_text)

        # Try to get short link if enabled
        if USE_SHORT_LINKS:
            short = get_short_link(asin)
            if short:
                affiliate_url = short

        add_deal_to_website(title, description, image, rating, category, affiliate_url)
        print(f"Website updated: {title[:60]}")

    except Exception as e:
        print(f"Website update error: {e}")


async def main():
    print("Bot started!")
    print(f"Watching 1 source, posting to {len(TARGET_CHANNELS)} channel(s).")
    await client.start()
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
