"""`python -m seizure_marker [FOLDER ...]` launches the marker window."""

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
