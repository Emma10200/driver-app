"""Compatibility entry point for hosts that still look for ``main.py``.

The Streamlit app's canonical entry point is ``app.py``. This thin wrapper is
kept so a cached deployment setting, old bookmark, or hosting integration that
expects ``main.py`` still runs the same application instead of failing to find a
source file.
"""

import app  # noqa: F401 - importing app runs the Streamlit application