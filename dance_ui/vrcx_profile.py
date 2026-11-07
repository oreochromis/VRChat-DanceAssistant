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
        "User-Agent": "VRChatDanceAssistant/3.9.9.37",
        "Accept": "application/json,image/*",
        "Cookie": cookies if host == "api.vrchat.cloud" else "",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        data = response.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("账号资料或头像文件过大。")
    return data


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
    avatar_url = (payload.get("profilePicOverride") or payload.get("currentAvatarThumbnailUrl")
                  or payload.get("currentAvatarImageUrl") or "")
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
        current = load_vrcx_profile()
        if not current or current.get("id") != account_id:
            try:
                avatar_path().unlink()
            except FileNotFoundError:
                pass
    result = load_vrcx_profile()
    result["name"] = name
    return result
