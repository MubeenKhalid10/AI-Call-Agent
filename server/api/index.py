from dotenv import load_dotenv

from src.app.server import create_unified_app
from src.config import Config

# Local development: load .env.
# On Vercel, environment variables come from Vercel.
load_dotenv(override=True)

config = Config.from_env()

# Vercel hosts the dashboard/API only.
# The persistent campaign/voice worker and outbox delivery
# remain outside Vercel.
app = create_unified_app(
    config,
    engine=False,
    deliver=False,
)