import os
import databases

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:password@localhost:5432/postgres"
)

database = databases.Database(DATABASE_URL)
