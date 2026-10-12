"""
joiner.py — open a Meet URL, join as the host account, sit there, leave.

Why a persistent browser profile (not login-in-service):
  Google aggressively blocks automated logins (CAPTCHA, security
  challenges, unusual-location prompts, 2FA). Performing a login
  inside this service every run would be brittle to the point of
  useless.

  Instead, the developer runs scripts/setup_profile.py once and
  manually signs in as the central host account (SUPORTE_EMAIL in
  .env) in a real browser window. Playwright saves the cookies +
  local storage to BROWSER_PROFILE_DIR. The service then re-uses that
  profile — it opens a "logged in" browser without ever touching the
  login UI.

  Cookies do eventually expire (Google rotates them roughly weekly
  for personal accounts; less aggressively for Workspace). When
  that happens, run setup_profile.py again to refresh.

Why channel="chrome" (real Chrome, not Playwright's Chromium):
  Google refuses sign-in on the bundled automation Chromium with
  "This browser or app may not be secure", and degrades Meet.

Why headless=False (visible browser):
  Meet detects headless Chrome via several signals (missing GPU,
  fixed user agent, missing window properties) and either refuses
  to enable camera/mic OR shows a degraded "browser not supported"
  UI. In practice a visible browser is required.
  On a Linux server, run under Xvfb (virtual framebuffer) so there's
  a "display" for Chrome to draw to — see README for the command.
"""

import asyncio
import logging
import re
import shutil
import tempfile
from typing import Callable, Iterable, Optional

from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeout,
    Page,
)

from . import config, profile_utils

logger = logging.getLogger(__name__)


# Anchored regexes — they match the on/off TOGGLE buttons only.
# Never use a bare "Microphone"/"Camera" label here: it also matches
# the device-selector buttons ("Microphone: Mic not found"), and
# clicking one of those opens a dropdown overlay that swallows every
# subsequent click on "Join now".
_MIC_OFF_PATTERNS = [
    re.compile(r"^(turn off|switch off) microphone", re.I),
    re.compile(r"^mute\b", re.I),
    # Portuguese (pt-BR) fallbacks — in case the host account's Meet UI
    # renders in Portuguese despite the hl=en hint.
    re.compile(r"^desativar microfone", re.I),
    re.compile(r"^desligar microfone", re.I),
    re.compile(r"^silenciar\b", re.I),
]
_CAM_OFF_PATTERNS = [
    re.compile(r"^(turn off|switch off) camera", re.I),
    re.compile(r"^stop video", re.I),
    re.compile(r"^desativar c[âa]mera", re.I),
    re.compile(r"^desligar c[âa]mera", re.I),
    re.compile(r"^(desativar|parar) v[íi]deo", re.I),
]
_JOIN_LABELS = [
    "Join now",
    "Ask to join",
    "Join meeting",
    "Join",
    # Portuguese (pt-BR) fallbacks.
    "Participar agora",
    "Pedir para participar",
    "Participar",
    "Entrar agora",
    "Entrar",
]
_LEAVE_SELECTORS = [
    'button[aria-label*="Leave call" i]',
    'button[aria-label*="hang up" i]',
    'button[aria-label*="End call" i]',
    # pt-BR
    'button[aria-label*="Sair da chamada" i]',
    'button[aria-label*="Encerrar chamada" i]',
    'button[jsname="CQylAd"]',  # historically the leave button (language-agnostic)
]

# Absolute backstop on how long we sit in a call, measured from join. Past the
# scheduled session we stay only while people are present; this caps the
# "someone left the meeting open and idle for hours" case so the browser is
# never pinned indefinitely. Generous so it never trips a real session.
_SAFETY_EXTRA_SECONDS = 4 * 3600  # 4 hours beyond the scheduled length

# Chromium volatile state that's useless to clone and can be large.
_PROFILE_CLONE_IGNORE = shutil.ignore_patterns(
    "Cache*", "*Cache", "GPUCache", "ShaderCache", "GrShaderCache",
    "GraphiteDawnCache", "Code Cache", "Service Worker",
    "LOCK", "SingletonLock", "SingletonCookie", "SingletonSocket",
    "*.tmp", "*-journal",
    # Diagnostic dumps written into the profile dir — don't clone them.
    "meet-join-debug.png", "meet-join-debug.html",
)


def _clone_profile() -> str:
    """
    Copy the signed-in browser profile to a throwaway dir.

    Why: launch_persistent_context locks the profile directory, so two
    overlapping sessions sharing BROWSER_PROFILE_DIR would make the
    second launch fail with "profile is already in use". Each join gets
    its own clone (the profile is a few MB — cookies + Local State are
    what carry the Google sign-in). The clone is deleted afterwards.
    """
    dst = tempfile.mkdtemp(prefix="psike-meet-profile-")
    shutil.rmtree(dst)  # copytree wants a non-existent destination
    shutil.copytree(
        config.BROWSER_PROFILE_DIR, dst, ignore=_PROFILE_CLONE_IGNORE
    )
    return dst


async def join_meet(
    meeting_url: str,
    scheduled_seconds: int,
    on_joined: Optional[Callable[[], None]] = None,
    recheck: Optional[Callable[[], bool]] = None,
) -> bool:
    """
    Join a Meet URL, stay through the scheduled session (and longer while
    people are still in the call), leave cleanly.

    on_joined is called once we've confirmed the in-meeting UI —
    the scheduler uses it to record the 'joined' state truthfully.

    recheck (optional) is a synchronous callable, polled every ~30s while
    we're in the meeting, returning True when the session changed in the DB
    (status became cancelled/no-show/completed, or its meeting_url changed).
    When it returns True we leave ONLY if no one else is in the call; if a
    participant is present we stay. It runs in a worker thread (it does a DB
    query), so it never blocks the loop.

    Returns True if we believe we successfully joined (saw the
    in-meeting controls). False if anything went wrong — but the
    service keeps running and tries the next session.
    """
    # Which Chrome sub-profile (if any) is actually signed in as
    # SUPORTE_EMAIL? A Workspace sign-in often lands in a NEW managed
    # sub-profile (e.g. "Profile 1") rather than "Default" — if we always
    # load "Default" we open signed out even though the account is right
    # there in the profile dir. See bot/profile_utils.py.
    match_dir = await asyncio.to_thread(
        profile_utils.find_profile_for_user, config.BROWSER_PROFILE_DIR, config.SUPORTE_EMAIL
    )
    signed_in = bool(match_dir) and await asyncio.to_thread(
        profile_utils.has_google_auth_cookies, config.BROWSER_PROFILE_DIR, match_dir
    )
    if not signed_in:
        logger.error(
            f"Browser profile is not signed in as {config.SUPORTE_EMAIL} — run "
            "scripts.setup_profile on a LINUX machine and redeploy."
        )
        return False

    profile_dir = await asyncio.to_thread(_clone_profile)
    chrome_args = [
        # Auto-accept any leftover mic/cam UI prompts.
        "--use-fake-ui-for-media-stream",
        # Reduce automation-detection fingerprint.
        "--disable-blink-features=AutomationControlled",
        # Stable window size — Meet's responsive layout
        # sometimes hides controls at unusual viewport sizes.
        "--window-size=1280,800",
        # Use Chrome's portable "basic" cookie encryption instead of the OS
        # keyring. Chrome encrypts cookies with a machine-specific key; a
        # profile signed in on a desktop (with gnome-keyring) can't be
        # decrypted in the keyring-less container → it appears SIGNED OUT and
        # Meet redirects to accounts.google.com. Forcing 'basic' makes the
        # cookie store portable, so the same profile works locally and here.
        "--password-store=basic",
    ]
    if match_dir != "Default":
        # Load the sub-profile SUPORTE_EMAIL is actually signed into instead
        # of Chrome's default "Default" sub-profile. We clone the WHOLE
        # user-data-dir (below), so this sub-profile exists in the clone too.
        chrome_args.append(f"--profile-directory={match_dir}")
    if config.IN_DOCKER:
        chrome_args += ["--no-sandbox", "--disable-dev-shm-usage"]
    try:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                profile_dir,
                headless=config.HEADLESS,
                # Real Chrome, not bundled Chromium — Google refuses
                # sign-in (and degrades Meet) on the automation build.
                channel=config.BROWSER_CHANNEL or None,
                # Pre-grant mic/cam at the OS level so we don't have
                # to click through Chrome's permission popups.
                permissions=["microphone", "camera"],
                args=chrome_args,
                viewport={"width": 1280, "height": 800},
            )

            page = await ctx.new_page()
            # Open the meeting in whatever language the host account uses — the
            # button matchers below understand BOTH English and pt-BR labels, so
            # we don't force a UI language.
            logger.info(f"Opening {meeting_url}")
            await page.goto(meeting_url, wait_until="domcontentloaded", timeout=30_000)

            # Give the pre-join screen time to fully render. Meet does
            # a lot of async JS on this page; rushing the next clicks
            # causes them to miss their targets.
            await asyncio.sleep(3)

            # Signed-out profile = we would join as an anonymous
            # guest. Google then treats it as an external user and
            # auto-record/transcribe will NOT engage — joining would
            # be worse than useless because it looks like it worked.
            if await _is_signed_out(page):
                logger.error(
                    "Browser profile is NOT signed in to Google — the "
                    "pre-join page is showing 'Sign in'. Run "
                    "`python -m scripts.setup_profile` and sign in as "
                    f"{config.SUPORTE_EMAIL}, then restart the service."
                )
                await ctx.close()
                return False

            await _dismiss_popups(page)
            await _toggle_off(page, _MIC_OFF_PATTERNS, "microphone")
            await _toggle_off(page, _CAM_OFF_PATTERNS, "camera")

            if not await _click_join(page):
                logger.error("Could not find or click a Join button.")
                await _dump_join_debug(page)
                await ctx.close()
                return False

            # Confirm we're actually in by waiting for the leave button
            # to appear in the in-meeting UI. If it never appears, we
            # got stuck on a lobby/error screen.
            in_meeting = await _wait_for_in_meeting(page)
            if not in_meeting:
                logger.warning(
                    "Joined click succeeded but could not confirm in-meeting UI. "
                    "Continuing — might be in a lobby."
                )
            elif on_joined:
                on_joined()

            logger.info(
                f"In meeting — staying through the ~{scheduled_seconds // 60} "
                f"min session (and longer while people remain)"
            )

            # Sit in the meeting. We don't need to "do" anything —
            # Google's auto-record sees our Workspace user present and
            # engages. Check every 30s that we're still in the call so
            # we don't hold the browser open for an hour after everyone
            # hung up (or after Meet kicked us).
            try:
                await _stay_until_over(page, scheduled_seconds, recheck)
            except asyncio.CancelledError:
                logger.info("Cancelled — leaving now")

            await _leave(page)
            await asyncio.sleep(2)
            await ctx.close()
            return True

    except Exception as e:
        logger.exception(f"join_meet({meeting_url}) crashed: {e}")
        return False
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


async def _toggle_off(page: Page, patterns: Iterable, kind: str, max_wait_seconds: int = 8) -> None:
    """
    Click the mic-off or cam-off TOGGLE if it's currently ON.

    Meet's pre-join button labels swap between "Turn off X" (currently
    on) and "Turn on X" (currently off). We only click when we see
    a "Turn off"-style label, so we never accidentally turn them on.
    The page renders these toggles asynchronously, so we wait up to
    max_wait_seconds for one to appear instead of a single quick pass.
    """
    deadline = asyncio.get_event_loop().time() + max_wait_seconds
    while asyncio.get_event_loop().time() < deadline:
        for pat in patterns:
            try:
                btn = page.get_by_role("button", name=pat).first
                if await btn.is_visible():
                    await btn.click(timeout=2_000)
                    logger.info(f"Turned off {kind} via {pat.pattern!r}")
                    return
            except PlaywrightTimeout:
                continue
            except Exception:
                continue
        await asyncio.sleep(0.5)
    logger.info(f"No 'turn off {kind}' toggle appeared — assuming already off.")


async def _dump_join_debug(page: Page) -> None:
    """Diagnostics when the Join button can't be found on the server (headless).
    Logs the real URL + title and EVERY button's text/aria-label — which tells a
    cookie dialog ('Accept all') from a sign-in page ('Sign in') from a stale
    selector ('Request to join') from a blank/redirected page.

    Saves a screenshot + full HTML into BROWSER_PROFILE_DIR, which is BIND-
    MOUNTED to ./browser-profile on the host — so the files appear directly at
    ./browser-profile/meet-join-debug.{png,html} on the server, no `docker cp`
    needed and they survive container restarts.
    """
    shot = f"{config.BROWSER_PROFILE_DIR}/meet-join-debug.png"
    html = f"{config.BROWSER_PROFILE_DIR}/meet-join-debug.html"
    try:
        logger.error(f"[join-debug] url={page.url}  title={await page.title()!r}")
    except Exception as e:
        logger.error(f"[join-debug] url/title failed: {e}")
    try:
        btns = await page.query_selector_all("button")
        logger.error(f"[join-debug] {len(btns)} button(s) on the page:")
        for i, b in enumerate(btns[:60]):
            try:
                txt = (await b.inner_text() or "").strip().replace("\n", " ")
                aria = await b.get_attribute("aria-label")
                if txt or aria:
                    logger.error(f"[join-debug]   btn[{i}] text={txt!r} aria={aria!r}")
            except Exception:
                continue
    except Exception as e:
        logger.error(f"[join-debug] button scan failed: {e}")
    try:
        await page.screenshot(path=shot, full_page=True)
        logger.error(f"[join-debug] screenshot -> {shot} (host: ./browser-profile/meet-join-debug.png)")
    except Exception as e:
        logger.error(f"[join-debug] screenshot failed: {e}")
    try:
        with open(html, "w", encoding="utf-8") as f:
            f.write(await page.content())
        logger.error(f"[join-debug] page HTML -> {html} (host: ./browser-profile/meet-join-debug.html)")
    except Exception as e:
        logger.error(f"[join-debug] html dump failed: {e}")


async def _is_signed_out(page: Page) -> bool:
    """True when the profile isn't signed in. The clearest signal is a redirect
    to Google's sign-in flow (Meet bounces un-authenticated sessions there);
    we also match the in-page sign-in prompt in English and pt-BR."""
    try:
        if "accounts.google.com" in page.url:
            return True
    except Exception:
        pass
    signed_out_texts = [
        "Sign in with your Google account",   # English
        "Faça login com sua Conta do Google",  # pt-BR
        "Fazer login",                          # pt-BR (short)
    ]
    for txt in signed_out_texts:
        try:
            if await page.get_by_text(txt, exact=False).first.is_visible():
                return True
        except Exception:
            continue
    return False


async def _dismiss_popups(page: Page) -> None:
    """Close 'Got it' style onboarding dialogs that overlay the
    pre-join controls and swallow clicks."""
    for label in ("Got it", "Dismiss", "Close"):
        try:
            btn = page.get_by_role("button", name=label, exact=True).first
            if await btn.is_visible():
                await btn.click(timeout=2_000)
                logger.info(f"Dismissed popup via '{label}'")
                await asyncio.sleep(0.5)
        except Exception:
            continue


async def _click_join(page: Page, max_wait_seconds: int = 45) -> bool:
    """
    Keep trying the join labels for up to max_wait_seconds. The
    pre-join page renders its controls asynchronously and in stages —
    a single quick pass over the labels regularly fires before the
    button exists and then gives up.

    Each round starts with an Escape keypress: if an earlier click
    accidentally opened a dropdown/dialog, that overlay intercepts
    pointer events and a normal click on "Join now" times out forever.
    Escape closes it. If a click still gets intercepted, fall back to
    a direct DOM click which no overlay can block.
    """
    deadline = asyncio.get_event_loop().time() + max_wait_seconds
    last_problem = "no join-like button ever became visible"
    while asyncio.get_event_loop().time() < deadline:
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        for label in _JOIN_LABELS:
            try:
                btn = page.get_by_role("button", name=label, exact=False).first
                if not await btn.is_visible():
                    continue
                if await btn.is_disabled():
                    last_problem = f"'{label}' is visible but disabled"
                    continue
                try:
                    await btn.click(timeout=4_000)
                except Exception as e:
                    last_problem = f"click on '{label}' failed: {e}"
                    logger.info(f"{last_problem} — retrying via DOM click")
                    await btn.evaluate("el => el.click()")
                logger.info(f"Clicked '{label}'")
                return True
            except PlaywrightTimeout:
                continue
            except Exception as e:
                last_problem = f"'{label}': {e}"
                continue
        await asyncio.sleep(1)
    logger.error(f"Join not clicked after {max_wait_seconds}s. Last problem: {last_problem}")
    return False


async def _participant_count(page: Page) -> Optional[int]:
    """
    Best-effort participant count from Meet's in-meeting DOM.

    Two independent signals, take whichever is available:
      - the people-button badge (historically class .uGOf1d) holds
        the live participant number
      - each participant tile carries data-participant-id; counting
        the distinct ids gives a lower bound (>=1, ourselves)

    Returns None when neither signal is found (UI changed) — callers
    must treat None as "unknown", never as "alone".
    """
    try:
        result = await page.evaluate(
            """() => {
                const ids = new Set();
                document.querySelectorAll('[data-participant-id]')
                    .forEach(e => ids.add(e.getAttribute('data-participant-id')));
                let badge = null;
                const el = document.querySelector('.uGOf1d');
                if (el) {
                    const n = parseInt((el.textContent || '').replace(/\\D/g, ''), 10);
                    if (!Number.isNaN(n)) badge = n;
                }
                return {badge: badge, tiles: ids.size};
            }"""
        )
        if result.get("badge"):
            return int(result["badge"])
        if result.get("tiles"):
            return int(result["tiles"])
        return None
    except Exception:
        return None


async def _stay_until_over(
    page: Page, scheduled_seconds: int, recheck: Optional[Callable[[], bool]] = None
) -> None:
    """
    Stay through the scheduled session, then decide by who's in the call:

      • participants still present after the scheduled time → KEEP STAYING
        until they ALL leave (regardless of how long that takes), then
      • once everyone has left → leave (after the ALONE_LEAVE_SECONDS buffer);
      • if NO ONE ever joined → leave at the scheduled end ("empty → close").

    It also leaves early when:
      a) the in-meeting UI disappears (meeting ended / we were removed) —
         two consecutive misses so a re-render blip doesn't trip it;
      b) the session changed in the DB (recheck: cancelled/no-show/completed,
         or new meeting_url) AND the room is empty — if someone is present we
         stay (the changed meeting is joined separately).

    A hard backstop (scheduled + _SAFETY_EXTRA_SECONDS) bounds the absolute
    stay so a meeting left open with idle people can't pin the browser forever.
    """
    hard_cap = scheduled_seconds + _SAFETY_EXTRA_SECONDS
    elapsed = 0
    misses = 0
    others_seen = False
    alone_for = 0
    changed_logged = False
    while elapsed < hard_cap:
        step = 30
        await asyncio.sleep(step)
        elapsed += step

        still_in = False
        for sel in _LEAVE_SELECTORS:
            try:
                if await page.locator(sel).first.is_visible():
                    still_in = True
                    break
            except Exception:
                continue

        if still_in:
            misses = 0
        else:
            misses += 1
            if misses >= 2:
                logger.info(
                    f"In-meeting UI gone for {misses * 30}s after "
                    f"{elapsed}s — meeting appears to be over."
                )
                return

        # How many are in the call right now. None = detection failed (treat as
        # unknown — never as "empty", so we don't kick a possibly-active call).
        count = await _participant_count(page)

        # (b) Did the session's status or meeting_url change in the DB? If so,
        # leave only when the call is empty; stay while a participant is here.
        if recheck is not None:
            try:
                changed = await asyncio.to_thread(recheck)
            except Exception:
                # A DB blip must not evict us — only act on a definite True.
                changed = False
            if changed:
                if count is not None and count >= 2:
                    if not changed_logged:
                        logger.info(
                            "Session status/link changed but a participant is "
                            "present — staying; the changed meeting is joined "
                            "separately."
                        )
                        changed_logged = True
                else:
                    logger.info(
                        "Session status/link changed and the meeting is empty "
                        "— leaving."
                    )
                    return

        # Everyone-left: once others had joined and then the host is alone for
        # ALONE_LEAVE_SECONDS straight, they've all left → leave. This is what
        # makes the bot "stay until all users leave", however long the call ran.
        if config.ALONE_LEAVE_SECONDS > 0:
            if count is not None and count >= 2:
                if not others_seen:
                    logger.info(f"Other participants present ({count}).")
                others_seen = True
                alone_for = 0
            elif count == 1 and others_seen:
                alone_for += step
                if alone_for >= config.ALONE_LEAVE_SECONDS:
                    logger.info(
                        f"Everyone else left {alone_for}s ago — leaving."
                    )
                    return
            # count None = detection failed; keep current state.

        # Scheduled time is up and NO ONE ever joined → close now. If people are
        # (or were) present, we don't close here — the everyone-left rule above
        # ends it once they all leave, no matter how far past the scheduled end.
        if (
            elapsed >= scheduled_seconds
            and not others_seen
            and count is not None
            and count <= 1
        ):
            logger.info(
                f"Scheduled {scheduled_seconds // 60} min are up and no "
                f"participant joined — leaving."
            )
            return

    logger.info(
        f"Safety backstop reached ({hard_cap}s) — leaving even though the "
        f"call may still be active."
    )


async def _wait_for_in_meeting(page: Page, timeout_ms: int = 30_000) -> bool:
    for sel in _LEAVE_SELECTORS:
        try:
            await page.locator(sel).first.wait_for(
                state="visible", timeout=timeout_ms
            )
            return True
        except PlaywrightTimeout:
            continue
    return False


async def _leave(page: Page) -> None:
    for sel in _LEAVE_SELECTORS:
        try:
            await page.locator(sel).first.click(timeout=3_000)
            logger.info(f"Clicked leave via {sel}")
            return
        except PlaywrightTimeout:
            continue
        except Exception:
            continue
    logger.warning("Could not find a leave button — closing browser anyway.")
