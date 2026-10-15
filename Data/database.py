import os

from sqlalchemy import create_engine

# The only place that decides which database is used.
# Set PLSIM_DB_URL to point at a server database instead of the local file.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plant.db")
DATABASE_URL = os.environ.get("PLSIM_DB_URL", f"sqlite:///{DB_PATH}")


def get_engine():
    return create_engine(DATABASE_URL)
