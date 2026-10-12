"""
setup_profile.py — one-time login that seeds the persistent
browser profile the service uses.

Run this once before starting the service, and again whenever it
starts logging "session expired" / "NOT signed in" or you've been
forced to reconnect.

Usage:
  python -m scripts.setup_profile

What happens:
  1. A visible real-Chrome window opens at accounts.google.com
  2. You sign in manually as the central host account (SUPORTE_EMAIL
     in .env), complete any 2FA / device-verification challenges
  3. Once you're at the Google homepage (signed in successfully),
     close the browser window
  4. Playwright has saved cookies + local storage to
     BROWSER_PROFILE_DIR — the service can now reuse this session
"""

import asyncio
import sys

# Make 'from bot import config' work when running from the project root
sys.path.insert(0, ".")

from playwright.async_api import async_playwright

from bot import config


GUIDE_TEXT = f"""
─────────────────────────────────────────────────────────────────
PSike Meet host auto-join — one-time profile setup

A Chrome window is about to open. Inside that window:

  1. Sign in as:  {config.SUPORTE_EMAIL}
  2. Complete any 2FA / device-verification prompts
  3. Confirm you land on a normal Google page (Gmail, the Google
     homepage, anything that means "I am signed in")
  4. Optionally, visit https://meet.google.com and confirm the
     account icon in the top-right is the Suporte account
  5. CLOSE the Chrome window

When you close the window this script exits and the profile is
saved at:

  {config.BROWSER_PROFILE_DIR}

You can now run the service:

  python -m bot.main

─────────────────────────────────────────────────────────────────
"""


async def main() -> None:
    print(GUIDE_TEXT)
    try:
        input("Press Enter to open the browser…")
    except EOFError:
        pass  # non-interactive stdin (e.g. launched from a runner) — just go

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            config.BROWSER_PROFILE_DIR,
            # MUST be visible — a human signs in here. Headless would
            # open an invisible window you can never type into.
            headless=False,
            # Real Chrome, not Playwright's bundled Chromium — Google
            # rejects sign-in on the automation build with "This
            # browser or app may not be secure".
            channel=config.BROWSER_CHANNEL or None,
            # --password-store=basic: sign in using Chrome's portable cookie
            # encryption (NOT the desktop keyring), so this profile's cookies can
            # be decrypted by the keyring-less server/container. Without it, a
            # profile created here is "signed out" once copied to the server.
            args=[
                "--disable-blink-features=AutomationControlled",
                "--password-store=basic",
            ],
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        await page.goto("https://accounts.google.com/signin")

        print("\nBrowser open. Sign in, then close the window to finish.\n")

        # Wait until all pages in the context are closed (i.e. user
        # closed the window). We poll instead of using a single
        # wait_for_event because the user can open new tabs.
        while ctx.pages:
            await asyncio.sleep(1)

    print(f"\n✓ Profile saved at {config.BROWSER_PROFILE_DIR}")
    print("You can now start the service with:  python -m bot.main")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
