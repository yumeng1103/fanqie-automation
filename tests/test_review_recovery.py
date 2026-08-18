import unittest
from unittest.mock import MagicMock

from fanqie_reader import FanqieBot, UNSAVED_INPUT_PROMPT


class ReviewRecoveryTests(unittest.TestCase):
    def test_unsaved_dialog_uses_return_instead_of_cancel(self):
        bot = object.__new__(FanqieBot)
        bot.d = MagicMock()
        bot.log = MagicMock()
        bot.sleep_human = MagicMock()
        bot.click_text = MagicMock(return_value=True)
        bot.d.return_value.exists.return_value = True

        self.assertTrue(bot._confirm_discard_input())
        bot.d.assert_called_once_with(textContains=UNSAVED_INPUT_PROMPT)
        bot.click_text.assert_called_once_with("返回", 0.8)

    def test_review_editor_back_press_confirms_discard(self):
        bot = object.__new__(FanqieBot)
        bot.d = MagicMock()
        bot.log = MagicMock()
        bot.sleep_human = MagicMock()
        bot._current_activity = MagicMock(side_effect=[
            "com.dragon.read.social.template.AITextTemplateActivity",
            "com.dragon.read.MainActivity",
        ])
        bot._confirm_discard_input = MagicMock(side_effect=[False, True])

        self.assertTrue(bot._leave_review_editor())
        bot.d.press.assert_called_once_with("back")
        self.assertEqual(bot._confirm_discard_input.call_args_list[1].args, (1.2,))

    def test_click_text_treats_uiautomator_none_as_success(self):
        bot = object.__new__(FanqieBot)
        bot.log = MagicMock()
        bot.d = MagicMock()
        selector = MagicMock()
        selector.click.return_value = None
        bot.d.side_effect = [selector, MagicMock()]

        self.assertTrue(bot.click_text("下一步", 2.0))
        selector.click.assert_called_once_with(2.0)

    def test_review_editor_keeps_returning_when_keyboard_only_was_dismissed(self):
        bot = object.__new__(FanqieBot)
        bot.d = MagicMock()
        bot.log = MagicMock()
        bot.sleep_human = MagicMock()
        bot._current_activity = MagicMock(side_effect=[
            "com.dragon.read.social.template.AITextTemplateActivity",
            "com.dragon.read.social.template.AITextTemplateActivity",
            "com.dragon.read.MainActivity",
        ])
        bot._confirm_discard_input = MagicMock(return_value=False)

        self.assertTrue(bot._leave_review_editor())
        self.assertEqual(bot.d.press.call_count, 2)


if __name__ == "__main__":
    unittest.main()
