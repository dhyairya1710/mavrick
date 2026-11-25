"""Module entry point used by the Mavrick user service."""

import sys

from . import main

if __name__ == "__main__":
    try:
        main()
    except (ImportError, RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
