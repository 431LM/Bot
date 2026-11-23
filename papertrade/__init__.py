"""Paper-trading baseline. This package never submits broker orders."""
from .config import Config
from .engine import PaperEngine
from .models import Bar, Side, Signal

__all__ = ["Bar", "Config", "PaperEngine", "Side", "Signal"]
