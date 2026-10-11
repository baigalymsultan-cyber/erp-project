#!/usr/bin/env python3
"""Run one arena brief through an external model (default glm-5.3 via apinex).

The arena's briefs tell the agent to write its output files itself, which an
API model cannot do. This runner appends a MECHANICAL wrapper to the brief --
the task, requirements and strategy card are byte-identical; only the output
channel changes from "write files" to "return file contents in markers".

Usage:
    glm_runner.py <brief.md> [--model glm-5.3] [--max-tokens 16000]

The model must return:
    === FILE: /absolute/path/from/the/brief ===
    <file content, exactly as it should be written>
    === END FILE ===
    (repeated per output file, then the one-line status, e.g. DONE a001 1234)

Every FILE path is validated to live inside the arena run dir named in the
brief; anything else is refused.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
APINEX = os.path.expanduser("~/workspace/skills/apinex/bin/apinex.py")

WRAPPER = """
=== OUTPUT PROTOCOL (mechanical; the task above is unchanged) ===
You cannot write files. Return every file the brief asks you to write wrapped
in markers, then your one-line status reply last. Format exactly:

=== FILE: /absolute/path/copied/verbatim/from/the/brief ===
<the complete file content, nothing omitted>
=== END FILE ===

Rules: copy each output path VERBATIM from the brief (the lines saying
"Write ... to <path>"). One block per file, in the order the brief lists
them. No prose outside the blocks except the final status line, which goes
after the last === END FILE === on its own line.
"""

FILE_RE = re.compile(
    r"=== FILE:\s*(?P<path>\S+)\s*===\n(?P<body>.*?)\n=== END FILE ===",
    re.DOTALL,
)
ARENA_DIR_RE = re.compile(r"/\S*?\.arena/run-[^/\s]+")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("--model", default="glm-5.3")
    ap.add_argument("--max-tokens", type=int, default=16000)
    args = ap.parse_args()

    with open(args.brief) as f:
        brief = f.read()

    m = ARENA_DIR_RE.search(brief)
    if not m:
        print("refusing: no .arena/run dir found in brief", file=sys.stderr)
        return 2
    arena_dir = os.path.normpath(m.group(0))

    prompt = brief + WRAPPER
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", delete=False
    ) as tf:
        tf.write(prompt)
        prompt_file = tf.name

    try:
        proc = subprocess.run(
            [
                sys.executable,
                APINEX,
                "chat",
                "--model",
                args.model,
                "--prompt-file",
                prompt_file,
                "--max-tokens",
                str(args.max_tokens),
            ],
            capture_output=True,
            text=True,
            timeout=1200,
        )
    finally:
        os.unlink(prompt_file)

    if proc.returncode != 0:
        print(f"apinex failed: {proc.stderr[-2000:]}", file=sys.stderr)
        return 1

    out = proc.stdout
    blocks = list(FILE_RE.finditer(out))
    if not blocks:
        print("no FILE blocks in model output; raw tail:", file=sys.stderr)
        print(out[-2000:], file=sys.stderr)
        return 1

    for b in blocks:
        path = os.path.normpath(b.group("path"))
        if not path.startswith(arena_dir + os.sep):
            print(f"refusing path outside arena dir: {path}", file=sys.stderr)
            return 2
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(b.group("body").rstrip("\n") + "\n")
        print(f"wrote {path} ({len(b.group('body'))} chars)")

    # status line: last non-empty line that is not part of a block
    tail = FILE_RE.sub("", out).strip().splitlines()
    tail = [ln.strip() for ln in tail if ln.strip()]
    if tail:
        print("STATUS: " + tail[-1])
    return 0


if __name__ == "__main__":
    sys.exit(main())
