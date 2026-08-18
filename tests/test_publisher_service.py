import tempfile
import unittest
from pathlib import Path

from publisher_service import PublisherManager, parse_chapter


class PublisherServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = PublisherManager(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_parses_arabic_and_chinese_chapter_titles(self):
        source = Path(self.manager.get_config()["source_dir"]) / "书"
        source.mkdir(parents=True)
        arabic = source / "第12章 风暴.txt"
        arabic.write_text("第12章 风暴\n正文", encoding="utf-8")
        chinese = source / "第十二章 回声.txt"
        chinese.write_text("第十二章 回声\n第二段", encoding="utf-8")

        self.assertEqual(parse_chapter(arabic).number, 12)
        self.assertEqual(parse_chapter(chinese).label, "第12章 回声")

    def test_list_books_marks_invalid_files_without_hiding_book(self):
        source = Path(self.manager.get_config()["source_dir"]) / "书"
        source.mkdir(parents=True)
        (source / "第1章 有效.txt").write_text("第1章 有效\n正文", encoding="utf-8")
        (source / "第2章 缺正文.txt").write_text("第2章 缺正文\n", encoding="utf-8")

        self.assertEqual(self.manager.list_books(), [{
            "name": "书", "count": 2, "valid": 1, "invalid": 1,
        }])

    def test_rejects_nested_source_and_archive(self):
        with self.assertRaises(ValueError):
            self.manager.save_config({
                "source_dir": str(self.root / "root"),
                "archive_dir": str(self.root / "root" / "uploaded"),
            })

    def test_status_never_exposes_storage_state(self):
        self.manager.state_file.write_text('{"cookies":[{"name":"session"}]}', encoding="utf-8")
        status = self.manager.status()
        self.assertTrue(status["logged_in"])
        self.assertNotIn("cookies", status)
        self.assertNotIn("session", json_text(status))

    def test_start_requires_existing_book_and_login(self):
        with self.assertRaises(ValueError):
            self.manager.start_publish({"mode": "immediate", "book": "不存在"})

        source = Path(self.manager.get_config()["source_dir"]) / "书"
        source.mkdir(parents=True)
        (source / "第1章 标题.txt").write_text("第1章 标题\n正文", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.manager.start_publish({"mode": "immediate", "book": "书"})

    def test_archive_never_overwrites_existing_file(self):
        source = Path(self.manager.get_config()["source_dir"]) / "书"
        source.mkdir(parents=True)
        chapter = source / "第1章 标题.txt"
        chapter.write_text("第1章 标题\n正文", encoding="utf-8")
        self.manager.state_file.write_text("{}", encoding="utf-8")
        parsed = parse_chapter(chapter)
        destination = Path(self.manager.get_config()["archive_dir"]) / "书" / "第一卷"
        destination.mkdir(parents=True)
        (destination / chapter.name).write_text("历史归档", encoding="utf-8")

        archived = self.manager._archive_chapter(parsed, {"book": "书", "mode": "immediate", "volume": 1})

        self.assertTrue(archived.is_file())
        self.assertTrue((destination / chapter.name).read_text(encoding="utf-8") == "历史归档")
        self.assertFalse(chapter.exists())


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
