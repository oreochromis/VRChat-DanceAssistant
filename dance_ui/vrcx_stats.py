"""只读 VRCX 本地数据库，计算当前好友与用户在每个房间实例里的共同游玩时长。"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import dance_cache as core

SETTINGS_FILENAME = "vrcx-settings.json"
STATS_FILENAME = "vrcx-stats.json"
SOURCE_FILENAME = "vrcx-source.sqlite3"
DANCE_WORLD_IDS = frozenset({
    "wrld_f20326da-f1ac-45fc-a062-609723b097b1",
    "wrld_8ac0b9db-17ae-44af-9d20-7d8ab94a9129",
})
WORLD_KEYS = {
    "wrld_f20326da-f1ac-45fc-a062-609723b097b1": "pypy",
    "wrld_8ac0b9db-17ae-44af-9d20-7d8ab94a9129": "wd",
}


def default_vrcx_path() -> Path:
    appdata = os.environ.get("APPDATA")
    return (Path(appdata) if appdata else Path.home() / "AppData" / "Roaming") / "VRCX" / "VRCX.sqlite3"


def saved_vrcx_path() -> Path | None:
    try:
        raw = json.loads((Path(core.data_dir()) / SETTINGS_FILENAME).read_text(encoding="utf-8"))
        value = raw.get("path")
        return Path(value).expanduser() if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError, TypeError):
        return None


def archived_vrcx_path() -> Path:
    return Path(core.data_dir()) / SOURCE_FILENAME


def is_vrcx_archive(path) -> bool:
    try:
        return Path(path).expanduser().resolve() == archived_vrcx_path().resolve()
    except OSError:
        return False


def sanitize_vrcx_archive(path=None):
    target = Path(path).expanduser().resolve() if path else archived_vrcx_path()
    connection = sqlite3.connect(target)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cookies'").fetchone()
        if exists:
            connection.execute("DROP TABLE cookies")
            connection.commit()
            connection.execute("VACUUM")
    except sqlite3.DatabaseError:
        return target
    finally:
        connection.close()
    return target


def archive_vrcx_database(path) -> Path:
    source = Path(path).expanduser().resolve()
    target = archived_vrcx_path()
    if is_vrcx_archive(source):
        return sanitize_vrcx_archive(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    target_connection = None
    try:
        target_connection = sqlite3.connect(temporary)
        source_connection.backup(target_connection)
        target_connection.close()
        target_connection = None
        sanitize_vrcx_archive(temporary)
        os.replace(temporary, target)
    finally:
        source_connection.close()
        if target_connection is not None:
            target_connection.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def resolve_vrcx_path(path=None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    saved = saved_vrcx_path()
    if saved and saved.is_file():
        return saved.resolve()
    default = default_vrcx_path()
    if default.is_file():
        return default.resolve()
    edited = Path(core.data_dir()) / "vrcx-edited.sqlite3"
    if edited.is_file():
        return edited.resolve()
    archived = archived_vrcx_path()
    if archived.is_file():
        return archived.resolve()
    if saved is not None:
        return saved.resolve()
    return default.resolve()


def _update_settings(values) -> None:
    target = Path(core.data_dir()) / SETTINGS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raw = {}
    except (OSError, ValueError):
        raw = {}
    raw.update(values)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def save_vrcx_path(path) -> None:
    _update_settings({"path": str(Path(path).resolve())})


def saved_self_name() -> str:
    try:
        raw = json.loads((Path(core.data_dir()) / SETTINGS_FILENAME).read_text(encoding="utf-8"))
        value = raw.get("self_name")
        return value.strip() if isinstance(value, str) else ""
    except (OSError, ValueError, TypeError):
        return ""


def save_self_name(name) -> None:
    _update_settings({"self_name": str(name).strip()})


def save_vrcx_stats(result) -> None:
    payload = copy.deepcopy(result)
    payload["synced_at"] = time.time()
    target = Path(core.data_dir()) / STATS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def load_vrcx_stats():
    try:
        result = json.loads((Path(core.data_dir()) / STATS_FILENAME).read_text(encoding="utf-8"))
        return result if isinstance(result, dict) and isinstance(result.get("friends"), list) else None
    except (OSError, ValueError, TypeError):
        return None


def _timestamp(value) -> float:
    if not isinstance(value, str):
        raise ValueError("VRCX 时间字段无效。")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _connect(path) -> sqlite3.Connection:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("没有找到 VRCX 数据库，请确认 VRCX 已使用过，或手动选择 VRCX.sqlite3。")
    try:
        connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=3000")
        required = {"gamelog_location", "gamelog_join_leave"}
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not required.issubset(tables):
            connection.close()
            raise ValueError("所选文件缺少 VRCX 房间记录，不是受支持的 VRCX 数据库。")
        return connection
    except sqlite3.Error as error:
        raise ValueError("无法只读打开 VRCX 数据库：" + str(error)) from None


def _friend_table(connection):
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")
        if row[0].endswith("_friend_log_current")]
    if not tables:
        raise ValueError("VRCX 数据库中没有当前好友表。请先在 VRCX 中登录并同步好友。")
    if len(tables) == 1:
        return tables[0]
    candidates = []
    for table in tables:
        history = table.removesuffix("_current") + "_history"
        latest = connection.execute(
            f'SELECT MAX(created_at) FROM "{history}"').fetchone()[0] if history else None
        candidates.append((latest or "", table))
    return max(candidates)[1]


def _friends(connection, table):
    try:
        rows = connection.execute(f'SELECT user_id, display_name FROM "{table}"').fetchall()
    except sqlite3.Error as error:
        raise ValueError("VRCX 好友表结构不受支持：" + str(error)) from None
    return {str(user_id): str(display_name or user_id) for user_id, display_name in rows if user_id}


PLAY_INACTIVITY_SECONDS = 14 * 60


def _activity_timestamps(vrcx_timestamps=()):
    timestamps = {float(timestamp) for timestamp in vrcx_timestamps}
    for path in (core.history_path(), core.played_history_path()):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                timestamp = float(json.loads(line)["ts"])
                if timestamp >= 0:
                    timestamps.add(timestamp)
            except (ValueError, TypeError, KeyError):
                continue
    return sorted(timestamps)


def _own_intervals(rows, observed_end, activity_timestamps=(), own_leaves=()):
    intervals = {}
    events = []
    for created_at, location, world_id in rows:
        try:
            events.append((_timestamp(created_at), str(location or ""), str(world_id or "")))
        except (ValueError, OverflowError, OSError):
            continue
    events.sort(key=lambda item: item[0])
    for index, (start, location, world_id) in enumerate(events):
        end = events[index + 1][0] if index + 1 < len(events) else observed_end
        for leave, leave_location in own_leaves:
            if leave <= start:
                continue
            if not leave_location or leave_location == location:
                end = min(end, leave)
                break
        if not location or world_id not in DANCE_WORLD_IDS or end <= start:
            continue
        segment_start = start
        last_activity = start
        for timestamp in activity_timestamps:
            if timestamp <= start:
                continue
            if timestamp >= end:
                break
            if timestamp - last_activity > PLAY_INACTIVITY_SECONDS:
                if last_activity > segment_start:
                    intervals.setdefault(location, []).append((segment_start, last_activity))
                segment_start = timestamp
            last_activity = timestamp
        segment_end = end if end - last_activity <= PLAY_INACTIVITY_SECONDS else last_activity
        if segment_end > segment_start:
            intervals.setdefault(location, []).append((segment_start, segment_end))
    return intervals


def _daily_play_stats(own_intervals, start_ts, end_ts):
    days = {}
    for intervals in own_intervals.values():
        for start, end in intervals:
            cursor = max(start, start_ts)
            end = min(end, end_ts)
            while cursor < end:
                current = datetime.fromtimestamp(cursor)
                day_start = datetime(current.year, current.month, current.day).timestamp()
                segment_end = min(end, day_start + 86400)
                day = current.strftime("%Y-%m-%d")
                days[day] = days.get(day, 0) + max(0, segment_end - cursor)
                cursor = segment_end
    return [{"day": day, "seconds": round(days[day])} for day in sorted(days)]


def _new_friend_stats(rows, own_intervals, start_ts, end_ts):
    added = {}
    for created_at, kind, user_id, display_name in rows:
        if kind != "Friend" or not user_id:
            continue
        try:
            timestamp = _timestamp(created_at)
        except (ValueError, OverflowError, OSError):
            continue
        if not start_ts <= timestamp < end_ts or user_id in added:
            continue
        in_dance_room = any(start <= timestamp < end
                            for intervals in own_intervals.values() for start, end in intervals)
        if in_dance_room:
            added[user_id] = {"user_id": user_id, "name": str(display_name or user_id),
                              "ts": timestamp}
    return sorted(added.values(), key=lambda item: item["ts"])


def _player_intervals(rows, friends, observed_end):
    opened = {}
    intervals = {}
    skipped = 0
    for user_id, display_name, kind, created_at, location in rows:
        if user_id not in friends or not location:
            continue
        try:
            ts = _timestamp(created_at)
        except (ValueError, OverflowError, OSError):
            skipped += 1
            continue
        key = (user_id, location)
        if kind == "OnPlayerJoined":
            if key in opened:
                start, old_name = opened.pop(key)
                if ts > start:
                    intervals.setdefault(user_id, []).append((location, start, ts, old_name))
            opened[key] = (ts, friends[user_id] or str(display_name or user_id))
        elif kind == "OnPlayerLeft" and key in opened:
            start, name = opened.pop(key)
            if ts > start:
                intervals.setdefault(user_id, []).append((location, start, ts, name))
    for (user_id, location), (start, name) in opened.items():
        if observed_end > start:
            intervals.setdefault(user_id, []).append((location, start, observed_end, name))
    return intervals, skipped


def _overlap_intervals(player_intervals, own_intervals, start_ts, end_ts):
    result = []
    for location, start, end, name in player_intervals:
        start, end = max(start, start_ts), min(end, end_ts)
        if end <= start:
            continue
        for own_start, own_end in own_intervals.get(location, []):
            if own_end <= start:
                continue
            if own_start >= end:
                break
            overlap_start, overlap_end = max(start, own_start), min(end, own_end)
            if overlap_end > overlap_start:
                result.append((overlap_start, overlap_end, name))
    return result


def _merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def read_co_room_stats(path, start_ts, end_ts):
    start_ts, end_ts = float(start_ts), float(end_ts)
    if end_ts <= start_ts:
        raise ValueError("开始日期必须早于结束日期。")
    source = Path(path).expanduser().resolve()
    connection = _connect(source)
    try:
        friend_table = _friend_table(connection)
        friends = _friends(connection, friend_table)
        location_rows = connection.execute(
            "SELECT created_at, location, world_id FROM gamelog_location ORDER BY created_at, id").fetchall()
        join_rows = connection.execute(
            "SELECT user_id, display_name, type, created_at, location FROM gamelog_join_leave "
            "ORDER BY created_at, id").fetchall()
        history_table = friend_table.removesuffix("_current") + "_history"
        history_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (history_table,)).fetchone()
        friend_history = connection.execute(
            f'SELECT created_at, type, user_id, display_name FROM "{history_table}" '
            "ORDER BY created_at, id").fetchall() if history_exists else []
    finally:
        connection.close()
    timestamps = []
    for collection, index in ((location_rows, 0), (join_rows, 3)):
        for row in collection:
            try:
                timestamps.append(_timestamp(row[index]))
            except (ValueError, OverflowError, OSError):
                pass
    if not timestamps:
        raise ValueError("VRCX 数据库中没有可用的时间记录。")
    activity = _activity_timestamps(timestamps)
    observed_end = max(activity)
    self_id = friend_table.removesuffix("_friend_log_current")
    own_leaves = []
    for user_id, _, kind, created_at, location in join_rows:
        if user_id != self_id or kind != "OnPlayerLeft":
            continue
        try:
            own_leaves.append((_timestamp(created_at), str(location or "")))
        except (ValueError, OverflowError, OSError):
            pass
    own = _own_intervals(location_rows, observed_end, activity, sorted(own_leaves))
    players, skipped = _player_intervals(join_rows, friends, observed_end)
    friends_stats = []
    for user_id, intervals in players.items():
        overlaps = []
        name = friends[user_id]
        for location, start, end, event_name in intervals:
            name = name or event_name
            overlaps.extend(_overlap_intervals([(location, start, end, event_name)],
                                               own, start_ts, end_ts))
        merged = _merge_intervals([(start, end) for start, end, _ in overlaps])
        seconds = round(sum(end - start for start, end in merged))
        if seconds > 0:
            friends_stats.append({
                "user_id": user_id, "name": name, "seconds": seconds,
                "sessions": len(merged), "last_ts": max(end for _, end in merged),
            })
    friends_stats.sort(key=lambda item: (-item["seconds"], item["name"].casefold()))
    daily_play = _daily_play_stats(own, start_ts, end_ts)
    self_names = sorted({str(row[1]).strip() for row in join_rows
                         if row[0] == self_id and str(row[1] or "").strip()})
    world_play = {"pypy": 0, "wd": 0}
    world_by_location = {str(location): WORLD_KEYS.get(str(world_id))
                         for _, location, world_id in location_rows}
    for location, intervals in own.items():
        key = world_by_location.get(location)
        if not key:
            continue
        for start, end in intervals:
            overlap = max(0, min(end, end_ts) - max(start, start_ts))
            world_play[key] += round(overlap)
    result = {
        "path": str(source), "friends": friends_stats,
        "friend_count": len(friends), "matched_friends": len(friends_stats),
        "skipped_events": skipped, "observed_end": observed_end,
        "start_ts": start_ts, "end_ts": end_ts,
        "self_names": self_names, "daily_play": daily_play,
        "total_play_seconds": round(sum(item["seconds"] for item in daily_play)),
        "world_play": world_play,
        "new_friends": _new_friend_stats(friend_history, own, start_ts, end_ts),
    }
    archive_vrcx_database(source)
    return result


def format_duration(seconds) -> str:
    seconds = max(0, round(float(seconds)))
    hours, minutes = seconds // 3600, seconds % 3600 // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    if minutes:
        return f"{minutes} 分钟"
    return "不足 1 分钟" if seconds else "0 分钟"
