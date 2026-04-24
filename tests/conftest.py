"""Pytest configuration — garante que `src/` está no sys.path e que as settings
têm valores dummy durante os testes."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Valores dummy para Pydantic Settings nos testes unitários
os.environ.setdefault("POLYGON_PRIVATE_KEY", "0" * 64)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
os.environ.setdefault("LIVE_MODE", "false")
