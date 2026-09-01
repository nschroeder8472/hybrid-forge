"""Counting the words in a piece of text."""


def count_words(text):
    """How many times each word appears in `text`, case-folded.

    Words are whatever whitespace separates. Returns a dict of word to count;
    an empty text counts nothing.
    """
    counts = {}
    for word in text.split():
        key = word.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts
