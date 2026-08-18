import json
import unittest
from unittest.mock import patch

import app


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


class ModelDetectionTests(unittest.TestCase):
    def test_fetches_and_sorts_model_ids(self):
        response = Response({"object": "list", "data": [{"id": "model-b"}, {"id": "model-a"}]})
        with patch("app.urllib.request.urlopen", return_value=response) as request:
            result = app._fetch_vision_models({
                "base_url": "https://api.example.test/v1/",
                "api_key": "test-key",
                "timeout": 8,
            })
        self.assertEqual(result["models"], ["model-a", "model-b"])
        sent_request = request.call_args.args[0]
        self.assertEqual(sent_request.full_url, "https://api.example.test/v1/models")
        self.assertEqual(sent_request.headers["Authorization"], "Bearer test-key")


if __name__ == "__main__":
    unittest.main()
