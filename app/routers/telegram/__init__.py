"""Auto-split from the original telegram.py monolith. No behavior change."""
from ._common import router
from . import keyboards  # noqa: F401
from . import senders    # noqa: F401
from . import webhook    # noqa: F401  (registers /webhook etc.)
from . import endpoints  # noqa: F401  (registers admin endpoints)
