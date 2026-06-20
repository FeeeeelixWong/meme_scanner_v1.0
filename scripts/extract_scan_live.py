#!/usr/bin/env python3
"""Extract scan_live.py from the Meme Scanner skill markdown."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


def extract_python(markdown_path: Path) -> str:
    content = markdown_path.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", content, re.DOTALL)
    if not blocks:
        raise SystemExit(f"No python blocks found in {markdown_path}")

    code = "\n\n".join(blocks)
    line_count = len(code.splitlines())
    if line_count < 500:
        raise SystemExit(f"Extraction looks too short: {line_count} lines")

    ast.parse(code)
    return code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "skill_file",
        nargs="?",
        default="meme_scanner_v1.0.md",
        help="Path to the skill markdown file.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="scan_live.py",
        help="Output Python file path.",
    )
    args = parser.parse_args()

    markdown_path = Path(args.skill_file)
    output_path = Path(args.output)
    code = extract_python(markdown_path)
    output_path.write_text(code, encoding="utf-8")
    print(f"Extracted {len(code.splitlines())} lines -> {output_path}")


if __name__ == "__main__":
    main()
