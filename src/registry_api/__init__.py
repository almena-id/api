"""Backend API of the Almena ID registry portal."""

import os
from importlib.metadata import version

# ALMENA_VERSION when the image sets it (year.month.sequence, see
# .github/workflows/docker.yml), else the package version.
__version__ = os.environ.get("ALMENA_VERSION") or version("registry-api")
