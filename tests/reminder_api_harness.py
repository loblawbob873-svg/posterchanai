"""Real reminder code with isolated SQLAlchemy metadata and no production connections.

Load entire shipped modules, not extracted endpoint functions. Imports made later by the actual
route/delivery functions see these same boundaries for the lifetime of each test. Restore both
sys.modules and package attributes afterwards so unrelated tests keep their own module graph.
"""
from contextlib import contextmanager, ExitStack
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from fastapi import HTTPException
from sqlalchemy.orm import declarative_base
import importlib

for _package in ('app', 'app.services', 'app.routers'):
    importlib.import_module(_package)

ROOT = Path(__file__).resolve().parents[1]


def forbidden(*args, **kwargs):
    raise AssertionError('Production database or external service used by reminder test')


def unauthorized():
    raise HTTPException(status_code=401, detail='Authentication required')


@contextmanager
def reminder_modules():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules))
        def install(name, module):
            sys.modules[name] = module
            parent, attr = name.rsplit('.', 1)
            stack.enter_context(patch.object(sys.modules[parent], attr, module, create=True))
            return module
        def stub(name, **attrs):
            module = ModuleType(name);module.__dict__.update(attrs)
            return install(name, module)
        def load(name, relative):
            spec = importlib.util.spec_from_file_location(name, ROOT/relative)
            module = importlib.util.module_from_spec(spec)
            install(name, module)
            spec.loader.exec_module(module)
            return module
        stub('app.database', Base=declarative_base(), get_db=forbidden, SessionLocal=forbidden)
        models = load('app.models', 'app/models.py')
        load('app.schemas', 'app/schemas.py')
        stub('app.auth', get_current_user=unauthorized, verify_password=forbidden,
             create_access_token=forbidden, get_password_hash=forbidden)
        stub('app.services.email_service', EmailService=forbidden)
        stub('app.services.storage_service', StorageService=forbidden)
        settings = stub('app.services.settings_store', get=lambda *args: 7,
                        get_int=forbidden, get_bool=forbidden, set=forbidden)
        stub('app.services.chat_history', append=forbidden)
        stub('app.routers.chat', manager=SimpleNamespace(send_json=forbidden))
        stub('app.services.push_service', send=forbidden)
        stub('app.services.direct_push_service', subscription_dict=forbidden)
        stub('app.services.nostr', nostr_service=SimpleNamespace(to_pubkey_hex=forbidden))
        # Parse the actual stored preference fields; no push service or database import is needed.
        load('app.services.push_prefs', 'app/services/push_prefs.py')
        reminder = load('app.services.reminder_service', 'app/services/reminder_service.py')
        auth_router = load('app.routers.auth', 'app/routers/auth.py')
        yield SimpleNamespace(models=models, service=reminder, router=auth_router.router,
                              get_db=forbidden, get_current_user=unauthorized, settings=settings)
