"""Canonical chunker for the assessment corpus.

Rule: a chunk starts at every markdown heading line (any level) and runs to
the line before the next heading. Chunks are numbered from 0 in file order,
including headings with no body. Text before the first heading, if any, is
chunk 0. Chunk ID format: <stem>::chunk<N>.
"""
from dataclasses import dataclass
from pathlib import Path
import re

HEADING = re.compile(r"^#{1,6}\s")


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    content: str


def chunk_file(path: Path) -> list[Chunk]:
    stem = path.stem
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if HEADING.match(line) and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    # drop a leading block that is only whitespace (file starts with blank lines)
    if blocks and not "".join(blocks[0]).strip():
        blocks = blocks[1:]
    return [
        Chunk(id=f"{stem}::chunk{i}", source=path.name, content="\n".join(b).strip())
        for i, b in enumerate(blocks)
    ]


def chunk_corpus(docs_dir: Path) -> list[Chunk]:
    out: list[Chunk] = []
    for p in sorted(docs_dir.glob("*.md")):
        out.extend(chunk_file(p))
    return out


if __name__ == "__main__":
    import sys
    for c in chunk_corpus(Path(sys.argv[1] if len(sys.argv) > 1 else "docs")):
        print(c.id, "|", c.content.splitlines()[0][:60])
