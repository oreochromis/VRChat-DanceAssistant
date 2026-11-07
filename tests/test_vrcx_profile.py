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
                       "currentAvatarThumbnailUrl": "https://api.vrchat.cloud/avatar.png"}
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
                loaded = vrcx_profile.load_vrcx_profile()
                self.assertEqual(loaded["name"], "测试昵称")
                self.assertTrue(loaded["avatar"])


if __name__ == "__main__":
    unittest.main()
