"""Synchronize the current VRCX account profile without persisting login sessions."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

import dance_cache as core

PROFILE_FILENAME = "profile.json"
AVATAR_FILENAME = "profile-avatar.png"
PROFILE_URL = "https://api.vrchat.cloud/api/1/auth/user"
MAX_PROFILE_BYTES = 1024 * 1024
MAX_AVATAR_BYTES = 10 * 1024 * 1024


def profile_path() -> Path:
    return Path(core.data_dir()) / PROFILE_FILENAME


def avatar_path() -> Path:
    return Path(core.data_dir()) / AVATAR_FILENAME


def load_vrcx_profile():
    try:
        profile = json.loads(profile_path().read_text(encoding="utf-8"))
        if not isinstance(profile, dict) or not profile.get("id") or not profile.get("name"):
            return None
        profile["avatar"] = str(avatar_path()) if avatar_path().is_file() else ""
        return profile
    except (OSError, ValueError, TypeError):
        return None


def _account_id(path: Path) -> str:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM configs WHERE key='config:lastuserloggedin'").fetchone()
    finally:
        connection.close()
    value = str(row[0]).strip() if row and row[0] else ""
    if not value.startswith("usr_"):
        raise ValueError("VRCX 中没有当前登录账号，请先在 VRCX 登录。")
    return value


def _cookie_header(path: Path) -> str:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value FROM cookies WHERE key='default'").fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        raise ValueError("VRCX 没有可用的登录会话，请先登录 VRCX。")
    payload = json.loads(base64.b64decode(row[0]))
    items = payload if isinstance(payload, list) else payload.get("cookies", []) if isinstance(payload, dict) else []
    pairs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("Name", item.get("name"))
        value = item.get("Value", item.get("value"))
        domain = str(item.get("Domain", item.get("domain", ""))).lstrip(".").lower()
        if name and value and (domain in ("vrchat.cloud", "api.vrchat.cloud") or domain.endswith(".vrchat.cloud")):
            pairs.append(f"{name}={value}")
    if not pairs:
        raise ValueError("VRCX 登录会话已失效，请在 VRCX 重新登录后再同步。")
    return "; ".join(pairs)


def _download(url: str, cookies: str, maximum: int) -> bytes:
    host = urllib.parse.urlparse(url).hostname
    if host not in ("api.vrchat.cloud", "assets.vrchat.com"):
        raise ValueError("头像地址不属于 VRChat。")
    request = urllib.request.Request(url, headers={
        "User-Agent": "VRChatDanceAssistant/4.1.1",
        "Accept": "application/json,image/*",
        "Cookie": cookies if host == "api.vrchat.cloud" else "",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("账号资料或头像文件过大。")
    return data


def _avatar_url(payload):
    return (payload.get("profilePicOverride") or payload.get("profilePicOverrideThumbnail")
            or payload.get("iconUrl") or payload.get("currentAvatarThumbnailUrl")
            or payload.get("currentAvatarThumbnailImageUrl") or payload.get("currentAvatarImageUrl") or "")


def sync_vrcx_profile(path):
    source = Path(path).expanduser().resolve()
    account_id = _account_id(source)
    cookies = _cookie_header(source)
    payload = json.loads(_download(PROFILE_URL, cookies, MAX_PROFILE_BYTES))
    if not isinstance(payload, dict) or payload.get("id") != account_id:
        raise ValueError("VRChat 返回的账号与 VRCX 当前账号不一致，请重新登录后再同步。")
    name = str(payload.get("displayName") or "").strip()
    if not name:
        raise ValueError("VRChat 账号资料中没有昵称。")
    avatar_url = _avatar_url(payload)
    avatar = _download(avatar_url, cookies, MAX_AVATAR_BYTES) if avatar_url else b""
    profile = {"id": account_id, "name": name, "synced_at": time.time()}
    profile_path().parent.mkdir(parents=True, exist_ok=True)
    profile_temporary = profile_path().with_suffix(".tmp")
    profile_temporary.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(profile_temporary, profile_path())
    if avatar:
        avatar_temporary = avatar_path().with_suffix(".tmp")
        avatar_temporary.write_bytes(avatar)
        os.replace(avatar_temporary, avatar_path())
    else:
        try:
            avatar_path().unlink()
        except FileNotFoundError:
            pass
    result = load_vrcx_profile()
    result["name"] = name
    return result


AVATAR_INDEX_FILENAME = "avatar-index.json"
AVATAR_REFRESH_SECONDS = 86400
AVATAR_INDEX_VERSION = 1


def avatar_index_path() -> Path:
    return Path(core.data_dir()) / AVATAR_INDEX_FILENAME


def load_avatar_index():
    try:
        value = json.loads(avatar_index_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def friend_avatar_path(user_id) -> Path:
    import hashlib
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:32]
    return Path(core.data_dir()) / "avatars" / (digest + ".png")


def sync_friend_avatars(path, friends, force=False):
    index = load_avatar_index()
    now = time.time()
    stale = []
    for friend in friends:
        user_id = str(friend.get("user_id") or "")
        if not user_id:
            continue
        entry = index.get(user_id) or {}
        if (force or entry.get("version") != AVATAR_INDEX_VERSION
                or now - float(entry.get("checked_at", 0)) >= AVATAR_REFRESH_SECONDS):
            stale.append((user_id, entry))
    if not stale:
        return index
    cookies = _cookie_header(Path(path).expanduser().resolve())

    def sync_avatar(item):
        user_id, entry = item
        try:
            url = "https://api.vrchat.cloud/api/1/users/" + urllib.parse.quote(user_id, safe="")
            payload = json.loads(_download(url, cookies, MAX_PROFILE_BYTES))
            avatar_url = _avatar_url(payload)
        except Exception:
            avatar_url = None
        target = friend_avatar_path(user_id)
        if avatar_url and (force or avatar_url != entry.get("url") or not target.is_file()):
            try:
                data = _download(avatar_url, cookies, MAX_AVATAR_BYTES)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(data)
                os.replace(temporary, target)
                entry["url"] = avatar_url
            except Exception:
                pass
        elif avatar_url == "":
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            entry.pop("url", None)
        entry["checked_at"] = now
        entry["version"] = AVATAR_INDEX_VERSION
        return user_id, entry

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        index.update(pool.map(sync_avatar, stale))
    avatar_index_path().parent.mkdir(parents=True, exist_ok=True)
    temporary = avatar_index_path().with_suffix(".tmp")
    temporary.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, avatar_index_path())
    return index
