

import asyncio
import base64
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse


class SeleniumCardLadderSession:
    """Keep one Chrome session open and observe its successful auth responses."""

    def __init__(self, manual_login=False):
        self.manual_login = manual_login
        self._auto_login_attempted = False
        self.token = None
        self.closed = asyncio.Event()
        self.login_finished = False
        self._driver = None
        self._window = None
        self._monitor = None
        self._page_ready = False
        self._closing = False
        self._requests = {}
        self._diagnostics = set()
        # Serialize all WebDriver calls on one worker; scanning stays async.
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def _call(self, function):
        return await asyncio.get_running_loop().run_in_executor(self._executor, function)

    def accept_token(self, token):
        if isinstance(token, str) and scanner.cl_token_expiry(token) > time.time() + 300:
            self.token = token
            scanner.LIVE_CL_TOKEN = token

    @staticmethod
    def _endpoint(url):
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return None, parsed.path
        if (parsed.hostname == "identitytoolkit.googleapis.com" and parsed.path in (
            "/v1/accounts:signInWithPassword", "/v1/accounts:signInWithCustomToken"
        )) or (parsed.hostname == "securetoken.googleapis.com" and parsed.path == "/v1/token"):
            return "auth", parsed.path
        if parsed.hostname == "us-central1-cardladder-71d53.cloudfunctions.net":
            return "cardladder", parsed.path
        return None, parsed.path

    @staticmethod
    def _bearer(headers):
        for name, value in headers.items():
            if name.lower() == "authorization" and isinstance(value, str):
                if value.lower().startswith("bearer "):
                    return value[7:].strip()
        return None

    def _start_browser(self):
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException

        options = webdriver.ChromeOptions()
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        self._driver = webdriver.Chrome(options=options)
        self._window = self._driver.current_window_handle
        self._driver.set_page_load_timeout(45)
        self._driver.execute_cdp_cmd("Network.enable", {})
        try:
            self._driver.get("https://app.cardladder.com/login")
        except TimeoutException:
            print("Page load timed out. If the login page is visible, complete login there.")

    def _read_browser(self):
        """Worker thread: read logs and return only tokens and safe diagnostics."""
        from selenium.common.exceptions import JavascriptException, WebDriverException

        if self._window not in self._driver.window_handles:
            return [], [], False, True
        tokens, diagnostics = [], []
        for entry in self._driver.get_log("performance"):
            try:
                message = json.loads(entry["message"])["message"]
                method, params = message["method"], message.get("params", {})
                request_id = params.get("requestId")
                if method == "Network.requestWillBeSent":
                    request = params["request"]
                    kind, path = self._endpoint(request.get("url", ""))
                    # Redirects can reuse an ID. Drop previous response state.
                    self._requests.pop(request_id, None)
                    if kind:
                        self._requests[request_id] = {
                            "kind": kind, "path": path,
                            "token": self._bearer(request.get("headers", {})),
                            "success": False,
                        }
                elif method == "Network.responseReceived":
                    response = params["response"]
                    kind, path = self._endpoint(response.get("url", ""))
                    if not kind:
                        self._requests.pop(request_id, None)
                        continue
                    state = self._requests.setdefault(request_id, {})
                    status = int(response["status"])
                    state.update(kind=kind, path=path, success=200 <= status < 300)
                    if status >= 400:
                        diagnostics.append(f"{path} returned HTTP {status}")
                    elif state["success"] and kind == "cardladder":
                        token = state.get("token") or self._bearer(response.get("requestHeaders", {}))
                        if token:
                            tokens.append(token)
                elif method == "Network.loadingFinished":
                    state = self._requests.pop(request_id, None)
                    if state and state.get("kind") == "auth" and state.get("success"):
                        try:
                            body = self._driver.execute_cdp_cmd(
                                "Network.getResponseBody", {"requestId": request_id}
                            )
                            raw = body["body"]
                            if body.get("base64Encoded"):
                                raw = base64.b64decode(raw).decode("utf-8")
                            data = json.loads(raw)
                            if isinstance(data, dict):
                                tokens.append(data.get("idToken") or data.get("id_token"))
                        except (WebDriverException, ValueError, KeyError, UnicodeError):
                            diagnostics.append("Login response unavailable; open a card in the signed-in app.")
                elif method == "Network.loadingFailed":
                    self._requests.pop(request_id, None)
            except (ValueError, KeyError, TypeError):
                continue
        # Bound pending request metadata; no raw network logs are retained.
        while len(self._requests) > 1000:
            self._requests.pop(next(iter(self._requests)))
        try:
            page_ready = bool(self._driver.execute_script("""
                return location.origin === 'https://app.cardladder.com'
                    && document.readyState !== 'loading'
                    && !location.pathname.includes('/login')
                    && !Array.from(document.querySelectorAll('input[type=password]'))
                        .some(el => el.getClientRects().length > 0);
            """))
        except JavascriptException:
            page_ready = False
        return tokens, diagnostics, page_ready, False

    def _try_auto_login(self):
        """Wait for usable fields, then fill and submit once; no CAPTCHA interaction."""
        from selenium.webdriver.common.keys import Keys
        from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

        if self.manual_login or self._auto_login_attempted:
            return None
        current = urlparse(self._driver.current_url)
        if (current.scheme != "https" or current.hostname != "app.cardladder.com"
                or not current.path.startswith("/login")):
            return None
        try:
            email = next((el for el in self._driver.find_elements("css selector", "input[type='email']")
                          if el.is_displayed() and el.is_enabled()), None)
            password = next((el for el in self._driver.find_elements("css selector", "input[type='password']")
                             if el.is_displayed() and el.is_enabled()), None)
        except StaleElementReferenceException:
            return None  # Form is still rendering; no typing or submission yet.
        if email is None or password is None:
            return None
        # Mark before interacting so even an uncertain submission is never repeated.
        self._auto_login_attempted = True
        try:
            email.clear()
            email.send_keys(scanner.CL_EMAIL)
            password.clear()
            password.send_keys(scanner.CL_PASSWORD)
            password.send_keys(Keys.ENTER)
        except WebDriverException as exc:
            return f"Automatic login stopped ({type(exc).__name__}); no automatic retry. Check the browser."
        return "Automatic login submitted once. Waiting for Card Ladder."

    async def _watch_browser(self):
        try:
            while not self._closing:
                tokens, diagnostics, page_ready, closed = await self._call(self._read_browser)
                if closed:
                    self.closed.set()
                    return
                for diagnostic in diagnostics:
                    if diagnostic not in self._diagnostics:
                        self._diagnostics.add(diagnostic)
                        print("  Login diagnostic:", diagnostic)
                for token in tokens:
                    self.accept_token(token)
                self._page_ready = page_ready
                if not self.manual_login and not self._auto_login_attempted and not self.token:
                    message = await self._call(self._try_auto_login)
                    if message:
                        print(message)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Selenium session stopped ({type(exc).__name__}); no automatic restart.")
            self.closed.set()

    async def get_token(self):
        if self.closed.is_set():
            print("Card Ladder browser closed or disconnected. Stopping.")
            return None
        if self.login_finished and scanner.cl_token_expiry(self.token) > time.time() + 300:
            return self.token
        if self._driver is None:
            if not self.manual_login and not all(
                isinstance(getattr(scanner, name, None), str) and getattr(scanner, name)
                for name in ("CL_EMAIL", "CL_PASSWORD")
            ):
                print("Set CL_EMAIL and CL_PASSWORD in your local .env before using automatic login.")
                self.closed.set()
                return None
            print("Opening Card Ladder in Chrome using Selenium...")
            if self.manual_login:
                print("MANUAL LOGIN: enter your details and click Log in yourself.")
            else:
                print("AUTO LOGIN: using CL_EMAIL and CL_PASSWORD from your local environment; one submission.")
            print("Keep this Chrome window open while scanning. No automatic login retries.")
            try:
                await self._call(self._start_browser)
            except asyncio.CancelledError:
                raise
            except ImportError:
                print("Install Selenium: python -m pip install --upgrade selenium")
                self.closed.set()
                return None
            except Exception as exc:
                print(f"Chrome startup failed ({type(exc).__name__}).")
                self.closed.set()
                return None
            self._monitor = asyncio.create_task(self._watch_browser())
        else:
            print("Waiting for a fresh token in the same Chrome window.")
        timeout = getattr(scanner, "CL_LOGIN_TIMEOUT_SECONDS", 180)
        print(f"Waiting up to {timeout} seconds. After signing in, open a card in the app.")
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.closed.is_set():
                print("Card Ladder browser closed or disconnected. Stopping.")
                return None
            if self._page_ready and scanner.cl_token_expiry(self.token) > time.time() + 300:
                self.login_finished = True
                return self.token
            await asyncio.sleep(0.25)
        print("No completed login with a current token captured. Stopping without retrying.")
        return None

    def _quit_browser(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        self._requests.clear()

    async def close(self):
        if self._closing:
            return
        self._closing = True
        self.closed.set()
        if self._monitor is not None:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)
        try:
            await self._call(self._quit_browser)
        finally:
            self._executor.shutdown(wait=False)
            self.token = None
            scanner.LIVE_CL_TOKEN = None


def run():
    global scanner
    try:
        import card_scanner as scanner
    except ModuleNotFoundError as exc:
        if exc.name == "card_scanner":
            print("Place this file in the SAME folder as your existing card_scanner.py.")
        else:
            print(f"Your scanner requires the missing package: {exc.name}")
            print("Use the same virtual environment you normally use for card_scanner.py.")
        return 1
    if not all(hasattr(scanner, name) for name in ("main", "CardLadderSession", "cl_token_expiry")):
        print("This scanner version has a different login interface. Share the current code before changing it.")
        return 1
    scanner.CardLadderSession = SeleniumCardLadderSession
    try:
        return asyncio.run(scanner.main())
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl+C).")
        return 130
    except RuntimeError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(run())
