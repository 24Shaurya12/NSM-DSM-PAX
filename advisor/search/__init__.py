"""Search strategies (plan §6.3). Importing registers the built-ins."""
from . import exhaustive  # noqa: F401
from .base import SEARCHES, SearchContext, make_search, register_search  # noqa: F401
