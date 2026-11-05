"""只读 VRCX 本地数据库，计算当前好友与用户在每个房间实例里的同房时长。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import dance_cache as core

SETTINGS_FILENAME = "vrcx-settings.json"


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


def resolve_vrcx_path(path=None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    saved = saved_vrcx_path()
    if saved and saved.is_file():
        return saved.resolve()
    default = default_vrcx_path()
    if default.is_file() or saved is None:
        return default.resolve()
    return saved.resolve()


def save_vrcx_path(path) -> None:
    target = Path(core.data_dir()) / SETTINGS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({"path": str(Path(path).resolve())}, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    os.replace(temporary, target)


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


def _own_intervals(rows, observed_end):
    intervals = {}
    events = []
    for created_at, location in rows:
        try:
            events.append((_timestamp(created_at), str(location or "")))
        except (ValueError, OverflowError, OSError):
            continue
    events.sort(key=lambda item: item[0])
    for index, (start, location) in enumerate(events):
        end = events[index + 1][0] if index + 1 < len(events) else observed_end
        if location and end > start:
            intervals.setdefault(location, []).append((start, end))
    return intervals


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
            "SELECT created_at, location FROM gamelog_location ORDER BY created_at, id").fetchall()
        join_rows = connection.execute(
            "SELECT user_id, display_name, type, created_at, location FROM gamelog_join_leave "
            "ORDER BY created_at, id").fetchall()
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
    observed_end = max(timestamps)
    own = _own_intervals(location_rows, observed_end)
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
    return {
        "path": str(source), "friends": friends_stats,
        "friend_count": len(friends), "matched_friends": len(friends_stats),
        "skipped_events": skipped, "observed_end": observed_end,
        "start_ts": start_ts, "end_ts": end_ts,
    }


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
