"""Compatibility entry point for Streamlit deployments that target ``main.py``.

The canonical app module is ``app.py``. Importing it runs the Streamlit app.
"""

import app  # noqa: F401 - import side effects render the Streamlit application
