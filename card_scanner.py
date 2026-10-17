from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().with_name(".env"))

import asyncio
import time
import re
import requests
import json
import os
import base64
import sys
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from playwright.async_api import async_playwright

# ==========================================
# 🔑 AUTONOMOUS CREDENTIALS
# ==========================================
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")

# YOUR BURNER ACCOUNT
CL_EMAIL = os.getenv("CL_EMAIL", "")
CL_PASSWORD = os.getenv("CL_PASSWORD", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ------
MIN_PROFIT_DOLLARS = 15.00
MIN_PROFIT_MARGIN = 0.20  

MANUAL_OFFSET_JUMP = 54600

LIVE_EBAY_TOKEN = None
LIVE_CL_TOKEN = None


SEEN_FILE = "seen_items.txt"

def load_seen_items():
    """Loads previously checked eBay Item IDs into a set for fast lookup."""
    if not os.path.exists(SEEN_FILE):
        return set()
    with open(SEEN_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())

def mark_item_seen(item_id):
    """Appends a new Item ID to the text file instantly."""
    with open(SEEN_FILE, "a") as f:
        f.write(f"{item_id}\n")

# ==========================================
# 🔄 AUTO-REFRESH MECHANISMS
# ==========================================
def refresh_ebay_token():
    print("🔄 Generating fresh eBay Developer Token...")
    import base64
    auth_str = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {b64_auth}"
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope"
    }
    
    try:
        resp = requests.post(url, headers=headers, data=data, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("access_token")
        else:
            print(f"  [X] Failed to refresh eBay: {resp.text}")
    except Exception as e:
        print(f"  [X] eBay Auth Error: {e}")
    return None

# Use the original temporary Chrome context; keep it open for this run.
CL_LOGIN_TIMEOUT_SECONDS = 180


def cl_token_expiry(token):
    """Read expiry only; Card Ladder still validates the token on each request."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        if claims.get("aud") != "cardladder-71d53":
            return 0.0
        return float(claims.get("exp", 0))
    except (AttributeError, IndexError, ValueError, TypeError):
        return 0.0


class CardLadderSession:
    """One browser and token stream for the entire run; no relaunch on refresh."""

    def __init__(self, manual_login=False):
        self.manual_login = manual_login
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.token = None
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.pending = set()
        self.diagnostics = set()
        self.login_finished = False

    def accept_token(self, token):
        global LIVE_CL_TOKEN
        if isinstance(token, str) and cl_token_expiry(token) > time.time() + 300:
            self.token = token
            LIVE_CL_TOKEN = token
            self.ready.set()

    async def handle_response(self, response):
        try:
            parsed = urlparse(response.url)
            is_auth = (
                parsed.hostname == "identitytoolkit.googleapis.com"
                and parsed.path in (
                    "/v1/accounts:signInWithPassword",
                    "/v1/accounts:signInWithCustomToken",
                )
            ) or (
                parsed.hostname == "securetoken.googleapis.com"
                and parsed.path == "/v1/token"
            )
            is_cl = parsed.hostname == "us-central1-cardladder-71d53.cloudfunctions.net"
            if not (is_auth or is_cl):
                return
            if response.status >= 400:
                key = (parsed.hostname, parsed.path, response.status)
                if key not in self.diagnostics:
                    self.diagnostics.add(key)
                    print(f"  Login diagnostic: {parsed.path} returned HTTP {response.status}")
                return
            if is_auth:
                data = await response.json()
                self.accept_token(data.get("idToken") or data.get("id_token"))
            elif 200 <= response.status < 300:
                auth = await response.request.header_value("authorization")
                if auth and auth.lower().startswith("bearer "):
                    self.accept_token(auth[7:].strip())
        except asyncio.CancelledError:
            raise
        except Exception:
            # A response may disappear when the browser is closed.
            return

    def on_response(self, response):
        task = asyncio.create_task(self.handle_response(response))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def get_token(self):
        if self.closed.is_set():
            print("Card Ladder browser was closed. Stopping without reopening it.")
            return None
        if self.login_finished and cl_token_expiry(self.token) > time.time() + 300:
            return self.token

        self.ready.clear()
        waiters = []
        try:
            if self.context is None:
                print("Opening Card Ladder using the original Chrome launch setup...")
                print("Complete any verification or login in this window; keep it open while scanning.")
                if self.manual_login:
                    print("MANUAL LOGIN: the script will not type or submit. Enter your details and click Log in yourself.")
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(
                    headless=False, channel="chrome",
                )
                self.page = await self.browser.new_page()
                self.context = self.page.context
                self.context.on("response", self.on_response)
                self.context.on("close", lambda *_: self.closed.set())
                self.page.on("close", lambda *_: self.closed.set())
                await self.page.goto("https://app.cardladder.com/login", wait_until="domcontentloaded")
                try:
                    await asyncio.wait_for(self.ready.wait(), timeout=8)
                except asyncio.TimeoutError:
                    pass
                if not self.manual_login and not self.ready.is_set() and not self.page.is_closed():
                    email = self.page.locator("input[type='email']").first
                    password = self.page.locator("input[type='password']").first
                    if await email.is_visible() and await password.is_visible():
                        await email.fill(CL_EMAIL)
                        await password.fill(CL_PASSWORD)
                        await password.press("Enter")  # At most one automatic submission.
            else:
                print("Waiting for a fresh token in the existing Card Ladder window.")

            print("Waiting up to 3 minutes. If signed in already, open a card in the app.")
            waiters = [asyncio.create_task(self.ready.wait()),
                       asyncio.create_task(self.closed.wait())]
            await asyncio.wait(waiters, timeout=CL_LOGIN_TIMEOUT_SECONDS,
                               return_when=asyncio.FIRST_COMPLETED)
            if self.closed.is_set() or not self.ready.is_set():
                print("No current token captured. Stopping; no automatic login retry.")
                return None
            if not self.login_finished:
                try:
                    await self.page.wait_for_function(
                        """() => !location.pathname.includes('/login') &&
                        !Array.from(document.querySelectorAll('input[type=password]'))
                            .some(el => el.getClientRects().length > 0)""",
                        timeout=15000,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    print("A token arrived, but the page did not finish login. Stopping.")
                    return None
                self.login_finished = True
            return self.token
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Browser login failed ({type(exc).__name__}).")
            print("Keep the page error/HTTP diagnostic. No automatic retry will be made.")
            return None
        finally:
            for task in waiters:
                task.cancel()
            if waiters:
                await asyncio.gather(*waiters, return_exceptions=True)

    async def close(self):
        tasks = list(self.pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.browser is not None:
                await self.browser.close()
        finally:
            if self.playwright is not None:
                await self.playwright.stop()


CL_SESSION = None


async def refresh_cl_token():
    if CL_SESSION is None:
        raise RuntimeError("Start the scanner through main() to create its browser session.")
    return await CL_SESSION.get_token()

# ==========================================
# 🚀 CORE ENGINE
# ==========================================
def get_ebay_data(offset=0):
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
    headers = {
        "Authorization": f"Bearer {LIVE_EBAY_TOKEN}",
        "Content-Type": "application/json"
    }
    params = {
        "q": "(Topps,bowman,panini,prizm,select,leaf,optic,contenders,flawless,immaculate,national treasures) ",
        "category_ids": "213",
        # ⚡ MAXIMUM PRICE CAPPED AT $500 AND COMC BLOCKED
        "filter": "price:[..1000],priceCurrency:USD,sellers:{!comc_consignment}",
        "aspect_filter": "categoryId:213,Graded:{Yes}",
        # ⚡ SORTING HIGHEST TO LOWEST
        "sort": "-price",
        "limit": 50,
        "offset": offset 
    }
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            return response.json().get("itemSummaries", [])
        return []
    except Exception:
        return []
    
async def extract_cert_from_image(image_url):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {OPENAI_API_KEY}"
    }
    payload = {
        "model": "gpt-4o",
        "response_format": { "type": "json_object" }, 
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text", 
                        "text": "Analyze this graded sports card slab. Identify the grading company (PSA, BGS, SGC, or CGC), the certification number, and the player's last name printed on the label. The certification number is the unique barcode serial number (7 to 11 digits). PSA certs are strictly 8 or 9 digits. If the image is NOT a graded slab, or you cannot find a valid barcode, return null for all fields. Return ONLY a JSON object in this exact format: {\"grader\": \"PSA\", \"cert\": \"12345678\", \"player\": \"Skenes\"} or {\"grader\": null, \"cert\": null, \"player\": null}."
                    },
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ],
        "max_tokens": 100,
        "temperature": 0.0
    }
    try:
        response = await asyncio.to_thread(requests.post, "https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=15)
        if response.status_code != 200:
            return None, None, None
        
        raw_content = response.json()['choices'][0]['message']['content']
        cleaned_content = raw_content.replace('```json', '').replace('```', '').strip()
        result = json.loads(cleaned_content)
        
        if not isinstance(result, dict):
            return None, None, None
            
        grader = result.get("grader")
        cert = result.get("cert")
        player = result.get("player")
        
        if cert and grader in ["PSA", "BGS", "SGC", "CGC"]:
            return str(cert), grader, str(player) if player else None
        return None, None, None
    except Exception:
        return None, None, None

async def get_cl_profile_from_cert(cert_number, grader):
    await asyncio.sleep(0.5) 
    url = "https://us-central1-cardladder-71d53.cloudfunctions.net/httpcertlookup"
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {LIVE_CL_TOKEN}",
        "content-type": "application/json",
        "origin": "https://app.cardladder.com",
        "user-agent": "Mozilla/5.0"
    }
    clean_cert = re.sub(r'\D', '', str(cert_number))
    payload = {
        "data": {
            "cert": clean_cert,
            "grader": grader.lower(),
            "includeProfile": True
        }
    }
    try:
        response = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"      [DEBUG] CRITICAL API REJECTION ({response.status_code}): {response.text}")
            return None, None, None, None
            
        data = response.json().get("result", {})
        profile_id = data.get("profileId")
        grade = data.get("grade")
        autograph_grade = data.get("autographGrade")
        profile_info = data.get("profile") or {}
        cl_player = profile_info.get("player") or profile_info.get("name") or profile_info.get("title") or data.get("player")
        
        if profile_id and grade:
            return profile_id, grade, autograph_grade, cl_player
    except Exception as e:
        print(f"  [X] API Translation Error: {e}")
    return None, None, None, None 

async def get_card_ladder_value(profile_id, grader, grade, autograph_grade):
    url = "https://us-central1-cardladder-71d53.cloudfunctions.net/httpcardestimate"
    headers = {
        "accept": "*/*",
        "authorization": f"Bearer {LIVE_CL_TOKEN}",
        "content-type": "application/json",
        "origin": "https://app.cardladder.com",
        "user-agent": "Mozilla/5.0"
    }
    payload = {
        "data": {
            "profileId": profile_id,
            "grader": grader.lower(),
            "grade": grade,
            "qualifierType": None,
            "autographGrade": autograph_grade 
        }
    }
    try:
        response = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=10)
        if response.status_code == 200:
            data = response.json()
            result_data = data.get("result", 0.0)
            if result_data is None:
                return 0.0
            if isinstance(result_data, dict):
                cl_value = result_data.get("value") or result_data.get("price") or result_data.get("estimatedValue") or 0.0
                return float(cl_value)
            return float(result_data)
    except Exception:
        pass
    return 0.0

def send_discord_alert(title, price, cl_value, cert, grader, item_url, profit, margin_pct):
    payload = {
        "embeds": [{
            "title": f"🚨 DEEP SWEEP DEAL FOUND: {title}",
            "url": item_url,
            "color": 15105570,
            "fields": [
                {"name": f"{grader} Cert #", "value": str(cert), "inline": True},
                {"name": "eBay Price", "value": f"${price:.2f}", "inline": True},
                {"name": "Card Ladder Comp", "value": f"${cl_value:.2f}", "inline": True},
                {"name": "Est. Profit", "value": f"${profit:.2f}", "inline": True},
                {"name": "Est. ROI", "value": f"{margin_pct:.1f}%", "inline": True}
            ]
        }]
    }
    requests.post(DISCORD_WEBHOOK_URL, json=payload)

async def process_listing(item, semaphore, scanned_certs, seen_ids):
    async with semaphore:
        if CL_SESSION is not None and CL_SESSION.closed.is_set():
            raise RuntimeError("Card Ladder browser closed; scanner stopped.")
        item_id = item.get("itemId")
        
        # Mark as processed in text file and memory set
        if item_id:
            mark_item_seen(item_id)
            seen_ids.add(item_id)

        # THE MASTER SPEED LIMIT (DO NOT CHANGE)
        await asyncio.sleep(3.0)

        title = item.get("title", "Unknown")
        price = float(item.get("price", {}).get("value", 0))
        raw_image_url = item.get("image", {}).get("imageUrl")
        item_url = item.get("itemWebUrl")
        
        if not raw_image_url or price == 0:
            return

        high_res_url = re.sub(r's-l\d+', 's-l1600', raw_image_url)
        cert, grader, vision_player = await extract_cert_from_image(high_res_url)
        
        if not cert or not grader:
            return
            
        if vision_player and len(vision_player) > 2:
            clean_vision = re.sub(r'[^a-zA-Z]', '', vision_player).lower()
            clean_title = re.sub(r'[^a-zA-Z0-9 ]', '', title).lower()
            if clean_vision not in clean_title:
                return

        print(f"  └─ Extracted: {title[:30]}... -> Cert: {cert} ({grader})")
        
        if cert in scanned_certs:
            cached_data = scanned_certs[cert]
            if isinstance(cached_data, dict):
                cl_value = cached_data.get("value", 0.0)
                cl_player = cached_data.get("player")
            else:
                cl_value = cached_data
                cl_player = None
            print(f"      [>] {cert} (Session Cache): CL Value ${cl_value:.2f} | eBay Price ${price:.2f}")
        else:
            profile_id, grade, autograph_grade, cl_player = await get_cl_profile_from_cert(cert, grader)
            if profile_id and grade:
                cl_value = await get_card_ladder_value(profile_id, grader, grade, autograph_grade)
                scanned_certs[cert] = {"value": cl_value, "player": cl_player}
                print(f"      [>] {cert} (Card Ladder API): CL Value ${cl_value:.2f} | eBay Price ${price:.2f}")
            else:
                cl_value = 0.0
                cl_player = None
                scanned_certs[cert] = {"value": 0.0, "player": None}

        if cl_player:
            player_tokens = [t.lower() for t in re.findall(r'[a-zA-Z]{3,}', cl_player)]
            title_lower = title.lower()
            if player_tokens and not any(token in title_lower for token in player_tokens):
                print(f"      [X] Cert Mismatch: Card Ladder '{cl_player}' vs eBay '{title[:30]}...'")
                return

        if cl_value > 0:
            payout = cl_value * 0.90
            profit = payout - price
            margin_pct = (profit / price) * 100 if price > 0 else 0

            if profit >= MIN_PROFIT_DOLLARS and (profit / price) >= MIN_PROFIT_MARGIN:
                print(f"  [$$$] QUALIFIED SWEEP DEAL! {title[:30]}... (Profit: ${profit:.2f} | ROI: {margin_pct:.1f}%)")
                await asyncio.to_thread(send_discord_alert, title, price, cl_value, cert, grader, item_url, profit, margin_pct)

async def sweep_loop():
    global LIVE_EBAY_TOKEN, LIVE_CL_TOKEN
    
    print("🧹 Starting Deep Sweep Arbitrage Engine...")
    scanned_certs = {}
    
    # ⚡ Load seen IDs from permanent file
    seen_ids = load_seen_items()
    print(f"🧠 Loaded {len(seen_ids)} previously scanned items from seen_items.txt")
    
    # FORCED TO 1 TO KEEP A STEADY PACE
    semaphore = asyncio.Semaphore(1) 
    
    # ⚡ Apply the offset jump to start where you left off
    offset = MANUAL_OFFSET_JUMP
    max_offset = 100000 # eBay API max offset limit
    next_refresh_at = 0
    
    if offset > 0:
        print(f"⏩ JUMPING PAST FIRST {offset} LISTINGS TO RESUME SWEEP...")
    
    while offset < max_offset:
        current_time = time.time()
        
        # A failed login stops the scanner instead of opening browsers repeatedly.
        if current_time >= next_refresh_at or not LIVE_EBAY_TOKEN or not LIVE_CL_TOKEN:
            LIVE_CL_TOKEN = await refresh_cl_token()
            if not LIVE_CL_TOKEN:
                print("Card Ladder login failed. Fix verification before restarting.")
                return
            LIVE_EBAY_TOKEN = await asyncio.to_thread(refresh_ebay_token)
            if not LIVE_EBAY_TOKEN:
                print("eBay authentication failed. Stopping sweep.")
                return
            # A reused browser token can have less than one hour remaining.
            next_refresh_at = min(time.time() + 3300, cl_token_expiry(LIVE_CL_TOKEN) - 120)
            print("Credentials secured; starting scan.")

        current_page = (offset // 50) + 1
        print(f"\n📄 Pulling eBay Page {current_page} (Offset: {offset})...")
        
        listings = await asyncio.to_thread(get_ebay_data, offset)
        
        if not listings:
            print("End of search results or API error. Stopping sweep.")
            break
            
        unique_listings = []
        for item in listings:
            item_id = item.get("itemId")
            if item_id and item_id not in seen_ids:
                unique_listings.append(item)
            else:
                print(f"  ⏩ Skipping {item_id} (Already processed)")
            
        print(f"⚡ Batch Processing {len(unique_listings)} new unique listings...")
        if unique_listings:
            await asyncio.gather(*(process_listing(item, semaphore, scanned_certs, seen_ids) for item in unique_listings))
        
        offset += 50
        print("\nSleeping for 10 seconds before turning the next page...")
        await asyncio.sleep(10)
        
    print("\n✅ Deep Sweep Complete!")

async def login_test():
    token = await refresh_cl_token()
    if token:
        print("LOGIN TEST PASSED: captured a current Card Ladder token.")
        print("Card lookup access is not tested here. No scan or Discord alert was sent.")
        return 0
    print("LOGIN TEST FAILED: no current token captured.")
    return 1


async def main():
    global CL_SESSION
    import argparse
    parser = argparse.ArgumentParser(description="Graded-card deal scanner")
    parser.add_argument("--login-test", action="store_true", help="Test login only")
    parser.add_argument("--scan", action="store_true", help="Continue into scanning after a successful login test")
    parser.add_argument("--manual-login", action="store_true", help="Leave all typing and login submission to you")
    args = parser.parse_args()
    CL_SESSION = CardLadderSession(manual_login=args.manual_login)
    try:
        if args.login_test:
            result = await login_test()
            if result or not args.scan:
                return result
            print("Continuing into scan using the SAME browser and captured token.")
        await sweep_loop()
        return 0
    finally:
        await CL_SESSION.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl+C).")
    except RuntimeError as exc:
        print(str(exc))
        sys.exit(1)
