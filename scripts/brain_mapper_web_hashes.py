"""Makes (or checks) the list of sha256 hashes of the brain-mapper page files.

    uv run python scripts/brain_mapper_web_hashes.py           # rewrite the list
    uv run python scripts/brain_mapper_web_hashes.py --check   # exit 1 if the list is out of date

The list is ``brain_mapper/src/brain_mapper/web_files.json``: every file under
``brain_mapper/src/brain_mapper/web/`` (page files, fonts, assets and the vendored three.js) with
its sha256. ``brain-mapper --check`` and the start screen compare the installed files with it.
Run this after any change to a file under ``web/``; ``tests/brain_mapper/test_checks.py`` fails
until you have.
"""

from __future__ import annotations

import argparse
import json
import sys

from brain_mapper import checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if out of date")
    args = parser.parse_args(argv)
    found = checks.web_hashes()
    text = json.dumps({"files": found}, indent=1, sort_keys=True) + "\n"
    current = checks.RECORDED.read_text(encoding="utf-8") if checks.RECORDED.exists() else ""
    if args.check:
        if text != current:
            print(f"{checks.RECORDED} is out of date; run scripts/brain_mapper_web_hashes.py", file=sys.stderr)
            return 1
        return 0
    checks.RECORDED.write_text(text, encoding="utf-8")
    print(f"{len(found)} files recorded in {checks.RECORDED}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
