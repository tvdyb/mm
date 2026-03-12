"""Configuration and environment variable loading."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# API Keys
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Paths
BASE_DIR = Path(__file__).parent
TASKS_DIR = BASE_DIR / "tasks"
LOGS_DIR = BASE_DIR / "logs"
STATE_FILE = BASE_DIR / "state.json"

# Defaults
DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_TURNS = 25
DEFAULT_CHECKPOINT_EVERY = 3
DEFAULT_NOTIFY_EVERY = 5

# Ensure directories exist
TASKS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
