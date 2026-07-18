#!/usr/bin/env python3
"""Regenerate the GDD §16 configuration table from the declarative schema.

The schema (``brain/cortana/config_schema.py``) is the single source of truth
for every key's type, default, reload class, and doc line. This script renders
that table into ``docs/GDD.md`` between the GENERATED CONFIG TABLE markers, so
the spec can never drift from the code — CI runs ``--check`` and fails the
build when the committed table is stale.

Usage:
    python scripts/gen_config_docs.py            # rewrite docs/GDD.md in place
    python scripts/gen_config_docs.py --check    # exit 1 if the table is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "brain"))

BEGIN = "<!-- BEGIN GENERATED CONFIG TABLE (scripts/gen_config_docs.py) -->"
END = "<!-- END GENERATED CONFIG TABLE -->"


def render_table() -> str:
    from cortana.config_schema import KEYS, REQUIRED, SECTIONS

    lines = [
        BEGIN,
        "",
        "| Key | Type | Default | Reload | Purpose |",
        "|---|---|---|---|---|",
    ]
    section_docs = {s.path: s.doc for s in SECTIONS}
    seen_sections: set[str] = set()
    for key in KEYS:
        top = key.path.split(".", 1)[0]
        if top not in seen_sections:
            seen_sections.add(top)
            doc = section_docs.get(top, "")
            lines.append(f"| **`{top}:`** | | | | *{doc}* |")
        default = "**required**" if key.default is REQUIRED else f"`{key.default!r}`"
        choices = f" One of: {', '.join(f'`{c}`' for c in key.choices)}." if key.choices else ""
        doc = " ".join(key.doc.split())  # collapse newlines from wrapped docstrings
        lines.append(f"| `{key.path}` | {key.type} | {default} | {key.reload.value} | {doc}{choices} |")
    lines += ["", END]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the table is stale")
    args = parser.parse_args(argv)

    gdd = REPO / "docs" / "GDD.md"
    text = gdd.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"ERROR: markers not found in {gdd} — cannot place the generated table")
        return 2
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    new = head + render_table() + tail
    if args.check:
        if new != text:
            print("GDD §16 config table is STALE — run: python scripts/gen_config_docs.py")
            return 1
        print("GDD §16 config table is up to date")
        return 0
    if new != text:
        gdd.write_text(new, encoding="utf-8")
        print(f"rewrote the generated table in {gdd}")
    else:
        print("already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
