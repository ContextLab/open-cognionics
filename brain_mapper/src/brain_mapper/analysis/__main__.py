"""``python -m brain_mapper.analysis <session folder> [--out <folder>] [--no-zip]``

Computes the results of a session folder. Without ``--out`` they are saved as new files in the
session's ``analysis/`` folder with an additional zip beside the session folder; with ``--out``
they go into that folder instead and the session folder is only read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import pipeline, run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brain_mapper.analysis", description=__doc__.split("\n\n")[1])
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="write the results here; the session folder is only read")
    parser.add_argument("--no-zip", action="store_true", help="do not make the additional zip")
    args = parser.parse_args(argv)
    last = [-1]

    def progress(fraction: float, stage: str) -> None:
        percent = int(fraction * 100)
        if percent // 10 != last[0] // 10:
            print(f"{percent:3d} %  {stage}", flush=True)
        last[0] = percent

    try:
        results = run.analyze(args.folder, out_folder=args.out, progress=progress, zip_it=not args.no_zip)
    except pipeline.NotComputable as error:
        print(f"The results could not be computed: {error}.", file=sys.stderr)
        return 1
    a = results["analyzed"]
    print(
        f"analyzed {a['seconds']} s; {a['fraction_rejected'] * 100:.1f} % of 2 s segments rejected; "
        f"bad channels: {', '.join(results['channels']['bad']) or 'none'}; "
        f"computed in {results['compute_seconds']} s"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
