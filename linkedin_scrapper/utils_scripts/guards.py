# utils_scripts/guards.py
import asyncio
import random 
import re
import time
from contextlib import asynccontextmanager
from playwright.async_api import Page

# --- Détection UI / URL ---
CAPTCHA_PATTERNS = [
    r"/checkpoint/", r"/uas/captcha", r"hcaptcha", r"recaptcha",
    r"security-verification", r"challenge", r"authwall"
]
CAPTCHA_TEXT = [
    "Are you a human", "Security check", "Vérification de sécurité",
    "Confirmez que vous n’Ãªtes pas un robot", "Checkpoint"
]

def _contains_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)

async def is_captcha_or_checkpoint(page: Page) -> bool:
    url = page.url or ""
    if _contains_any(url, CAPTCHA_PATTERNS):
        return True
    # Cherche des indices DOM (iframe hcaptcha, messages)
    if await page.query_selector("iframe[src*='hcaptcha'], iframe[src*='recaptcha']"):
        return True
    txt = (await page.content())[:50_000]  # ne lis pas tout
    if _contains_any(txt, CAPTCHA_TEXT):
        return True
    return False

# --- Watchers réseau (429/403) ---
class HealthCounters:
    def __init__(self) -> None:
        self.http_429 = 0
        self.http_403 = 0
        self.captchas = 0

    def score(self) -> int:
        # pondère plus fort captcha/429
        return self.captchas * 3 + self.http_429 * 2 + self.http_403

def attach_response_watchers(page: Page, counters: HealthCounters):
    def _on_response(resp):
        try:
            if "linkedin.com" in (resp.url or ""):
                if resp.status == 429:
                    counters.http_429 += 1
                elif resp.status == 403:
                    counters.http_403 += 1
        except Exception:
            pass
    page.on("response", _on_response)

# --- Backoff exponentiel avec jitter ---
async def backoff_sleep(base: float = 5.0, factor: float = 2.0, attempt: int = 1, cap: float = 900.0):
    # base en secondes, cap ~15 min
    t = min(base * (factor ** max(0, attempt - 1)), cap)
    # jitter plein (0.5xâ€“1.5x)
    t *= random.uniform(0.5, 1.5)
    await asyncio.sleep(t)

# --- Rate limiter simple (token bucket) ---
class AsyncRateLimiter:
    """Capacité tokens, refill par seconde. await limiter.acquire() avant une action réseau."""
    def __init__(self, capacity: int, refill_per_sec: float):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_per_sec = refill_per_sec
        self._last = time.perf_counter()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.perf_counter()
            elapsed = now - self._last
            self._last = now
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            if self.tokens < 1.0:
                need = 1.0 - self.tokens
                wait = need / self.refill_per_sec if self.refill_per_sec > 0 else 0.5
                await asyncio.sleep(wait)
                self.tokens = 0.0
            else:
                self.tokens -= 1.0

# --- Circuit breaker par compte ---
class CircuitBreaker:
    def __init__(self, open_after_score: int = 3, sleep_when_open_s: int = 1800):
        self.open_after_score = open_after_score
        self.sleep_when_open_s = sleep_when_open_s
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return time.time() < self._open_until

    def maybe_open(self, health: HealthCounters):
        if health.score() >= self.open_after_score:
            self._open_until = time.time() + self.sleep_when_open_s
            return True
        return False

    def reset(self):
        self._open_until = 0.0

@asynccontextmanager
async def guarded_action(page: Page, counters: HealthCounters, limiter: AsyncRateLimiter):
    # A appeler autour d'une navigation ou extraction "couteuse"
    await limiter.acquire()
    yield
    # Après l’action : check captcha
    if await is_captcha_or_checkpoint(page):
        counters.captchas += 1



