# replay/runner.py
import asyncio, re, sys, orjson, time
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

from playwright.async_api import async_playwright, Page

# ===== KNOBS you can tune =====
HEADLESS = False
VIEWPORT = {"width": 1600, "height": 900}

# Pace/visibility
SLOWMO_MS = 150               # slows all Playwright ops (visible human-like)
STEP_DELAY_MS = 350           # added after each step
USE_TIMESTAMP_PACING = True   # respect original timing gaps between events
TIMESCALE = 1.0               # 1.0 = real-time, 0.5 = 2x faster, 2.0 = 2x slower
MAX_GAP_MS = 1500             # cap very long idle times from the recording

# Waiting strategy
WAIT_AFTER_CLICK_MS = 250
WAIT_FOR_URL_CHANGE = True
WAIT_FOR_NETWORKIDLE = True
NETWORKIDLE_TIMEOUT_MS = 8000
DOMCONTENT_TIMEOUT_MS = 8000

# Replay end behavior
FINAL_PAUSE_SEC = 15          # keep browser open at the end (set 0 to auto-close)

# Optional trace (view with: playwright show-trace trace.zip)
TRACE_ON = False
TRACE_PATH = "trace.zip"

REDACTED = "••••••"

# ===== Optional: credentials autofill during replay =====
try:
    from config import USERNAME, PASSWORD
except Exception:
    USERNAME = ""
    PASSWORD = ""

try:
    from config import USERNAME_SELECTOR, PASSWORD_SELECTOR  # optional
except Exception:
    USERNAME_SELECTOR = ""
    PASSWORD_SELECTOR = ""

EMAIL_PAT = re.compile(r"(e[-\s]*mail|user(name)?|login|account)", re.I)
PASS_PAT  = re.compile(r"pass(word)?", re.I)

@dataclass
class ElementInfo:
    tag: Optional[str] = None
    id: Optional[str] = None
    classes: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    role: Optional[str] = None
    aria_label: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    value_preview: Optional[str] = None
    selectors_css: Optional[str] = None
    selectors_xpath: Optional[str] = None

def _el_from_event(ev: Dict[str, Any]) -> ElementInfo:
    el = (ev.get("el") or {})
    sel = el.get("selectors") or {}
    return ElementInfo(
        tag = el.get("tag"),
        id = el.get("id"),
        classes = el.get("classes"),
        name = el.get("name"),
        type = el.get("type"),
        role = el.get("role"),
        aria_label = el.get("ariaLabel") or el.get("aria_label"),
        title = el.get("title"),
        text = el.get("text"),
        value_preview = el.get("value_preview"),
        selectors_css = sel.get("css"),
        selectors_xpath = sel.get("xpath"),
    )

def _role_to_aria(role: Optional[str]) -> Optional[str]:
    return role or None

async def _best_locator(page: Page, info: ElementInfo):
    # Priority: explicit CSS → #id → role+name → visible text → XPath → [name]
    cands = []

    if info.selectors_css:
        cands.append(page.locator(info.selectors_css))
    if info.id:
        cands.append(page.locator(f"#{info.id}"))

    role = _role_to_aria(info.role)
    name_source = info.aria_label or info.title or info.text or ""
    if role and name_source:
        cands.append(page.get_by_role(role, name=re.compile(re.escape(name_source), re.I)))

    if (info.tag in {"button", "a"}) and info.text:
        cands.append(page.get_by_role("button", name=re.compile(re.escape(info.text), re.I)))
        cands.append(page.get_by_text(re.compile(re.escape(info.text), re.I)))

    if info.selectors_xpath:
        cands.append(page.locator(f"xpath={info.selectors_xpath}"))

    if info.name:
        cands.append(page.locator(f'[name="{info.name}"]'))

    # wait for first visible & enabled candidate
    for c in cands:
        try:
            await c.first.wait_for(state="visible", timeout=3000)
            # ensure it’s interactable
            try:
                await c.first.wait_for(state="attached", timeout=1000)
            except Exception:
                pass
            return c.first
        except Exception:
            continue

    return cands[0] if cands else page.locator("html")

async def _maybe_wait_for_nav(page: Page, prev_url: str):
    changed = False
    if WAIT_FOR_URL_CHANGE:
        try:
            changed = await _wait_for_url_change(page, prev_url, timeout_ms=NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            changed = False
    if WAIT_FOR_NETWORKIDLE:
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
        except Exception:
            pass
    if not changed:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=DOMCONTENT_TIMEOUT_MS)
        except Exception:
            pass

async def _wait_for_url_change(page: Page, prev_url: str, timeout_ms: int = 8000) -> bool:
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        if page.url != prev_url:
            return True
        await asyncio.sleep(0.05)
    return False

def _looks_like_email_field(info: ElementInfo) -> bool:
    hay = " ".join(filter(None, [info.id, info.name, info.aria_label, info.title, info.text, info.type]))
    if info.type and info.type.lower() == "email":
        return True
    return bool(EMAIL_PAT.search(hay))

def _looks_like_password_field(info: ElementInfo) -> bool:
    if info.type and info.type.lower() == "password":
        return True
    hay = " ".join(filter(None, [info.id, info.name, info.aria_label, info.title, info.text]))
    return bool(PASS_PAT.search(hay))

async def _fill_locator(locator, value: str) -> bool:
    if not locator:
        return False
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await locator.fill(value)
        return True
    except Exception:
        try:
            await locator.click()
            await locator.fill(value)
            return True
        except Exception:
            try:
                await locator.type(value)
                return True
            except Exception:
                return False

async def _autofill_from_selectors(page: Page) -> bool:
    ok_u = ok_p = False
    if USERNAME_SELECTOR:
        try:
            loc = page.locator(USERNAME_SELECTOR).first
            if await loc.count() > 0 and await _fill_locator(loc, USERNAME):
                print("↪ filled USERNAME via USERNAME_SELECTOR")
                ok_u = True
        except Exception:
            pass
    if PASSWORD_SELECTOR:
        try:
            loc = page.locator(PASSWORD_SELECTOR).first
            if await loc.count() > 0 and await _fill_locator(loc, PASSWORD):
                print("↪ filled PASSWORD via PASSWORD_SELECTOR")
                ok_p = True
        except Exception:
            pass
    return ok_u and ok_p

async def _heuristic_autofill(page: Page) -> bool:
    ok_u = ok_p = False
    u_cands = page.locator(
        'input[type="email"], input[name*="email" i], input[id*="email" i], '
        'input[name*="user" i], input[id*="user" i], input[name*="login" i], input[id*="login" i], '
        '[role="textbox"]'
    )
    try:
        cnt = await u_cands.count()
        for i in range(min(10, cnt)):
            el = u_cands.nth(i)
            try:
                await el.wait_for(state="visible", timeout=800)
                if await _fill_locator(el, USERNAME):
                    print("↪ filled USERNAME via heuristic candidate")
                    ok_u = True
                    break
            except Exception:
                continue
    except Exception:
        pass

    p_cands = page.locator('input[type="password"], input[name*="pass" i], input[id*="pass" i]')
    try:
        cnt = await p_cands.count()
        for i in range(min(10, cnt)):
            el = p_cands.nth(i)
            try:
                await el.wait_for(state="visible", timeout=800)
                if await _fill_locator(el, PASSWORD):
                    print("↪ filled PASSWORD via heuristic candidate")
                    ok_p = True
                    break
            except Exception:
                continue
    except Exception:
        pass
    return ok_u and ok_p

async def maybe_autofill_credentials(page: Page):
    if not (USERNAME and PASSWORD):
        return
    try:
        await page.wait_for_timeout(200)
    except Exception:
        pass
    both = await _autofill_from_selectors(page)
    if both:
        return
    await _heuristic_autofill(page)

def _event_time_ms(ev: Dict[str, Any]) -> Optional[float]:
    # Expect ISO timestamp in ev["t"]; return epoch-ish ms for pacing. If missing, None.
    t = ev.get("t")
    if not t:
        return None
    # Fast parse: yyyy-mm-ddTHH:MM:SS.sssZ
    try:
        # strip Z and split
        ts = t.rstrip("Z")
        date, timepart = ts.split("T")
        y, m, d = (int(x) for x in date.split("-"))
        hh, mm, ss = timepart.split(":")
        hh, mm = int(hh), int(mm)
        if "." in ss:
            ss, frac = ss.split(".")
            sec = int(ss)
            ms = int(float("0."+frac) * 1000)
        else:
            sec = int(ss); ms = 0
        # crude epoch-less relative ms (only used for deltas between consecutive events)
        return ((hh*3600 + mm*60 + sec) * 1000 + ms)
    except Exception:
        return None

async def replay(jsonl_path: str):
    # load events
    events: List[Dict[str, Any]] = []
    with open(jsonl_path, "rb") as f:
        for line in f:
            if line.strip():
                events.append(orjson.loads(line))
    if not events:
        print("No events found in JSONL.")
        return

    # find initial URL
    start_url = None
    for ev in events:
        if ev.get("etype") == "nav" and ev.get("meta", {}).get("reason") in ("load", "popstate"):
            start_url = ev.get("to_url") or ev.get("url")
            break
        if not start_url:
            start_url = ev.get("url")

    # precompute simple pacing timestamps (relative deltas)
    rel_times = []
    if USE_TIMESTAMP_PACING:
        base = _event_time_ms(events[0]) or 0.0
        for ev in events:
            t = _event_time_ms(ev)
            if t is None:
                rel_times.append(None)
            else:
                rel_times.append(max(0.0, (t - base)/TIMESCALE))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, slow_mo=SLOWMO_MS, args=["--start-maximized"])
        context = await browser.new_context(viewport=VIEWPORT)

        # optional trace
        if TRACE_ON:
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)

        page = await context.new_page()

        if start_url:
            print(f"→ navigating to: {start_url}")
            await page.goto(start_url, wait_until="domcontentloaded")

        # initial creds attempt
        await maybe_autofill_credentials(page)

        prev_url = page.url
        start_clock = time.time()

        for i, ev in enumerate(events):
            # ---- pacing (match recorded rhythm) ----
            if USE_TIMESTAMP_PACING and rel_times[i] is not None:
                target_s = rel_times[i] / 1000.0
                elapsed_s = time.time() - start_clock
                if target_s - elapsed_s > 0:
                    gap = min(target_s - elapsed_s, MAX_GAP_MS / 1000.0)
                    await asyncio.sleep(gap)

            # ---- event handling ----
            et = ev.get("etype")
            if et == "nav":
                to_url = ev.get("to_url") or ev.get("url")
                if to_url and page.url != to_url:
                    print(f"[{i}] nav → {to_url}")
                    try:
                        await page.goto(to_url, wait_until="domcontentloaded")
                    except Exception:
                        pass
                await _maybe_wait_for_nav(page, prev_url)
                prev_url = page.url
                await maybe_autofill_credentials(page)
                await page.wait_for_timeout(STEP_DELAY_MS)
                continue

            if et == "click":
                info = _el_from_event(ev)
                loc = await _best_locator(page, info)

                label = (info.text or info.aria_label or info.title or "").strip()
                print(f"[{i}] click on {info.tag or ''} {'#'+info.id if info.id else ''} {label}".strip())
                try:
                    await loc.scroll_into_view_if_needed()
                except Exception:
                    pass
                try:
                    await loc.click()
                except Exception:
                    try:
                        await loc.click(force=True)
                    except Exception as e:
                        print(f"  ! click failed: {e}")

                await page.wait_for_timeout(WAIT_AFTER_CLICK_MS)
                await _maybe_wait_for_nav(page, prev_url)
                prev_url = page.url

                # reattempt creds after transitional buttons
                if re.search(r"(continue|next|login|sign\s*in|submit)", label, re.I):
                    await maybe_autofill_credentials(page)

                await page.wait_for_timeout(STEP_DELAY_MS)
                continue

            if et == "input":
                info = _el_from_event(ev)
                loc = await _best_locator(page, info)
                recorded_val = ev.get("input_value")

                if recorded_val == REDACTED:
                    value_to_use = None
                    if _looks_like_password_field(info):
                        value_to_use = PASSWORD
                    elif _looks_like_email_field(info):
                        value_to_use = USERNAME

                    if value_to_use:
                        print(f"[{i}] input (fill inferred {'PASSWORD' if value_to_use==PASSWORD else 'USERNAME'})")
                        try:
                            await _fill_locator(loc, value_to_use)
                        except Exception as e:
                            print(f"  ! input failed: {e}")
                    else:
                        print(f"[{i}] input (redacted) → cannot infer; skipping")
                else:
                    print(f"[{i}] input → {recorded_val!r}")
                    try:
                        await loc.fill(recorded_val or "")
                    except Exception:
                        try:
                            await loc.click()
                            if recorded_val:
                                await page.keyboard.type(recorded_val)
                        except Exception as e:
                            print(f"  ! input failed: {e}")

                await page.wait_for_timeout(STEP_DELAY_MS)
                continue

            if et == "keydown":
                key = ev.get("key")
                if key in ("Enter", "Tab", "Escape", "ArrowDown", "ArrowUp"):
                    print(f"[{i}] press {key}")
                    try:
                        await page.keyboard.press(key)
                    except Exception:
                        pass
                await page.wait_for_timeout(STEP_DELAY_MS // 2)
                continue

            # ignore change/submit/visibility
            await page.wait_for_timeout(50)

        print("✅ replay complete")

        if TRACE_ON:
            await context.tracing.stop(path=TRACE_PATH)
            print(f"📦 trace saved → {TRACE_PATH} (open with: playwright show-trace {TRACE_PATH})")

        if FINAL_PAUSE_SEC > 0:
            print(f"⏸  keeping window open for {FINAL_PAUSE_SEC}s...")
            await page.wait_for_timeout(FINAL_PAUSE_SEC * 1000)

        await context.close()
        await browser.close()

def _usage():
    print("Usage:\n  python -m replay.runner <path/to/recordings/session-*.jsonl> [--keep-open]")
    sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        _usage()
    keep = any(a == "--keep-open" for a in sys.argv[2:])
    if keep:
        # we're at module scope, so no 'global' needed
        FINAL_PAUSE_SEC = max(FINAL_PAUSE_SEC, 60)
    asyncio.run(replay(sys.argv[1]))
