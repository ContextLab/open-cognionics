"""Runs in a subprocess of test_package_safety.py: a session zip that the disk refuses part-way.

The file size limit (RLIMIT_FSIZE) is set here, on this process only, never in the pytest process:
it applies to every file the process writes. Usage:

    python zip_fsize_driver.py <session folder> <limit in bytes>

``session.json`` is completed first (``package.describe``, under no limit), then the limit is set
and ``package.finish`` runs: the temporary zip reaches the limit and the write fails, as on a full
disk. Prints one JSON line: the error (type, errno, words), the names in the sessions folder
afterwards and the limit.
"""

from __future__ import annotations

import json
import resource
import signal
import sys
from pathlib import Path

from brain_mapper import package


def main() -> None:
    folder, limit = Path(sys.argv[1]), int(sys.argv[2])
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # the default would end this process
    package.describe(folder, complete=False)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))
    error: dict | None = None
    try:
        package.finish(folder, complete=False)
    except OSError as failure:
        error = {"type": type(failure).__name__, "errno": failure.errno, "words": package.in_words(failure)}
    print(json.dumps({"error": error, "names": sorted(p.name for p in folder.parent.iterdir()), "limit": limit}))


if __name__ == "__main__":
    main()
