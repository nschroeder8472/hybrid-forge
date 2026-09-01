# Punctuation is counted as part of the word

Counting a sentence that has any punctuation in it gives the wrong answer.

    >>> from wordcount.counter import count_words
    >>> count_words("Hello, world! Hello again.")
    {'hello,': 1, 'world!': 1, 'hello': 1, 'again.': 1}

"Hello," and "Hello" are the same word and should be counted together, so the
answer should be `{'hello': 2, 'world': 1, 'again': 1}`. Leading and trailing
punctuation should not be part of the word. An apostrophe inside a word is part
of it: "don't" is one word, not "don" and "t".

The existing suite does not catch this — every test in it uses text with no
punctuation at all, which is why it has been green the whole time.
