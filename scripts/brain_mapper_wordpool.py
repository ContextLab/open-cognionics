"""Write the word pool of the Words task from the Toronto Word Pool noun file.

    uv run python scripts/brain_mapper_wordpool.py PATH/TO/nouns.txt

Reads `nouns.txt` as distributed by the University of Pennsylvania Computational
Memory Lab (tab-separated: word, imagery, Kucera-Francis frequency, concreteness;
a header line; comment text in columns to the right of the data; CRLF line ends)
and writes `brain_mapper/src/brain_mapper/web/assets/wordpool.txt`: the words
only, lower case, one per line, LF line ends, in the order of the file. The
norms and the comment text are not copied. Credits are in `web/assets/CREDITS.md`.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

OUT = (
    Path(__file__).resolve().parents[1]
    / "brain_mapper" / "src" / "brain_mapper" / "web" / "assets" / "wordpool.txt"
)


def words_of(text: str) -> list[str]:
    """The words of the noun file: column 1 of every line after the header, lower case."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].split("\t")[0].strip().lower() != "word":
        raise ValueError("the first line is not the header (word, imagery, ...)")
    words = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        word = line.split("\t")[0].strip().lower()
        if not re.fullmatch(r"[a-z]+", word):
            raise ValueError(f"line {number}: {word!r} is not a word of letters a-z")
        words.append(word)
    if len(set(words)) != len(words):
        raise ValueError("the file holds a word twice")
    return words


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("nouns", type=Path, help="the nouns.txt file")
    args = parser.parse_args()
    words = words_of(args.nouns.read_text(encoding="utf-8"))
    OUT.write_text("".join(w + "\n" for w in words), encoding="utf-8", newline="\n")
    print(f"wrote {len(words)} words to {OUT}")


if __name__ == "__main__":
    main()
