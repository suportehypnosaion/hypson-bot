"""
profile_utils.py — inspect a Chrome user-data-dir WITHOUT launching a
browser: which sub-profiles it contains, which one (if any) is signed in
as SUPORTE_EMAIL, and whether it was created on Windows/macOS (which
can't be decrypted on a keyring-less Linux server).

Used by bot/joiner.py (to pick --profile-directory and to gate joining on
a real signed-in profile) and scripts/verify_profile.py (pre-deploy check).

Chrome background:
  A single BROWSER_PROFILE_DIR ("user-data-dir") can hold several
  sub-profiles ("Default", "Profile 1", ...). Signing in to a Google
  Workspace account often lands you in a NEW managed sub-profile rather
  than "Default" — if the bot always loads "Default" it opens signed out
  even though the profile dir clearly has a signed-in account in it.
  Local State (JSON, at the root of the user-data-dir) records, per
  sub-profile, the signed-in "user_name" in profile.info_cache.
"""

import json
import os
import shutil
import sqlite3
import tempfile
from typing import Dict, Optional

# Cookies that only exist once a Google login has completed. Any one of
# these present for a *.google.com host is a reliable "this profile is
# actually signed in" signal (metadata in Local State can be stale).
_GOOGLE_AUTH_COOKIE_NAMES = (
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
)


def load_local_state(user_data_dir: str) -> Optional[dict]:
    path = os.path.join(user_data_dir, "Local State")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_profiles(user_data_dir: str) -> Dict[str, dict]:
    """profile sub-dir name (e.g. "Default", "Profile 1") -> its
    info_cache entry (has "name", "user_name", ...). Empty dict if
    Local State is missing/unreadable."""
    local_state = load_local_state(user_data_dir)
    if not local_state:
        return {}
    return local_state.get("profile", {}).get("info_cache", {}) or {}


def find_profile_for_user(user_data_dir: str, email: str) -> Optional[str]:
    """The sub-profile dir whose user_name matches email
    (case-insensitive), or None if no sub-profile matches."""
    target = (email or "").strip().lower()
    if not target:
        return None
    for profile_dir, info in list_profiles(user_data_dir).items():
        if (info.get("user_name") or "").strip().lower() == target:
            return profile_dir
    return None


def has_google_auth_cookies(user_data_dir: str, profile_dir: str) -> bool:
    """
    Best-effort check for a Google auth cookie in the sub-profile's
    cookie store (Network/Cookies in modern Chrome, Cookies in older
    versions). The sqlite file is copied first since a running Chrome
    can hold a lock on it.
    """
    for rel in (
        os.path.join(profile_dir, "Network", "Cookies"),
        os.path.join(profile_dir, "Cookies"),
    ):
        path = os.path.join(user_data_dir, rel)
        if not os.path.isfile(path):
            continue
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".sqlite")
            os.close(fd)
            shutil.copy2(path, tmp_path)
            conn = sqlite3.connect(tmp_path)
            try:
                placeholders = ",".join("?" * len(_GOOGLE_AUTH_COOKIE_NAMES))
                cur = conn.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%google.com' "
                    f"AND name IN ({placeholders})",
                    _GOOGLE_AUTH_COOKIE_NAMES,
                )
                if cur.fetchone()[0] > 0:
                    return True
            finally:
                conn.close()
        except Exception:
            continue
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    return False


def has_os_crypt_encrypted_key(user_data_dir: str) -> bool:
    """
    True when Local State carries os_crypt.encrypted_key — written when
    the profile's cookies were encrypted with Windows DPAPI or macOS
    Keychain. A profile with this key CANNOT be decrypted on a Linux
    server/container (no DPAPI/Keychain there), regardless of
    --password-store=basic, because the key itself is undecryptable.
    """
    local_state = load_local_state(user_data_dir)
    if not local_state:
        return False
    return bool(local_state.get("os_crypt", {}).get("encrypted_key"))
