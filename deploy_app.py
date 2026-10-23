"""Single-process app for hosting (Hugging Face Spaces, Render, ...): the dashboard and the
mock admin console are served together on one port, and the agent's browser drives the
console over localhost.

  uvicorn deploy_app:app --host 0.0.0.0 --port 7860
"""
import os

PORT = os.getenv("PORT", "7860")
os.environ.setdefault("CONSOLE_URL", f"http://127.0.0.1:{PORT}")
os.environ.setdefault("CONSOLE_PUBLIC_URL", "/login")
os.environ.setdefault("HEADLESS", "1")

from dashboard.server import app  # noqa: E402
from mock_console.app import app as console_app  # noqa: E402

# Dashboard routes (/, /api, /static, /files) are registered first and win;
# everything else (/login, /tenants, /t/..., /__control) is the mock console.
app.mount("/", console_app)
