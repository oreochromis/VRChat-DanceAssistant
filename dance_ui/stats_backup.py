"""Export and incrementally merge local play/VRCX statistics backups."""
from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import dance_cache as core
from . import stats_records, vrcx_stats

FORMAT = "vrchat-dance-stats-backup"
VERSION = 1
HISTORY_FILES = ("play_history.jsonl", "played_history.jsonl")
ROOM_EVENTS_FILENAME = "room_events.jsonl"
PROFILE_FILES = ("profile.json", "profile-avatar.png")
MAX_TEXT_MEMBER = 256 * 1024 * 1024
MAX_DATABASE_MEMBER = 2 * 1024 * 1024 * 1024


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _entry(line):
    try:
        value = json.loads(line)
        if not isinstance(value, dict):
            return None
        timestamp = float(value["ts"])
        song_id = value["id"]
        if isinstance(song_id, bool) or not isinstance(song_id, int):
            return None
        kind = value.get("kind") if value.get("kind") in ("pypy", "wd") else "pypy"
        player = str(value.get("player") or "").strip()
        return timestamp, kind, song_id, player, value
    except (ValueError, TypeError, KeyError):
        return None


def _history_entries(text):
    entries = {}
    for line in text.splitlines():
        parsed = _entry(line)
        if parsed is not None:
            timestamp, kind, song_id, player, value = parsed
            entries[(timestamp, kind, song_id, player)] = value
    return entries


def _merge_history(path: Path, incoming_text: str):
    existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    existing = _history_entries(existing_text)
    incoming = _history_entries(incoming_text)
    added = len(incoming.keys() - existing.keys())
    if added:
        existing.update(incoming)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for timestamp, kind, song_id, player in sorted(existing):
                handle.write(json.dumps(existing[(timestamp, kind, song_id, player)], ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    return added


def _room_event_key(line):
    try:
        value = json.loads(line)
        timestamp = float(value["ts"])
        kind = value["kind"]
        if kind not in ("location", "leave"):
            return None
        return timestamp, kind, str(value.get("location") or ""), str(value.get("user_id") or ""), value
    except (ValueError, TypeError, KeyError):
        return None


def _room_event_entries(text):
    entries = {}
    for line in text.splitlines():
        parsed = _room_event_key(line)
        if parsed is not None:
            timestamp, kind, location, user_id, value = parsed
            entries[(timestamp, kind, location, user_id)] = value
    return entries


def _merge_room_events(path, incoming_text):
    existing = _room_event_entries(path.read_text(encoding="utf-8") if path.is_file() else "")
    incoming = _room_event_entries(incoming_text)
    added = len(incoming.keys() - existing.keys())
    if added:
        existing.update(incoming)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for key in sorted(existing):
                handle.write(json.dumps(existing[key], ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    return added


def _zip_text(archive, name, maximum=MAX_TEXT_MEMBER):
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > maximum:
        raise ValueError("备份中的文件过大，无法导入：" + name)
    return archive.read(name).decode("utf-8")


def export_stats_backup(path):
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    root = Path(core.data_dir())
    files = [name for name in (*HISTORY_FILES, *PROFILE_FILES, "avatar-index.json", ROOM_EVENTS_FILENAME,
                                vrcx_stats.SETTINGS_FILENAME, vrcx_stats.STATS_FILENAME, vrcx_stats.SOURCE_FILENAME,
                                stats_records.EDITABLE_SOURCE_FILENAME)
             if (root / name).is_file()]
    for name in (vrcx_stats.SOURCE_FILENAME, stats_records.EDITABLE_SOURCE_FILENAME):
        if name in files:
            vrcx_stats.sanitize_vrcx_archive(root / name)
    avatars = sorted((root / "avatars").rglob("*.png")) if (root / "avatars").is_dir() else []
    files.extend(path.relative_to(root).as_posix() for path in avatars)
    manifest = {"format": FORMAT, "version": VERSION, "created_at": time.time(), "files": files}
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for name in files:
                archive.write(root / name, name)
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return manifest


def import_stats_backup(path):
    source = Path(path).expanduser().resolve()
    with zipfile.ZipFile(source) as archive:
        manifest_text = _zip_text(archive, "manifest.json", 1024 * 1024)
        if not manifest_text:
            raise ValueError("备份缺少 manifest.json。")
        manifest = json.loads(manifest_text)
        if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
            raise ValueError("这不是受支持的跳舞房统计备份。")
        result = {
            "play_history_added": 0, "played_history_added": 0,
            "settings_added": 0, "stats_updated": False, "source_imported": False,
            "profile_imported": False, "avatars_added": 0, "room_events_added": 0,
        }
        root = Path(core.data_dir())
        root.mkdir(parents=True, exist_ok=True)
        for name in HISTORY_FILES:
            incoming = _zip_text(archive, name)
            if incoming is not None:
                result[name.removesuffix(".jsonl") + "_added"] = _merge_history(root / name, incoming)
        incoming_room_events = _zip_text(archive, ROOM_EVENTS_FILENAME)
        if incoming_room_events is not None:
            result["room_events_added"] = _merge_room_events(root / ROOM_EVENTS_FILENAME, incoming_room_events)
        incoming_settings = _zip_text(archive, vrcx_stats.SETTINGS_FILENAME)
        if incoming_settings is not None:
            incoming = json.loads(incoming_settings)
            if not isinstance(incoming, dict):
                raise ValueError("备份中的 VRCX 设置格式无效。")
            settings_path = root / vrcx_stats.SETTINGS_FILENAME
            current = _read_json(settings_path)
            additions = {key: value for key, value in incoming.items() if key not in current}
            if additions:
                current.update(additions)
                _write_json(settings_path, current)
                result["settings_added"] = len(additions)
        incoming_stats = _zip_text(archive, vrcx_stats.STATS_FILENAME)
        if incoming_stats is not None:
            incoming = json.loads(incoming_stats)
            if not isinstance(incoming, dict) or not isinstance(incoming.get("friends"), list):
                raise ValueError("备份中的 VRCX 统计格式无效。")
            current = _read_json(root / vrcx_stats.STATS_FILENAME)
            if not current or float(incoming.get("synced_at", 0)) > float(current.get("synced_at", 0)):
                _write_json(root / vrcx_stats.STATS_FILENAME, incoming)
                result["stats_updated"] = True
        for name in (vrcx_stats.SOURCE_FILENAME, stats_records.EDITABLE_SOURCE_FILENAME):
            try:
                database_info = archive.getinfo(name)
            except KeyError:
                continue
            if database_info.file_size > MAX_DATABASE_MEMBER:
                raise ValueError("备份中的 VRCX 数据库过大，无法导入。")
            target = root / name
            if not target.exists():
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(archive.read(name))
                os.replace(temporary, target)
                if name == vrcx_stats.SOURCE_FILENAME:
                    result["source_imported"] = True
        profile_text = _zip_text(archive, "profile.json")
        if profile_text is not None:
            incoming_profile = json.loads(profile_text)
            if not isinstance(incoming_profile, dict) or not incoming_profile.get("id") or not incoming_profile.get("name"):
                raise ValueError("备份中的账号资料格式无效。")
            current_profile = _read_json(root / "profile.json")
            if not current_profile:
                _write_json(root / "profile.json", incoming_profile)
                current_profile = incoming_profile
                result["profile_imported"] = True
            try:
                avatar_info = archive.getinfo("profile-avatar.png")
            except KeyError:
                avatar_info = None
            if avatar_info is not None and avatar_info.file_size > 10 * 1024 * 1024:
                raise ValueError("备份中的头像文件过大。")
            avatar_target = root / "profile-avatar.png"
            if (avatar_info is not None and not avatar_target.exists()
                    and current_profile.get("id") == incoming_profile.get("id")):
                temporary = avatar_target.with_suffix(".tmp")
                temporary.write_bytes(archive.read("profile-avatar.png"))
                os.replace(temporary, avatar_target)
        incoming_index = _zip_text(archive, "avatar-index.json")
        if incoming_index is not None:
            incoming = json.loads(incoming_index)
            if not isinstance(incoming, dict):
                raise ValueError("备份中的头像索引格式无效。")
            index_path = root / "avatar-index.json"
            current = _read_json(index_path)
            merged = {**incoming, **current}
            if merged != current:
                _write_json(index_path, merged)
        for info in archive.infolist():
            name = info.filename
            parts = Path(name).parts
            if (not name.startswith("avatars/") or not name.endswith(".png")
                    or ".." in parts or Path(name).is_absolute()):
                continue
            if info.file_size > 10 * 1024 * 1024:
                raise ValueError("备份中的好友头像过大。")
            target = root / name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(archive.read(info))
            os.replace(temporary, target)
            result["avatars_added"] += 1
    return result
