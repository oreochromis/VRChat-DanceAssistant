"""Current VRCX account profile synchronization tests with mocked network."""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dance_ui import vrcx_profile


class VRCXProfileTests(unittest.TestCase):
    def test_profile_sync_saves_only_public_profile_and_avatar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "VRCX.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE configs (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO configs VALUES ('config:lastuserloggedin', 'usr_test')")
            connection.execute("CREATE TABLE cookies (key TEXT PRIMARY KEY, value TEXT)")
            cookies = base64.b64encode(json.dumps([
                {"Name": "auth", "Value": "not-a-real-session", "Domain": ".vrchat.cloud"},
            ]).encode()).decode()
            connection.execute("INSERT INTO cookies VALUES ('default', ?)", (cookies,))
            connection.commit()
            connection.close()
            profile = {"id": "usr_test", "displayName": "测试昵称",
                       "iconUrl": "https://api.vrchat.cloud/icon.png",
                       "currentAvatarThumbnailImageUrl": "https://api.vrchat.cloud/avatar.png"}
            calls = []

            def download(url, cookie_header, maximum):
                calls.append((url, cookie_header))
                return json.dumps(profile).encode() if url == vrcx_profile.PROFILE_URL else b"avatar bytes"

            with (patch.object(vrcx_profile.core, "data_dir", return_value=str(root)),
                  patch.object(vrcx_profile, "_download", side_effect=download)):
                result = vrcx_profile.sync_vrcx_profile(database)
                self.assertEqual(result["name"], "测试昵称")
                self.assertEqual(result["id"], "usr_test")
                self.assertTrue(vrcx_profile.profile_path().is_file())
                self.assertEqual(vrcx_profile.avatar_path().read_bytes(), b"avatar bytes")
                self.assertNotIn("not-a-real-session", vrcx_profile.profile_path().read_text(encoding="utf-8"))
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][0], profile["iconUrl"])
                loaded = vrcx_profile.load_vrcx_profile()
                self.assertEqual(loaded["name"], "测试昵称")
                self.assertTrue(loaded["avatar"])


if __name__ == "__main__":
    unittest.main()


class FriendAvatarTests(unittest.TestCase):
    def test_friend_avatar_prefers_profile_picture_and_uses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            payload = {"profilePicOverride": "https://api.vrchat.cloud/profile.png",
                       "currentAvatarThumbnailUrl": "https://api.vrchat.cloud/avatar.png"}

            def download(url, cookies, maximum):
                calls.append(url)
                return json.dumps(payload).encode() if "/users/" in url else b"friend avatar"

            with (patch.object(vrcx_profile.core, "data_dir", return_value=str(root)),
                  patch.object(vrcx_profile, "_cookie_header", return_value="session"),
                  patch.object(vrcx_profile, "_download", side_effect=download)):
                index = vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", [
                    {"user_id": "friend-1", "name": "Friend"},
                ])
                path = vrcx_profile.friend_avatar_path("friend-1")
                self.assertEqual(path.read_bytes(), b"friend avatar")
                self.assertEqual(index["friend-1"]["url"], "https://api.vrchat.cloud/profile.png")
                vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", [
                    {"user_id": "friend-1", "name": "Friend"},
                ])
                self.assertEqual(len(calls), 2)
                vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", [
                    {"user_id": "friend-1", "name": "Friend"},
                ], force=True)
                self.assertEqual(len(calls), 4)

    def test_legacy_avatar_index_refreshes_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "avatar-index.json").write_text(json.dumps({
                "friend-1": {"checked_at": 1e12},
            }), encoding="utf-8")
            payload = {"iconUrl": "https://api.vrchat.cloud/icon.png"}

            def download(url, cookies, maximum):
                return json.dumps(payload).encode() if "/users/" in url else b"friend icon"

            with (patch.object(vrcx_profile.core, "data_dir", return_value=str(root)),
                  patch.object(vrcx_profile, "_cookie_header", return_value="session"),
                  patch.object(vrcx_profile, "_download", side_effect=download) as request):
                index = vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", [
                    {"user_id": "friend-1", "name": "Friend"},
                ])
                self.assertEqual(request.call_count, 2)
                self.assertEqual(index["friend-1"]["version"], vrcx_profile.AVATAR_INDEX_VERSION)

    def test_friend_avatar_uses_vrcx_icon_url_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = {"iconUrl": "https://api.vrchat.cloud/icon.png"}

            def download(url, cookies, maximum):
                return json.dumps(payload).encode() if "/users/" in url else b"friend icon"

            with (patch.object(vrcx_profile.core, "data_dir", return_value=str(root)),
                  patch.object(vrcx_profile, "_cookie_header", return_value="session"),
                  patch.object(vrcx_profile, "_download", side_effect=download)):
                index = vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", [
                    {"user_id": "friend-1", "name": "Friend"},
                ])
                self.assertEqual(index["friend-1"]["url"], payload["iconUrl"])
                self.assertEqual(vrcx_profile.friend_avatar_path("friend-1").read_bytes(), b"friend icon")

    def test_friend_profiles_and_images_download_in_parallel(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = threading.Lock()
            active = 0
            maximum = 0

            def download(url, cookies, size):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.03)
                with lock:
                    active -= 1
                if "/users/" in url:
                    user_id = url.rsplit("/", 1)[-1]
                    return json.dumps({"iconUrl": "https://api.vrchat.cloud/" + user_id + ".png"}).encode()
                return b"avatar"

            friends = [{"user_id": f"friend-{index}"} for index in range(4)]
            with (patch.object(vrcx_profile.core, "data_dir", return_value=str(root)),
                  patch.object(vrcx_profile, "_cookie_header", return_value="session"),
                  patch.object(vrcx_profile, "_download", side_effect=download)):
                vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", friends)
            self.assertGreater(maximum, 1)

    def test_failed_friend_avatar_request_waits_before_retrying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (patch.object(vrcx_profile.core, "data_dir", return_value=str(root)),
                  patch.object(vrcx_profile, "_cookie_header", return_value="session"),
                  patch.object(vrcx_profile, "_download", side_effect=OSError("offline")) as download):
                friends = [{"user_id": "friend-1", "name": "Friend"}]
                index = vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", friends)
                self.assertGreater(index["friend-1"]["checked_at"], 0)
                vrcx_profile.sync_friend_avatars(root / "VRCX.sqlite3", friends)
            self.assertEqual(download.call_count, 1)
