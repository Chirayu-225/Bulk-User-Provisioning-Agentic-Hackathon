"""Starts the mock admin console and the dashboard together.

  python run.py
  -> dashboard:     http://127.0.0.1:8000
  -> admin console: http://127.0.0.1:8001   (test harness: /__control)
"""
import subprocess
import sys
import time

import config

procs = [
    subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_console.app:app", "--host", "127.0.0.1",
                      "--port", str(config.CONSOLE_PORT), "--log-level", "warning"], cwd=config.ROOT),
    subprocess.Popen([sys.executable, "-m", "uvicorn", "dashboard.server:app", "--host", "127.0.0.1",
                      "--port", str(config.DASHBOARD_PORT), "--log-level", "warning"], cwd=config.ROOT),
]
print(f"Dashboard:      http://127.0.0.1:{config.DASHBOARD_PORT}")
print(f"Admin console:  http://127.0.0.1:{config.CONSOLE_PORT}  (login {config.CONSOLE_USER} / {config.CONSOLE_PASSWORD})")
print(f"Test harness:   http://127.0.0.1:{config.CONSOLE_PORT}/__control")
print("Press Ctrl+C to stop.")
try:
    while all(p.poll() is None for p in procs):
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    for p in procs:
        p.terminate()
