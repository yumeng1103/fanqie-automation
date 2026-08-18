import unittest

from app import Handler


class PublisherApiSecurityTests(unittest.TestCase):
    def test_publisher_api_allows_loopback(self):
        handler = object.__new__(Handler)
        handler.client_address = ("127.0.0.1", 12345)
        handler._json = lambda *_args, **_kwargs: self.fail("loopback request was rejected")

        self.assertTrue(handler._publisher_local_only())

    def test_publisher_api_rejects_lan_client(self):
        handler = object.__new__(Handler)
        handler.client_address = ("203.0.113.55", 12345)
        captured = {}
        handler._json = lambda body, code=200: captured.update(body=body, code=code)

        self.assertFalse(handler._publisher_local_only())
        self.assertEqual(captured["code"], 403)
        self.assertIn("仅允许", captured["body"]["error"])


if __name__ == "__main__":
    unittest.main()
