"""Local CDN bookmarks; independent from unapplied service settings."""
import ipaddress
import json
import os
import tempfile
from pathlib import Path


class CDNFavorites:
    def __init__(self, root, *, legacy_roots=()):
        self.path = Path(root) / "cdn-favorites.json"
        self.items = []
        self.warning = ""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, list):
                    raise ValueError("收藏列表格式无效")
                for ip in data:
                    self.validate(ip)
                    if ip not in self.items:
                        self.items.append(ip)
            except (ValueError, OSError, TypeError) as exc:
                self.items = []
                self.warning = f"CDN 收藏读取失败，原文件已保留：{exc}"
        if legacy_roots and not self.warning:
            self._migrate(legacy_roots)

    def _migrate(self, roots):
        """Merge old appearance files once; retain them as recovery copies."""
        marker = self.path.with_name("cdn-favorites-migration.json")
        try:
            completed = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else []
            if not isinstance(completed, list) or any(not isinstance(p, str) for p in completed):
                raise ValueError("收藏迁移记录格式无效")
            for root in roots:
                source = Path(root) / self.path.name
                key = str(source.resolve())
                if source.resolve() == self.path.resolve() or key in completed or not source.is_file():
                    continue
                legacy = CDNFavorites(root)
                if legacy.warning:
                    self.warning = legacy.warning
                    continue
                self.save([*self.items, *legacy.items])
                completed.append(key)
                self._write_json(marker, completed)
        except (ValueError, OSError, TypeError) as exc:
            self.warning = f"CDN 收藏迁移未完成，旧文件已保留，下次启动重试：{exc}"

    @staticmethod
    def validate(ip):
        if not isinstance(ip, str) or not ipaddress.IPv4Address(ip).is_global:
            raise ValueError("收藏必须是公网 IPv4 地址")

    def save(self, items):
        for ip in items:
            self.validate(ip)
        items = list(dict.fromkeys(items))
        self._write_json(self.path, items)
        self.items = items
        self.warning = ""

    @staticmethod
    def _write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".cdn-favorites-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
