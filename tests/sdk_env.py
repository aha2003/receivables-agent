"""Imported first by tests/conftest.py, before anything can import the anthropic SDK (D-032).

The SDK reads ``ANTHROPIC_LOG`` once, at import, and would then turn on its own debug logging.
The root conftest loads before every other conftest and test module, so removing the variable
here keeps that logging off for the whole run, however pytest is started.
"""

import os
import sys

assert "anthropic" not in sys.modules, "the SDK was imported before conftest could clean up"
os.environ.pop("ANTHROPIC_LOG", None)
