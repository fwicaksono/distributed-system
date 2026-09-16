import os

# Injected at image build time (git SHA) so every log line and /health response says
# exactly which build is running — essential when a rollback is in progress.
__version__ = os.getenv("APP_VERSION", "dev")
