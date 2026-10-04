# -*- coding: utf-8 -*-
"""Placeholders for a source checkout.

The zipapp build replaces this module inside the archive. version --json prints
these constants and does not read git or hash the tree at runtime.
"""

CLI_VERSION = "1.0.0"
PROTOCOL_VERSION = 1
SOURCE_COMMIT = "dev"
BUILD_DIGEST = "dev"
PYTHON_REQUIRES = ">=3.9"
ARTIFACT = "source"
SOURCE_FILES = ()
