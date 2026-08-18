import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import fanqie_reader


class BookAiSummaryTests(unittest.TestCase):
    def test_missing_flag_defaults_to_enabled_when_loading(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "books.json"
            path.write_text(json.dumps([{"name": "旧书"}, {"name": "关闭书", "ai_summary": False}]), encoding="utf-8")
            books = fanqie_reader.load_books_from_json(path)
        self.assertEqual([book.ai_summary for book in books], [True, False])

    def test_save_device_books_preserves_per_book_flag(self):
        devices = {}
        with patch.object(app, "load_devices", return_value=devices), \
                patch.object(app, "save_devices"), \
                patch.object(app, "reload_config"):
            app.save_device_books("device", [
                {"name": "开", "ai_summary": True},
                {"name": "关", "ai_summary": False},
                {"name": "旧格式"},
            ])
        saved = devices["device"]["books"]
        self.assertEqual([book["ai_summary"] for book in saved], [True, False, True])

    def test_device_loader_reads_disabled_flag_and_defaults_old_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            devices = root / "devices.json"
            books = root / "books.json"
            devices.write_text(json.dumps({
                "device": {"books": [{"name": "开"}, {"name": "关", "ai_summary": False}]}
            }), encoding="utf-8")
            with patch.object(fanqie_reader, "DEVICES_FILE", devices), \
                    patch.object(fanqie_reader, "BOOKS_FILE", books):
                loaded = fanqie_reader.load_books_for_serial("device")
        self.assertEqual([book.ai_summary for book in loaded], [True, False])


if __name__ == "__main__":
    unittest.main()
