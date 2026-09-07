import os
import sys

from django.apps import AppConfig

# Commands that run a long-lived web server (model warm-up makes sense) vs.
# short-lived admin commands (migrate/check/shell...) where loading the ~90 MB
# embedding model would just waste time.
_SERVER_COMMANDS = {
    "runserver", "gunicorn", "uvicorn", "daphne", "hypercorn",
}


def _is_server_process() -> bool:
    """True when this process serves HTTP (dev runserver or a WSGI/ASGI server)."""
    args = sys.argv[1:]
    if args and args[0] in _SERVER_COMMANDS:
        return True
    return os.path.basename(sys.argv[0] or "") in _SERVER_COMMANDS


class MatchingEngineConfig(AppConfig):
    name = "matching_engine"

    def ready(self):
        # Warm the embedding model in the background at boot so the first
        # "Run AI matching" click never pays the model-load cost.
        if _is_server_process():
            from .services.scoring import warm_model
            warm_model()
