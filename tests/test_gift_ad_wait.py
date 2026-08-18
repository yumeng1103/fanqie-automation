import unittest
from unittest.mock import MagicMock, call

from fanqie_reader import FanqieBot, GIFT_PANEL_MARKER


class GiftAdWaitTests(unittest.TestCase):
    def test_waits_thirty_seconds_before_checking_for_returned_panel(self):
        bot = object.__new__(FanqieBot)
        bot.d = MagicMock()
        bot.log = MagicMock()
        bot.sleep_human = MagicMock()
        bot.click_text = MagicMock(return_value=False)
        bot._current_activity = MagicMock(return_value=".video.AdActivity")

        panel = MagicMock()
        panel.exists.return_value = True
        bot.d.return_value = panel

        self.assertTrue(bot._watch_ad_gift())
        self.assertEqual(bot.sleep_human.call_args_list[0], call(30.0, 32.0))
        bot.d.assert_called_once_with(text=GIFT_PANEL_MARKER)
        bot.d.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
