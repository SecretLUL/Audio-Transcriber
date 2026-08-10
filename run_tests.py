"""Run the full test suite.

    python run_tests.py           all tests
    python run_tests.py -v        with per-test output
    python run_tests.py dsp       only tests/test_dsp.py
"""

import sys
import unittest


def main():
    names = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    verbosity = 2 if "-v" in sys.argv else 1

    loader = unittest.TestLoader()
    if names:
        suite = unittest.TestSuite(
            loader.loadTestsFromName(f"tests.test_{name}") for name in names)
    else:
        suite = loader.discover("tests", top_level_dir=".")

    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
