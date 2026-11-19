"""Cost models (plan §5). Importing this package registers v0-v3 (ladder.py)."""
from . import ladder  # noqa: F401  (registers the models)
from .registry import MODELS, make, register_model  # noqa: F401
