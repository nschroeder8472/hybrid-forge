import unittest

from histogram.bars import bars


class BarsTest(unittest.TestCase):
    def test_the_tallest_bar_is_the_full_width(self):
        self.assertEqual(bars({"a": 2}, width=4), ["a ####"])

    def test_shorter_bars_are_scaled_against_the_tallest(self):
        self.assertEqual(bars({"a": 4, "b": 2}, width=4), ["a ####", "b ##"])

    def test_ties_are_broken_alphabetically(self):
        self.assertEqual(bars({"b": 1, "a": 1}, width=1), ["a #", "b #"])

    def test_nothing_counted_renders_nothing(self):
        self.assertEqual(bars({}), [])


if __name__ == "__main__":
    unittest.main()
