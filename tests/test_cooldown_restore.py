"""Cool Down cancellation/HTTP regressions: fake clients and temporary media only."""
import io
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import test_core as fixtures


class CooldownRestoreTests(unittest.TestCase):
    tearDown = fixtures.CoreTests.tearDown

    def setUp(self):
        fixtures.CoreTests.setUp(self)
        self.dc._running = True
        self.dc._stop_event.clear()
        self.dc._config['dudu_enabled'] = True
        self.clip = self.root / 'relax.mp4'
        self.replacement = b'RELAX' * (1024 * 256)
        self.clip.write_bytes(self.replacement)
        self.dc._config['cooldown_file'] = str(self.clip)
        self.original = b'ORIGINAL SONG DATA'

    def filename(self, kind, sid=2):
        if kind == 'wd':
            return self.dc.wd_cache_name(sid)
        if kind == 'dudu':
            return self.dc.dudu_cache_name(sid)
        return {1: 'video.mp4', 2: 'next.mp4', 3: 'other.mp4'}[sid]

    def item(self, kind, sid):
        return {'songNum': sid, **({kind: True} if kind != 'pypy' else {})}

    def prepare(self, mode, kind='pypy'):
        self.dc.cooldown_disarm(silent=True)
        self.clip.write_bytes(self.replacement)
        for sid in (1, 2, 3):
            (self.cache / self.filename(kind, sid)).write_bytes(self.original)
            if kind == 'pypy':
                self.dc.mark_mapping(sid, self.filename(kind, sid))
        self.dc._config['cooldown_trigger'] = mode
        self.dc._last_queue_items = [self.item(kind, 1 if mode == 'next' else 2)]
        self.assertTrue(self.dc.cooldown_arm())
        if mode == 'reload':
            self.dc._cooldown_queue_reloaded(self.dc._last_queue_items)
        return self.filename(kind)

    def handler(self, kind='pypy', sid=2, byte_range=None):
        handler = self.dc.DanceHandler.__new__(self.dc.DanceHandler)
        host = {'pypy': 'cdn.pypy.dance', 'wd': self.dc.UDON_PLAY_HOST,
                'dudu': self.dc.DUDU_PLAY_HOST}[kind]
        handler.path = ('/' + self.filename(kind, sid) if kind == 'pypy'
                        else f'/videos/{sid}-abcd.mp4?e=999&s=demo')
        handler.headers = {'Host': host}
        if byte_range:
            handler.headers['Range'] = byte_range
        handler.wfile = io.BytesIO()
        handler.close_connection = False
        handler.connection = SimpleNamespace(shutdown=mock.Mock(), close=mock.Mock())
        for name in ('send_response', 'send_header', 'end_headers', 'send_error'):
            setattr(handler, name, mock.Mock())
        return handler

    def headers(self, handler):
        return {str(call.args[0]).lower(): str(call.args[1])
                for call in handler.send_header.call_args_list}

    def assert_not_cached(self, handler):
        headers = self.headers(handler)
        for directive in ('no-store', 'no-cache', 'must-revalidate'):
            self.assertIn(directive, headers.get('cache-control', '').lower())
        self.assertIn('no-cache', headers.get('pragma', '').lower())
        self.assertIn('expires', headers)

    def test_head_is_nonconsuming_and_missing_clip_fallback_is_not_a_replacement(self):
        for mode in ('next', 'reload'):
            for kind in ('pypy', 'wd', 'dudu'):
                with self.subTest(mode=mode, kind=kind):
                    self.prepare(mode, kind)
                    head = self.handler(kind)
                    head.do_HEAD()
                    self.assertEqual(self.dc.get_cooldown_state()['phase'], 'armed')
                    self.assertEqual(head.wfile.getvalue(), b'')
                    self.assertEqual(self.headers(head)['content-length'], str(len(self.replacement)))
                    self.assert_not_cached(head)
                    get = self.handler(kind)
                    get.do_GET()
                    self.assertEqual(get.wfile.getvalue(), self.replacement)
                    self.assert_not_cached(get)
                    other = self.handler(kind, sid=3)
                    other.do_HEAD()
                    self.assertEqual(self.dc.get_cooldown_state()['phase'], 'serving')

                    self.clip.unlink()
                    fallback = self.handler(kind)
                    fallback.do_HEAD()
                    self.assertEqual(self.headers(fallback)['content-length'], str(len(self.original)))
                    self.assertNotIn('close', self.headers(fallback).get('connection', '').lower())
                    self.assertFalse(fallback.close_connection)
                    self.assertEqual(self.dc._cooldown_selection(self.filename(kind), consume=False), (None, None))
                    self.assertEqual(self.dc.get_cooldown_state()['phase'], 'serving')

                    self.prepare(mode, kind)
                    self.clip.unlink()
                    armed_fallback = self.handler(kind)
                    armed_fallback.do_HEAD()
                    self.assertEqual(self.headers(armed_fallback)['content-length'], str(len(self.original)))
                    self.assertFalse(armed_fallback.close_connection)
                    self.assertEqual(self.dc.get_cooldown_state()['phase'], 'armed')

    def test_cancel_mid_chunk_stops_old_bytes_in_all_rooms_and_both_modes(self):
        core = self.dc
        for mode in ('next', 'reload'):
            for kind in ('pypy', 'wd', 'dudu'):
                with self.subTest(mode=mode, kind=kind):
                    self.prepare(mode, kind)
                    class CancelAfterFirstWrite(io.BytesIO):
                        writes = 0
                        def write(self, data):
                            self.writes += 1
                            written = super().write(data)
                            if self.writes == 1:
                                core.cooldown_disarm()
                            return written
                    get = self.handler(kind)
                    get.wfile = CancelAfterFirstWrite()
                    get.do_GET()
                    self.assertEqual(get.wfile.writes, 1)
                    self.assertGreater(len(get.wfile.getvalue()), 0)
                    self.assertLess(len(get.wfile.getvalue()), len(self.replacement))
                    self.assertEqual(self.dc.get_cooldown_state()['phase'], 'idle')
                    self.assertTrue(get.close_connection)
                    get.connection.shutdown.assert_called()
                    self.assertEqual(self.dc._cooldown_streams, {})

    def test_cancel_then_refresh_head_and_range_get_return_original(self):
        for mode in ('next', 'reload'):
            for kind in ('pypy', 'wd', 'dudu'):
                with self.subTest(mode=mode, kind=kind):
                    self.prepare(mode, kind)
                    self.handler(kind).do_GET()
                    self.dc.cooldown_disarm()
                    head = self.handler(kind)
                    head.do_HEAD()
                    self.assertEqual(head.wfile.getvalue(), b'')
                    self.assertEqual(self.headers(head)['content-length'], str(len(self.original)))
                    self.assert_not_cached(head)
                    get = self.handler(kind, byte_range='bytes=3-10')
                    get.do_GET()
                    self.assertEqual(get.wfile.getvalue(), self.original[3:11])
                    get.send_response.assert_called_once_with(206)
                    self.assertEqual(self.headers(get)['content-range'], f'bytes 3-10/{len(self.original)}')
                    self.assert_not_cached(get)
                    self.assertFalse(get.close_connection)
                    get.connection.shutdown.assert_not_called()

    def test_delayed_snapshot_is_rejected_after_cancel_and_rearm(self):
        for mode in ('next', 'reload'):
            with self.subTest(mode=mode):
                name = self.prepare(mode)
                path, old = self.dc._cooldown_selection(name)
                self.assertEqual(path, str(self.clip))
                self.dc.cooldown_disarm()
                self.prepare(mode)
                path, new = self.dc._cooldown_selection(name)
                self.assertNotEqual(old, new)
                stale = self.handler()
                stale._serve_file(path, cooldown_generation=old)
                self.assertEqual(stale.wfile.getvalue(), b'')
                stale.send_response.assert_not_called()
                self.assertTrue(stale.close_connection)
                fresh = self.handler()
                fresh._serve_file(path, cooldown_generation=new)
                self.assertEqual(fresh.wfile.getvalue(), self.replacement)

    def test_cancel_restores_uncached_original_proxy_and_redirects_forbid_cache(self):
        for mode in ('next', 'reload'):
            for kind in ('pypy', 'wd', 'dudu'):
                with self.subTest(mode=mode, kind=kind):
                    name = self.prepare(mode, kind)
                    self.handler(kind).do_GET()
                    self.dc.cooldown_disarm()
                    (self.cache / name).unlink()
                    connection = fixtures.FakeConnection()
                    response = fixtures.FakeResponse([self.original], total=len(self.original))
                    get = self.handler(kind)
                    with mock.patch.object(self.dc, 'open_upstream', return_value=(connection, response)):
                        get.do_GET()
                    self.assertEqual(get.wfile.getvalue(), self.original)
                    self.assertEqual((self.cache / name).read_bytes(), self.original)
                    self.assert_not_cached(get)
        redirect = self.handler()
        redirect._redirect('http://cdn.pypy.dance/next.mp4')
        self.assert_not_cached(redirect)
        passthrough = self.handler()
        with mock.patch.object(self.dc, 'api_request', return_value=(302, {
                'location': 'http://cdn.pypy.dance/next.mp4',
                'cache-control': 'public, max-age=86400', 'etag': 'old-video'}, b'')):
            passthrough._api_passthrough(head_only=True, sid=2)
        self.assert_not_cached(passthrough)
        self.assertNotIn('etag', self.headers(passthrough))

    def test_original_stream_and_background_download_survive_cooldown_cancel(self):
        self.prepare('next')
        self.dc.cooldown_disarm()
        payload = self.original * (64 * 1024)
        (self.cache / 'next.mp4').write_bytes(payload)
        core = self.dc
        class CancelDuringOriginal(io.BytesIO):
            calls = 0
            def write(self, data):
                self.calls += 1
                if self.calls == 1:
                    core.cooldown_disarm()
                return super().write(data)
        get = self.handler()
        get.wfile = CancelDuringOriginal()
        get.do_GET()
        self.assertEqual(get.wfile.getvalue(), payload)
        self.assertFalse(get.close_connection)
        get.connection.shutdown.assert_not_called()
        response = fixtures.FakeResponse([lambda: core.cooldown_disarm() or b'HEAD', b'TAIL'], total=8)
        connection = fixtures.FakeConnection()
        with mock.patch.object(self.dc, 'open_upstream', return_value=(connection, response)):
            self.dc.download_file('background.mp4')
        self.assertEqual((self.cache / 'background.mp4').read_bytes(), b'HEADTAIL')

    def test_cancel_unblocks_a_replacement_socket_writer(self):
        self.prepare('next')
        blocked, released = threading.Event(), threading.Event()
        errors = []
        class BlockedWriter(io.BytesIO):
            def write(self, data):
                blocked.set()
                if not released.wait(2):
                    raise AssertionError('cancel did not unblock replacement writer')
                raise ConnectionResetError('cancelled')
        get = self.handler()
        get.wfile = BlockedWriter()
        get.connection.shutdown.side_effect = lambda *_: released.set()
        def serve():
            try:
                get.do_GET()
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            self.assertTrue(blocked.wait(1))
            self.dc.cooldown_disarm()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertTrue(get.close_connection)
            self.assertEqual(errors, [])
        finally:
            released.set()
            thread.join(2)

    def test_automatic_transition_invalidates_delayed_old_replacement(self):
        name = self.prepare('next')
        path, generation = self.dc._cooldown_selection(name)
        self.assertIsNone(self.dc.cooldown_check('other.mp4'))
        self.assertEqual(self.dc.get_cooldown_state()['phase'], 'idle')
        delayed = self.handler()
        delayed._serve_file(path, cooldown_generation=generation)
        self.assertEqual(delayed.wfile.getvalue(), b'')
        delayed.send_response.assert_not_called()

    def test_armed_preview_is_invalid_after_get_binds_a_different_next_song(self):
        name = self.prepare('next')
        path, preview = self.dc._cooldown_selection(name, consume=False)
        active_path, active = self.dc._cooldown_selection('other.mp4')
        self.assertNotEqual(preview, active)
        stale = self.handler()
        stale._serve_file(path, cooldown_generation=preview, head_only=True)
        stale.send_response.assert_not_called()
        self.assertTrue(stale.close_connection)
        current = self.handler(sid=3)
        current._serve_file(active_path, cooldown_generation=active)
        self.assertEqual(current.wfile.getvalue(), self.replacement)
