"""Run explicitly named test suites; core deliberately excludes neural fitting."""
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("core", "full"), default="full")
    args = parser.parse_args()
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if args.suite == "core" and path.stem == "test_neural_models":
            print("Core suite excludes real TensorFlow fitting; full suite remains required in CI.")
            continue
        suite.addTests(loader.loadTestsFromName("tests." + path.stem))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f"suite={args.suite}; run={result.testsRun}; skipped={len(result.skipped)}; failures={len(result.failures)}; errors={len(result.errors)}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
