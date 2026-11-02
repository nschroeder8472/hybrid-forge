"""Run every `tests/*_test.py` inside Blender and exit non-zero on a failure.

Started by the workspace's test command:

    blender -b --factory-startup --python-exit-code 1 --python tests/run_checks.py

`--python-exit-code 1` matters on its own: without it Blender exits 0 when a
script raises, so a check that crashed would pass. This script also calls
`sys.exit` with the suite's result, so a failing assertion fails the command
whichever way the flag is set.

Output is unittest's ordinary verbose format, which forge already knows how to
attribute to a file and a test.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    suite = unittest.defaultTestLoader.discover(HERE, pattern="*_test.py", top_level_dir=ROOT)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    sys.stdout.flush()
    sys.exit(0 if result.wasSuccessful() else 1)


main()
