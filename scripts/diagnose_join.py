"""
diagnose_join.py — one-shot diagnostic for "clicked Join but never got in".

Opens the SAME signed-in profile the service uses, navigates to a Meet URL,
waits for the pre-join page to settle, and reports the ground truth:

  • page.url + title  (a redirect to accounts.google.com == SIGNED OUT)
  • whether the signed-out prompt is on the page (EN + pt-BR)
  • every button's text / aria-label (tells a cookie banner from a
    "Sign in" page from a stale Join selector from a lobby)
  • a full-page screenshot + HTML saved INTO the profile dir, which is
    bind-mounted to ./browser-profile on the host — so the files appear at
    ./browser-profile/diag.png and ./browser-profile/diag.html with no docker cp.

Run inside the running container (profile already mounted):

  docker compose exec hypson-bot \
    python -m scripts.diagnose_join "https://meet.google.com/xxx-xxxx-xxx"

If no URL is given it uses accounts.google.com, which is enough to answer the
single most important question: IS THIS PROFILE SIGNED IN?
"""

import asyncio
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from bot import config, profile_utils


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://myaccount.google.com/"

    # Same sub-profile resolution as bot/joiner.py: SUPORTE_EMAIL may be
    # signed into a sub-profile other than "Default".
    match_dir = profile_utils.find_profile_for_user(config.BROWSER_PROFILE_DIR, config.SUPORTE_EMAIL)
    print(f"[diag] SUPORTE_EMAIL={config.SUPORTE_EMAIL!r}  matched sub-profile: {match_dir!r}")

    # Clone the profile so we never fight the running service for the lock.
    dst = tempfile.mkdtemp(prefix="diag-profile-")
    shutil.rmtree(dst)
    shutil.copytree(config.BROWSER_PROFILE_DIR, dst, ignore=shutil.ignore_patterns(
        "Cache*", "*Cache", "GPUCache", "ShaderCache", "GrShaderCache",
        "GraphiteDawnCache", "Code Cache", "Service Worker",
        "LOCK", "SingletonLock", "SingletonCookie", "SingletonSocket",
        "*.tmp", "*-journal",
    ))

    args = [
        "--disable-blink-features=AutomationControlled",
        # THE flag under investigation: without it a desktop-made profile's
        # cookies can't be decrypted here and the profile reads as signed out.
        "--password-store=basic",
        "--window-size=1280,800",
    ]
    if match_dir and match_dir != "Default":
        args.append(f"--profile-directory={match_dir}")
    if config.IN_DOCKER:
        args += ["--no-sandbox", "--disable-dev-shm-usage"]

    print(f"[diag] HEADLESS={config.HEADLESS}  channel={config.BROWSER_CHANNEL!r}  "
          f"IN_DOCKER={config.IN_DOCKER}")
    print(f"[diag] profile source: {config.BROWSER_PROFILE_DIR}")
    print(f"[diag] navigating to: {url}")

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            dst,
            headless=config.HEADLESS,
            channel=config.BROWSER_CHANNEL or None,
            args=args,
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await asyncio.sleep(6)  # let Meet's async JS settle

        try:
            print(f"[diag] FINAL url : {page.url}")
            print(f"[diag] title    : {await page.title()!r}")
        except Exception as e:
            print(f"[diag] url/title failed: {e}")

        signed_out = "accounts.google.com" in (page.url or "")
        print(f"[diag] redirected to accounts.google.com (== SIGNED OUT): {signed_out}")

        for txt in ("Sign in", "Fazer login", "Faça login",
                    "your browser", "not supported", "não é compatível"):
            try:
                if await page.get_by_text(txt, exact=False).first.is_visible():
                    print(f"[diag] page shows text: {txt!r}")
            except Exception:
                pass

        try:
            btns = await page.query_selector_all("button")
            print(f"[diag] {len(btns)} button(s) on the page:")
            for i, b in enumerate(btns[:60]):
                try:
                    t = (await b.inner_text() or "").strip().replace("\n", " ")
                    a = await b.get_attribute("aria-label")
                    if t or a:
                        print(f"[diag]   btn[{i}] text={t!r} aria={a!r}")
                except Exception:
                    continue
        except Exception as e:
            print(f"[diag] button scan failed: {e}")

        shot = f"{config.BROWSER_PROFILE_DIR}/diag.png"
        html = f"{config.BROWSER_PROFILE_DIR}/diag.html"
        try:
            await page.screenshot(path=shot, full_page=True)
            print(f"[diag] screenshot -> {shot}  (host: ./browser-profile/diag.png)")
        except Exception as e:
            print(f"[diag] screenshot failed: {e}")
        try:
            with open(html, "w", encoding="utf-8") as f:
                f.write(await page.content())
            print(f"[diag] html -> {html}  (host: ./browser-profile/diag.html)")
        except Exception as e:
            print(f"[diag] html dump failed: {e}")

        await ctx.close()

    shutil.rmtree(dst, ignore_errors=True)
    print("[diag] done.")


if __name__ == "__main__":
    asyncio.run(main())
