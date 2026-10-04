# -*- coding: utf-8 -*-
"""Install the application launcher. Paths are explicit; nothing defaults to the host."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notify_me_app.cli import main  # noqa: E402


if __name__ == "__main__":
    argv = ["install"] + sys.argv[1:]
    raise SystemExit(main(argv))
