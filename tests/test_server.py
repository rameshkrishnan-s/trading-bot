from contextlib import nullcontext
import io
import unittest
from unittest.mock import Mock, patch

from app.server import Handler
from app import server
from app.config import Config


class ResponseTests(unittest.TestCase):
    def handler(self, stream):
        handler = object.__new__(Handler)
        handler.request_version = 'HTTP/1.1'
        handler.command = 'GET'
        handler.close_connection = False
        handler.log_request = Mock()
        handler.wfile = stream
        return handler

    def test_browser_disconnect_during_headers_or_body_closes_request(self):
        for error in (BrokenPipeError, ConnectionResetError):
            for failing_write in (1, 2):
                with self.subTest(error=error, failing_write=failing_write):
                    stream = Mock()
                    stream.write.side_effect = ([error()] if failing_write == 1 else [1, error()])
                    handler = self.handler(stream)
                    handler._json(200, {'status': 'RUNNING'})
                    self.assertTrue(handler.close_connection)
                    self.assertEqual(stream.write.call_count, failing_write)

    def test_normal_response_remains_complete(self):
        stream = io.BytesIO()
        self.handler(stream)._json(200, {'status': 'RUNNING'})
        headers, body = stream.getvalue().split(b'\r\n\r\n', 1)
        self.assertIn(b'200 OK', headers)
        self.assertIn(b'Content-Length: ' + str(len(body)).encode(), headers)
        self.assertEqual(body, b'{"status":"RUNNING"}')

    def test_unrelated_write_errors_remain_visible(self):
        stream = Mock()
        stream.write.side_effect = OSError('Unexpected I/O failure')
        with self.assertRaisesRegex(OSError, 'Unexpected I/O failure'):
            self.handler(stream)._json(200, {})

    def test_host_and_origin_checks_block_cross_site_controls(self):
        for headers, expected in (({'Host': '127.0.0.1:8765'}, True),
                                  ({'Host': 'attacker.invalid:8765'}, False),
                                  ({'Host': '127.0.0.1:8765', 'Origin': 'https://attacker.invalid'}, False),
                                  ({'Host': 'localhost:8765', 'Origin': 'http://localhost:8765'}, True)):
            handler = self.handler(io.BytesIO())
            handler.config = Config()
            handler.headers = headers
            self.assertEqual(handler._local_request(), expected)

    def test_duplicate_port_fails_before_touching_database(self):
        with patch.object(server, 'engine_locks', return_value=nullcontext()), patch.object(server, 'load_env'), patch.object(server.Config, 'from_env', return_value=Config()), \
                patch.object(server, 'ThreadingHTTPServer', side_effect=OSError('Port already in use')), \
                patch.object(server, 'Store') as store:
            with self.assertRaisesRegex(OSError, 'Port already in use'):
                server.main()
            store.assert_not_called()
