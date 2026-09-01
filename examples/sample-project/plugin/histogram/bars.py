"""Rendering counts as text bars."""


def bars(counts, width=20):
    """One line per word, tallest first, ties broken alphabetically.

    `width` is the length of the longest bar; every other bar is scaled
    against it and rounded down. An empty mapping renders nothing.
    """
    if not counts:
        return []
    tallest = max(counts.values())
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"{word} {'#' * (count * width // tallest)}" for word, count in ranked]
