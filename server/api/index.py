import os

from src.app.server import create_unified_app
from src.config import Config


# Vercel should serve the web/API application only.
# The persistent campaign worker remains outside Vercel.
os.environ.setdefault("WORKER_EMBEDDED", "false")

config = Config.from_env()

app = create_unified_app(
    config,
    engine=False,
)