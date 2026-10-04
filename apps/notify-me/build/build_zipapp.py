# -*- coding: utf-8 -*-
"""Write the application zipapp. The output path is required."""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from notify_me_app.installation import build_zipapp_bytes  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the notify-me application zipapp")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    blob, _label, _digest = build_zipapp_bytes()
    with open(args.output, "wb") as handle:
        handle.write(blob)
    os.chmod(args.output, 0o700)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
