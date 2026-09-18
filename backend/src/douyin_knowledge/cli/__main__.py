"""Entry point for `python -m douyin_knowledge.cli`.

Deliberately here rather than in `main.py`: running `python -m douyin_knowledge.cli.main`
imports that module twice under two names, so the sibling command modules register their
commands on the first copy's `app` while `__main__` invokes a second, empty one -- the
pipeline and query commands silently vanish from `--help`. Importing `main` from a separate
module means there is only ever one `app`.
"""

from __future__ import annotations

from douyin_knowledge.cli.main import main

if __name__ == "__main__":
    main()
