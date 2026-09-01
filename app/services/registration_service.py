"""One admission rule shared by every route that lets a STRANGER create a User.

Deliberately not every route that can create one: an admin granting caps, stream access or AI
access still creates the account it is granting to, and an operator adding a user is not
"registration". Those paths are `_verify_admin_auth`-gated and must keep working with signup
closed — a future reader tidying this up by adding the guard to them would break admin account
creation on a closed server.

The value is read from settings_store for every decision. That cache is populated with database
defaults during init and then hydrated from the operator-signed relay settings document at startup;
Admin saves update it immediately. Keeping this helper stateless avoids a restart-only toggle.
"""


#: The stored value when an operator has never touched the switch. Registration is OPEN by default:
#: closing it is a decision somebody makes, never one a deployment falls into.
_DEFAULT = "true"


def enabled() -> bool:
    from app.services import settings_store
    value = settings_store.get("registration_enabled", _DEFAULT)
    text = str(value if value is not None else "").strip().lower()
    # A BLANK ROW IS "NOT CONFIGURED", NEVER "CLOSED". `settings_store.get` returns the stored value
    # even when it is empty, so a row written blank — an admin save of an untouched field, a
    # migration, a hand-edited settings document — read as false and would have closed signup for
    # the whole node with nothing anywhere saying why. This deployment has already paid for that
    # exact shape once: a blank `searxng_enabled` turned web search off node-wide, silently.
    if not text:
        text = _DEFAULT
    return text in {"1", "true", "yes", "on"}


def closed_message() -> str:
    return "New user registration is disabled on this server."
