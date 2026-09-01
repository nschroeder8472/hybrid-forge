import unittest

from wordcount.counter import count_words


class CountWordsTest(unittest.TestCase):
    def test_each_occurrence_is_counted(self):
        self.assertEqual(count_words("a b a"), {"a": 2, "b": 1})

    def test_case_is_folded(self):
        self.assertEqual(count_words("Apple apple APPLE"), {"apple": 3})

    def test_empty_text_counts_nothing(self):
        self.assertEqual(count_words(""), {})


if __name__ == "__main__":
    unittest.main()
