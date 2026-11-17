#!/usr/bin/env python3
"""Chrome native-messaging entry point. Never emit ordinary text on stdout."""
import argparse
import sys

from cleanmac_core.browser_bridge import serve_native
from cleanmac_core.state import StateStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--extension-id", required=True)
    parser.add_argument("origin")
    args = parser.parse_args()
    try:
        serve_native(StateStore(args.data_dir), args.extension_id, args.origin)
    except Exception:
        # Chrome receives a disconnect. Paths, tokens, page titles and URLs do
        # not leak into browser logs through an exception traceback.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
