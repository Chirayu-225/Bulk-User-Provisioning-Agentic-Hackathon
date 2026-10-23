"""Central settings. Values come from environment variables or a local .env file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()

# Mock admin console (the demo target)
CONSOLE_PORT = int(os.getenv("CONSOLE_PORT", "8001"))
CONSOLE_URL = os.getenv("CONSOLE_URL", f"http://127.0.0.1:{CONSOLE_PORT}")
CONSOLE_USER = os.getenv("CONSOLE_USER", "it.admin")
CONSOLE_PASSWORD = os.getenv("CONSOLE_PASSWORD", "demo-pass-123")
CONSOLE_PUBLIC_URL = os.getenv("CONSOLE_PUBLIC_URL", CONSOLE_URL)  # link shown to people (may differ when hosted)

# Dashboard
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

# LLM: "gemini" (free tier key), "openai" (any OpenAI-compatible server, e.g. Ollama or Groq), or "none"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "ollama")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "qwen2.5:3b")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "20"))

# Browser
HEADLESS = os.getenv("HEADLESS", "1") == "1"
SLOW_MO_MS = int(os.getenv("SLOW_MO_MS", "0"))

# Agent behaviour
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))
ROLE_CONFIDENCE_THRESHOLD = float(os.getenv("ROLE_CONFIDENCE_THRESHOLD", "0.6"))
PRIVILEGED_ROLES = {"admin", "owner", "super admin", "superadmin", "administrator"}

# Storage
DATA_DIR = ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
EVIDENCE_DIR = DATA_DIR / "evidence"
DB_PATH = DATA_DIR / "ledger.db"
for _d in (DATA_DIR, RUNS_DIR, EVIDENCE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
