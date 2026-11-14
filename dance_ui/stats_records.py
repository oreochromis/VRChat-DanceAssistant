"""Edit software-owned VRCX copies and local play history without changing VRCX."""
from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

import dance_cache as core
from . import vrcx_stats

EDITABLE_SOURCE_FILENAME = "vrcx-edited.sqlite3"
VRCX_TABLES = {
    "location": ("gamelog_location", ("created_at", "location", "world_id")),
    "event": ("gamelog_join_leave", ("created_at", "type", "user_id", "display_name", "location", "time")),
}
HISTORY_FILES = {"queue": core.HISTORY_FILENAME, "played": core.PLAYED_HISTORY_FILENAME}


def editable_vrcx_path() -> Path:
    return Path(core.data_dir()) / EDITABLE_SOURCE_FILENAME


def ensure_editable_vrcx(path=None) -> Path:
    target = editable_vrcx_path()
    if target.is_file():
        return target
    source = Path(path).expanduser().resolve() if path else vrcx_stats.resolve_vrcx_path()
    if source == target.resolve() or not source.is_file():
        raise ValueError("没有可用于创建编辑副本的 VRCX 数据库。")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    target_connection = None
    try:
        target_connection = sqlite3.connect(temporary)
        source_connection.backup(target_connection)
        target_connection.execute(
            "CREATE TABLE IF NOT EXISTS _dance_deleted (table_name TEXT, row_id INTEGER, "
            "PRIMARY KEY(table_name, row_id))")
        target_connection.commit()
        target_connection.close()
        target_connection = None
        vrcx_stats.sanitize_vrcx_archive(temporary)
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


def _columns(connection, table):
    return [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]


def _copy_new_rows(source, target, table, replace=False):
    source_columns = _columns(source, table)
    target_columns = _columns(target, table)
    columns = [name for name in source_columns if name in target_columns]
    if not columns:
        return
    quoted = ",".join(f'"{name}"' for name in columns)
    placeholders = ",".join("?" for _ in columns)
    action = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    rows = source.execute(f'SELECT {quoted} FROM "{table}"')
    target.executemany(f'{action} INTO "{table}" ({quoted}) VALUES ({placeholders})', rows)


def _sync_editable_vrcx(source, target):
    if source == target.resolve() or not source.is_file():
        return
    source_connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.execute("PRAGMA query_only=ON")
        target_connection.execute(
            "CREATE TABLE IF NOT EXISTS _dance_deleted (table_name TEXT, row_id INTEGER, "
            "PRIMARY KEY(table_name, row_id))")
        for table, _ in VRCX_TABLES.values():
            _copy_new_rows(source_connection, target_connection, table)
            target_connection.execute(
                f'DELETE FROM "{table}" WHERE id IN (SELECT row_id FROM _dance_deleted WHERE table_name=?)',
                (table,))
        friend_tables = [row[0] for row in source_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
            if row[0].endswith(("_friend_log_current", "_friend_log_history"))]
        for table in friend_tables:
            if not _columns(target_connection, table):
                schema = source_connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if schema and schema[0]:
                    target_connection.execute(schema[0])
            _copy_new_rows(source_connection, target_connection, table,
                           replace=table.endswith("_friend_log_current"))
        target_connection.commit()
    finally:
        source_connection.close()
        target_connection.close()


def prepare_statistics_source(path) -> Path:
    source = Path(path).expanduser().resolve()
    target = editable_vrcx_path()
    if not target.is_file():
        return source
    _sync_editable_vrcx(source, target)
    return target


def list_vrcx_records(kind, search="", limit=500):
    if kind not in VRCX_TABLES:
        raise ValueError("未知记录类型。")
    table, fields = VRCX_TABLES[kind]
    path = ensure_editable_vrcx()
    connection = sqlite3.connect(path)
    try:
        selected = ("id", *fields)
        quoted = ",".join(f'"{name}"' for name in selected)
        parameters = []
        where = ""
        if search.strip():
            where = "WHERE " + " OR ".join(f'CAST("{name}" AS TEXT) LIKE ?' for name in selected)
            parameters = ["%" + search.strip() + "%"] * len(selected)
        rows = connection.execute(
            f'SELECT {quoted} FROM "{table}" {where} ORDER BY id DESC LIMIT ?',
            (*parameters, max(1, min(int(limit), 5000)))).fetchall()
        return [dict(zip(selected, row)) for row in rows]
    finally:
        connection.close()


def save_vrcx_record(kind, values, row_id=None):
    if kind not in VRCX_TABLES:
        raise ValueError("未知记录类型。")
    table, fields = VRCX_TABLES[kind]
    cleaned = {}
    for name in fields:
        value = values.get(name)
        if name == "time":
            try:
                value = int(value or 0)
            except (TypeError, ValueError):
                raise ValueError("事件时间必须是整数。") from None
        else:
            value = str(value or "").strip()
        cleaned[name] = value
    vrcx_stats._timestamp(cleaned["created_at"])
    if not cleaned["location"]:
        raise ValueError("位置/实例 ID 不能为空。")
    if kind == "location" and not cleaned["world_id"]:
        raise ValueError("地图 ID 不能为空。")
    if kind == "event" and not cleaned["type"]:
        raise ValueError("事件类型不能为空。")
    path = ensure_editable_vrcx()
    connection = sqlite3.connect(path)
    try:
        if row_id is None:
            row_id = connection.execute(f'SELECT MIN(COALESCE(MIN(id), 0), 0) - 1 FROM "{table}"').fetchone()[0]
        assignments = ",".join(f'"{name}"=?' for name in fields)
        if connection.execute(f'SELECT 1 FROM "{table}" WHERE id=?', (row_id,)).fetchone():
            connection.execute(f'UPDATE "{table}" SET {assignments} WHERE id=?',
                               (*[cleaned[name] for name in fields], row_id))
        else:
            connection.execute(
                f'INSERT INTO "{table}" (id,{",".join(chr(34) + name + chr(34) for name in fields)}) '
                f'VALUES ({",".join("?" for _ in range(len(fields) + 1))})',
                (row_id, *[cleaned[name] for name in fields]))
        connection.commit()
        return row_id
    finally:
        connection.close()


def delete_vrcx_record(kind, row_id):
    if kind not in VRCX_TABLES:
        raise ValueError("未知记录类型。")
    table, _ = VRCX_TABLES[kind]
    path = ensure_editable_vrcx()
    connection = sqlite3.connect(path)
    try:
        row_id = int(row_id)
        connection.execute(f'DELETE FROM "{table}" WHERE id=?', (row_id,))
        if row_id > 0:
            connection.execute(
                "INSERT OR REPLACE INTO _dance_deleted (table_name,row_id) VALUES (?,?)",
                (table, row_id))
        connection.commit()
    finally:
        connection.close()


def _history_path(kind):
    if kind not in HISTORY_FILES:
        raise ValueError("未知历史类型。")
    return Path(core.data_dir()) / HISTORY_FILES[kind]


def _history_values(entry, line):
    try:
        timestamp = float(entry["ts"])
        song_id = entry["id"]
        if isinstance(song_id, bool) or not isinstance(song_id, int):
            return None
        return {"ts": timestamp, "kind": entry.get("kind") if entry.get("kind") in ("pypy", "wd", "dudu") else "pypy",
                "id": song_id, "name": str(entry.get("name") or ""),
                "player": str(entry.get("player") or ""), "_line": line}
    except (ValueError, TypeError, KeyError):
        return None


def list_history_records(kind, search="", limit=500):
    path = _history_path(kind)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    records = []
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            values = _history_values(entry, index)
            if values is not None:
                records.append(values)
    if search.strip():
        query = search.strip().casefold()
        records = [record for record in records if query in json.dumps(record, ensure_ascii=False).casefold()]
    return records[-max(1, min(int(limit), 5000)):]


def _clean_history(values):
    try:
        timestamp = float(values.get("ts"))
        song_id = values.get("id")
        if isinstance(song_id, bool):
            raise ValueError
        song_id = int(song_id)
    except (TypeError, ValueError):
        raise ValueError("播放时间或歌曲编号无效。") from None
    if not math.isfinite(timestamp) or timestamp < 0 or song_id <= 0:
        raise ValueError("播放时间或歌曲编号无效。")
    source = values.get("kind")
    if source not in ("pypy", "wd", "dudu"):
        raise ValueError("歌曲来源必须是 pypy、wd 或 dudu。")
    return {"ts": timestamp, "kind": source, "id": song_id,
            "name": str(values.get("name") or "").strip(),
            "player": str(values.get("player") or "").strip()}


def _write_history(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(temporary, path)


def save_history_record(kind, values, line_index=None):
    path = _history_path(kind)
    cleaned = _clean_history(values)
    with core._history_lock:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        if line_index is None:
            lines.append(json.dumps(cleaned, ensure_ascii=False))
            line_index = len(lines) - 1
        else:
            line_index = int(line_index)
            if not 0 <= line_index < len(lines):
                raise ValueError("这条历史记录已发生变化，请刷新后再编辑。")
            try:
                current = json.loads(lines[line_index])
            except (ValueError, TypeError):
                current = None
            if not isinstance(current, dict):
                raise ValueError("原始行不是有效记录，无法编辑。")
            current.update(cleaned)
            lines[line_index] = json.dumps(current, ensure_ascii=False)
        _write_history(path, lines)
    return line_index


def delete_history_record(kind, line_index):
    path = _history_path(kind)
    with core._history_lock:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        line_index = int(line_index)
        if not 0 <= line_index < len(lines):
            raise ValueError("这条历史记录已发生变化，请刷新后再删除。")
        del lines[line_index]
        _write_history(path, lines)


def create_record_snapshot():
    source = ensure_editable_vrcx()
    target = Path(core.data_dir()) / "record-backups" / (str(int(time.time())) + "-" + uuid.uuid4().hex[:8])
    target.mkdir(parents=True, exist_ok=False)
    source_connection = sqlite3.connect(source)
    destination_connection = sqlite3.connect(target / EDITABLE_SOURCE_FILENAME)
    try:
        source_connection.backup(destination_connection)
    finally:
        source_connection.close()
        destination_connection.close()
    with core._history_lock:
        for name in HISTORY_FILES.values():
            path = Path(core.data_dir()) / name
            if path.is_file():
                shutil.copy2(path, target / name)
    with core._room_event_lock:
        path = Path(core.data_dir()) / core.ROOM_EVENT_FILENAME
        if path.is_file():
            shutil.copy2(path, target / path.name)
    return target


def restore_record_snapshot(path):
    snapshot = Path(path)
    if not snapshot.is_dir() or not (snapshot / EDITABLE_SOURCE_FILENAME).is_file():
        raise ValueError("修改前快照不存在或已损坏。")
    target = editable_vrcx_path()
    temporary = target.with_suffix(".tmp")
    source_connection = sqlite3.connect(snapshot / EDITABLE_SOURCE_FILENAME)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
    finally:
        source_connection.close()
        destination_connection.close()
    os.replace(temporary, target)
    with core._history_lock:
        for name in HISTORY_FILES.values():
            source = snapshot / name
            target_history = Path(core.data_dir()) / name
            if source.is_file():
                shutil.copy2(source, target_history)
            else:
                target_history.unlink(missing_ok=True)
    with core._room_event_lock:
        source = snapshot / core.ROOM_EVENT_FILENAME
        target_events = Path(core.data_dir()) / source.name
        if source.is_file():
            shutil.copy2(source, target_events)
        else:
            target_events.unlink(missing_ok=True)
    shutil.rmtree(snapshot)


def reset_editable_vrcx(path=None):
    target = editable_vrcx_path()
    if target.exists():
        target.unlink()
    return ensure_editable_vrcx(path)
