import json
import tempfile
import unittest
from pathlib import Path

from vision_reader import (
    OpenAIVisionClient,
    VisionReadingSession,
    VisionSettings,
    extract_chapter_label,
    extract_model_ids,
    extract_response_text,
)


class Runtime:
    def __init__(self):
        self.data = {"vision": {"summaries": []}}

    def get(self):
        return self.data

    def update_vision(self, **fields):
        self.data.setdefault("vision", {}).update(fields)


class VisionProtocolTests(unittest.TestCase):
    def test_chapter_label_removes_page_and_clock(self):
        self.assertEqual(
            extract_chapter_label("第4章 赶车老汉指路 58/227 18:44 本章讨论"),
            "第4章 赶车老汉指路",
        )

    def test_extracts_responses_and_chat_shapes(self):
        self.assertEqual(extract_response_text({"output_text": "hello"}), "hello")
        self.assertEqual(
            extract_response_text({"output": [{"content": [{"type": "output_text", "text": "正文"}]}]}),
            "正文",
        )

    def test_normalizes_models_response(self):
        self.assertEqual(
            extract_model_ids({"data": [{"id": "gpt-4o-mini"}, {"id": "GPT-5"}, {"id": "gpt-4o-mini"}]}),
            ["gpt-4o-mini", "GPT-5"],
        )
        self.assertEqual(extract_model_ids({"models": ["z-model", "a-model"]}), ["a-model", "z-model"])
        self.assertEqual(
            extract_response_text({"choices": [{"message": {"content": "摘要"}}]}),
            "摘要",
        )

    def test_responses_payload_contains_input_image(self):
        calls = []

        def request(endpoint, body, timeout):
            calls.append((endpoint, json.loads(body.decode("utf-8"))))
            return {"output_text": "识别出的正文"}

        client = OpenAIVisionClient(
            VisionSettings(enabled=True, base_url="https://api.openai.com/v1", api_key="test"),
            request_fn=request,
        )
        self.assertEqual(client.extract_page(b"png", page_no=4), "识别出的正文")
        self.assertEqual(calls[0][0], "/responses")
        content = calls[0][1]["input"][0]["content"]
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))

    def test_chat_compatibility_fallback(self):
        endpoints = []

        def request(endpoint, body, timeout):
            endpoints.append(endpoint)
            if endpoint == "/responses":
                raise RuntimeError("unsupported")
            return {"choices": [{"message": {"content": "兼容正文"}}]}

        client = OpenAIVisionClient(
            VisionSettings(enabled=True, base_url="https://proxy.example/v1", api_key="test"),
            request_fn=request,
        )
        self.assertEqual(client.extract_page(b"png"), "兼容正文")
        self.assertEqual(endpoints, ["/responses", "/chat/completions"])

    def test_chat_compatibility_normalizes_image_detail(self):
        payloads = []

        def request(endpoint, body, timeout):
            if endpoint == "/responses":
                raise RuntimeError("unsupported")
            payloads.append(json.loads(body.decode("utf-8")))
            return {"choices": [{"message": {"content": "兼容正文"}}]}

        client = OpenAIVisionClient(
            VisionSettings(enabled=True, base_url="https://proxy.example/v1", api_key="test", detail="high"),
            request_fn=request,
        )
        self.assertEqual(client.extract_page(b"png"), "兼容正文")
        image = payloads[0]["messages"][0]["content"][1]
        self.assertEqual(image["image_url"]["detail"], "auto")

    def test_summary_uses_separate_text_model(self):
        models = []

        def request(endpoint, body, timeout):
            payload = json.loads(body.decode("utf-8"))
            models.append(payload["model"])
            return {"output_text": "章节摘要"}

        client = OpenAIVisionClient(
            VisionSettings(
                enabled=True,
                base_url="https://proxy.example/v1",
                api_key="test",
                model="vision-model",
                summary_model="text-model",
            ),
            request_fn=request,
        )
        self.assertEqual(client.summarize_chapter("第1章", ["正文"]), "章节摘要")
        self.assertEqual(models, ["text-model"])

    def test_session_flushes_summary_and_persists(self):
        class FakeClient:
            available = True

            def extract_page(self, image, page_no=None):
                return f"第{page_no}页正文"

            def summarize_chapter(self, chapter, pages):
                return f"{chapter}摘要({len(pages)}页)"

        runtime = Runtime()
        with tempfile.TemporaryDirectory() as folder:
            session = VisionReadingSession(
                VisionSettings(enabled=True, base_url="x", api_key="y"), runtime,
                book="测试书", serial="device", client=FakeClient(),
                persist_path=Path(folder) / "summaries.json",
            )
            self.assertTrue(session.submit_page(b"one", page_no=1, ui_text="第1章"))
            self.assertTrue(session.submit_page_content("第二页正文", page_no=2, ui_text="第1章"))
            summary = session.flush_chapter("第1章", final=True)
            session.close()
            self.assertEqual(summary, "第1章摘要(2页)")
            self.assertEqual(runtime.data["vision"]["pages_extracted"], 2)
            self.assertEqual(runtime.data["vision"]["summaries"][0]["chapter"], "第1章")
            saved = json.loads((Path(folder) / "summaries.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["device:测试书"][0]["summary"], summary)

    def test_new_session_overrides_stale_enabled_state_and_book(self):
        class FakeClient:
            available = True

        runtime = Runtime()
        runtime.data["vision"].update({"enabled": False, "book": "旧书"})
        session = VisionReadingSession(
            VisionSettings(enabled=True, base_url="x", api_key="y"),
            runtime,
            book="新书",
            serial="device",
            client=FakeClient(),
        )
        try:
            self.assertTrue(runtime.data["vision"]["enabled"])
            self.assertEqual(runtime.data["vision"]["book"], "新书")
        finally:
            session.close()

    def test_chapter_change_flushes_previous_pages_and_close_flushes_last(self):
        class FakeClient:
            available = True

            def extract_page(self, image, page_no=None):
                return f"第{page_no}页正文"

            def summarize_chapter(self, chapter, pages):
                return f"{chapter}摘要({len(pages)}页)"

        runtime = Runtime()
        with tempfile.TemporaryDirectory() as folder:
            session = VisionReadingSession(
                VisionSettings(enabled=True, base_url="x", api_key="y"),
                runtime,
                book="测试书",
                serial="device",
                client=FakeClient(),
                persist_path=Path(folder) / "summaries.json",
            )
            self.assertTrue(session.submit_page(b"page-1", page_no=1, ui_text="第1章 开端 1/2"))
            self.assertTrue(session.submit_page(b"page-2", page_no=2, ui_text="第2章 转折 1/2"))
            self.assertEqual(runtime.data["vision"]["summaries"][0]["chapter"], "第1章 开端")
            session.close()
            saved = json.loads((Path(folder) / "summaries.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["device:测试书"]), 2)
            self.assertEqual(saved["device:测试书"][1]["chapter"], "第2章 转折")


if __name__ == "__main__":
    unittest.main()
