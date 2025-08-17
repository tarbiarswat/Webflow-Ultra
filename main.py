# main.py
import asyncio
import os
import re
from typing import Any, Dict

from playwright.async_api import (
    async_playwright,
    Page,
    Frame,
    TimeoutError as PWTimeoutError,
)

from config import (
    URL,
    USERNAME,
    PASSWORD,
    HEADLESS,
    VIEWPORT,
    RECORDINGS_DIR,
    STOP_HOTKEY,
)

# Optional site-specific selectors (safe if missing)
try:
    from config import USERNAME_SELECTOR, PASSWORD_SELECTOR  # type: ignore
except Exception:
    USERNAME_SELECTOR = ""
    PASSWORD_SELECTOR = ""

from recorder.writer import JsonlWriter
from recorder.hotkey import StopSignal, attach_hotkey
from recorder.selectors import RECORDER_JS

REDACTED = "••••••"

# --------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------

async def _diag_probe(page: Page):
    print("— Frames detected —")
    for fr in page.frames:
        try:
            print(f"   • name={fr.name or '(none)'}  url={fr.url}")
        except:
            pass
    try:
        ok = await page.evaluate("""() => {
          if (window.__recordEventBridge) {
            window.__recordEventBridge({ etype: "visibility", url: location.href, meta: {probe:true} });
            return true;
          }
          return false;
        }""")
        print(f"→ probe: attempted synthetic event (bridge {'OK' if ok else 'NOT ready'})")
    except Exception as e:
        print(f"→ probe error: {e}")

# --------------------------------------------------------------------
# Recorder attach (ALL frames, before nav, and on re-navigations)
# --------------------------------------------------------------------

# Wrap the recorder so it auto-starts even if SPA replaces the document
REC_STARTER = """
(() => {
  function startIfReady() {
    try {
      if (window.__webflowRecorder && window.__webflowRecorder.start) {
        window.__webflowRecorder.start();
        return true;
      }
    } catch {}
    return false;
  }
  %RECORDER_JS%
  if (!startIfReady()) {
    let tries = 0;
    const t = setInterval(() => {
      tries++;
      if (startIfReady() || tries > 20) clearInterval(t);
    }, 200);
  }
})();
""".replace("%RECORDER_JS%", RECORDER_JS)

async def inject_recorder(page: Page, writer: JsonlWriter):
    async def record_event_binding(source, data: Dict[str, Any]):
        try:
            et = data.get("etype")
            if et in ("input", "change"):
                el = data.get("el") or {}
                if (el.get("type") == "password") or (
                    data.get("input_value") and data["input_value"] != REDACTED
                ):
                    data["input_value"] = REDACTED
                    data["value"] = REDACTED
                    if "el" in data:
                        data["el"]["value_preview"] = REDACTED
        except Exception:
            pass

        writer.write(data)
        if writer.count <= 3:
            print(f"   [rec] {data.get('etype')} @ {data.get('url')}")

    # Make the bridge available to all frames on this page
    await page.expose_binding("__recordEventBridge", record_event_binding)

    # Ensure all future documents (top + iframes) preload the recorder
    page.context.add_init_script(REC_STARTER)

    async def _start_in_frame(fr: Frame):
        try:
            await fr.add_init_script(REC_STARTER)
            await fr.evaluate(
                "(()=>{ if (window.__webflowRecorder && window.__webflowRecorder.start) "
                "window.__webflowRecorder.start(); return true; })()"
            )
            try:
                print(f"   [rec] attached → frame '{fr.name or '(no-name)'}' url={fr.url}")
            except:
                pass
        except Exception as e:
            try:
                print(f"   [rec] attach failed in frame url={getattr(fr, 'url', None)} : {e}")
            except:
                pass

    # Start now in main and existing frames
    await _start_in_frame(page)
    for fr in page.frames:
        if fr is not page.main_frame:
            await _start_in_frame(fr)

    # Re-attach when frames are added or navigated (SSO widgets, etc.)
    page.on("frameattached", lambda fr: asyncio.create_task(_start_in_frame(fr)))
    page.on("framenavigated", lambda fr: asyncio.create_task(_start_in_frame(fr)))

    # Optional: echo browser console for debugging
    page.on("console", lambda msg: print(f"   [console] {msg.type}: {msg.text}"))

# --------------------------------------------------------------------
# Autofill (hardened: frames + strategies + optional explicit selectors)
# --------------------------------------------------------------------

EMAIL_PAT = re.compile(r"e[-\s]*mail|user(name)?|login", re.I)
PASS_PAT  = re.compile(r"pass(word)?", re.I)

async def _visible_first(loc):
    try:
        await loc.first.wait_for(state="visible", timeout=3000)
        return loc.first
    except Exception:
        return None

async def _try_fill(locator, value: str) -> bool:
    if not locator:
        return False
    try:
        is_input_like = await locator.evaluate("""el => {
            if (!el) return false;
            const t = el.tagName?.toLowerCase?.();
            if (t === 'input' || t === 'textarea') return true;
            if (el.isContentEditable) return true;
            const role = el.getAttribute('role');
            return role === 'textbox';
        }""")
        if is_input_like:
            try:
                await locator.fill(value)
            except Exception:
                await locator.click()
                await locator.evaluate(
                    "(el, v) => { el.textContent = v; el.dispatchEvent(new Event('input', {bubbles:true})); }",
                    value,
                )
            return True
        else:
            inner = locator.locator("input,textarea,[contenteditable=''],[contenteditable='true'],[role='textbox']")
            if await inner.count() > 0:
                return await _try_fill(inner.first, value)
            return False
    except Exception:
        return False

async def _find_email_candidates(scope):
    cands = []
    cands.append(await _visible_first(scope.get_by_label(re.compile(r"email|user(name)?|login", re.I))))
    cands.append(await _visible_first(scope.get_by_placeholder(re.compile(r"email|user(name)?|login", re.I))))
    cands.append(await _visible_first(scope.get_by_role("textbox", name=re.compile(r"email|user(name)?|login", re.I))))

    attr_sel = ",".join([
        'input[type="email"]',
        'input[type="text"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[name*="user" i]',
        'input[id*="user" i]',
        'input[name*="login" i]',
        'input[id*="login" i]',
        '[role="textbox"]',
        '[contenteditable=""]',
        '[contenteditable="true"]'
    ])
    loc = scope.locator(attr_sel).filter(has_not=scope.locator(':is([type="hidden"],[aria-hidden="true"])'))
    max_scan = min(10, await loc.count())
    for i in range(max_scan):
        el = loc.nth(i)
        attrs = await el.evaluate("""el => ({
            id: el.id || '',
            name: el.name || '',
            ph: el.getAttribute('placeholder') || '',
            aria: el.getAttribute('aria-label') || '',
            type: el.getAttribute('type') || ''
        })""")
        hay = " ".join(attrs.values())
        if EMAIL_PAT.search(hay) or attrs["type"].lower() == "email":
            cands.append(el)

    uniq = []
    seen = set()
    for el in cands:
        if not el:
            continue
        try:
            box = await el.bounding_box()
            if not box:
                continue
            key = await el.evaluate("el => el.outerHTML.slice(0,200)")
            if key in seen:
                continue
            seen.add(key)
            uniq.append(el)
        except Exception:
            pass
    return uniq

async def _find_password_candidates(scope):
    cands = []
    cands.append(await _visible_first(scope.get_by_label(re.compile(r"pass(word)?", re.I))))
    cands.append(await _visible_first(scope.get_by_placeholder(re.compile(r"pass(word)?", re.I))))
    attr_sel = ",".join([
        'input[type="password"]',
        'input[name*="pass" i]',
        'input[id*="pass" i]'
    ])
    loc = scope.locator(attr_sel)
    max_scan = min(8, await loc.count())
    for i in range(max_scan):
        cands.append(loc.nth(i))

    uniq = []
    seen = set()
    for el in cands:
        if not el:
            continue
        try:
            box = await el.bounding_box()
            if not box:
                continue
            key = await el.evaluate("el => el.outerHTML.slice(0,200)")
            if key in seen:
                continue
            seen.add(key)
            uniq.append(el)
        except Exception:
            pass
    return uniq

async def _scopes(page: Page):
    scopes = [page]
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except PWTimeoutError:
        pass

    for fr in page.frames:
        try:
            u = (fr.url or "").lower()
            n = (fr.name or "").lower()
            if any(k in u for k in ("login", "signin", "auth")) or any(k in n for k in ("login", "signin", "auth")):
                scopes.append(fr)
        except:
            pass
    return scopes

async def try_autofill_login(page: Page) -> bool:
    filled_u = filled_p = False

    # Fast path: explicit selectors from config.py
    if USERNAME_SELECTOR:
        try:
            el = page.locator(USERNAME_SELECTOR)
            if await el.count() > 0:
                await el.first.fill(USERNAME)
                print("✅ Filled username via USERNAME_SELECTOR")
                filled_u = True
        except Exception:
            pass

    if PASSWORD_SELECTOR:
        try:
            el = page.locator(PASSWORD_SELECTOR)
            if await el.count() > 0:
                await el.first.fill(PASSWORD)
                print("✅ Filled password via PASSWORD_SELECTOR")
                filled_p = True
        except Exception:
            pass

    try:
        await page.wait_for_timeout(400)
    except Exception:
        pass

    for scope in await _scopes(page):
        if not filled_u:
            email_cands = await _find_email_candidates(scope)
            for el in email_cands:
                ok = await _try_fill(el, USERNAME)
                if ok:
                    try:
                        snip = await el.evaluate("el => el.outerHTML.slice(0,120)")
                    except Exception:
                        snip = "<element>"
                    print("✅ Filled username/email via:", snip)
                    filled_u = True
                    break

        if not filled_p:
            pw_cands = await _find_password_candidates(scope)
            for el in pw_cands:
                ok = await _try_fill(el, PASSWORD)
                if ok:
                    try:
                        snip = await el.evaluate("el => el.outerHTML.slice(0,120)")
                    except Exception:
                        snip = "<element>"
                    print("✅ Filled password via:", snip)
                    filled_p = True
                    break

        if filled_u and filled_p:
            break

    if not filled_u:
        print("⚠️  Could not locate username/email field after all strategies.")
    if not filled_p:
        print("⚠️  Could not locate password field after all strategies.")

    return filled_u and filled_p

# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

async def main():
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    writer = JsonlWriter(RECORDINGS_DIR)
    stop_signal = StopSignal()
    attach_hotkey(STOP_HOTKEY, stop_signal)
    print(f"⏺  Recording started. Hotkey to stop: {STOP_HOTKEY}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, args=["--start-maximized"])
        context = await browser.new_context(viewport=VIEWPORT, record_video_dir=None)
        page = await context.new_page()

        # IMPORTANT: Inject recorder BEFORE navigation so all future docs/frames preload it
        await inject_recorder(page, writer)

        print(f"→ navigating to: {URL}")
        await page.goto(URL, wait_until="domcontentloaded")

        # Quick diagnostics (frames + synthetic event)
        await _diag_probe(page)

        # Autofill (you click Login so the click is recorded)
        try:
            ok = await try_autofill_login(page)
            if ok:
                print("✅ Autofill complete. Proceed with your flow…")
            else:
                print("ℹ️  Autofill best effort done; proceed manually if needed.")
        except Exception as e:
            print(f"⚠️  Autofill error: {e}. You can still proceed manually; actions will be recorded.")

        # Keep alive until you hit the stop hotkey
        while not stop_signal.triggered:
            await asyncio.sleep(0.1)

        # Stop recorder (best-effort)
        try:
            await page.evaluate("window.__webflowRecorder && window.__webflowRecorder.stop()")
        except Exception:
            pass

        await context.close()
        await browser.close()

    writer.close()
    print(f"💾 Saved recording with {writer.count} events → {writer.path}")
    print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
