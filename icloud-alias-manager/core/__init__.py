from core.db import db
from core.logger import logger

# Connect logger to persist logs in SQLite database
logger.set_db_callback(db.add_log)
