import aiosqlite
from config import DB_NAME

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS accepted_scooters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scooter_number TEXT NOT NULL,
                service TEXT NOT NULL,
                accepted_by_user_id INTEGER NOT NULL,
                accepted_by_username TEXT,
                accepted_by_fullname TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                chat_id INTEGER NOT NULL,
                UNIQUE(scooter_number, accepted_by_user_id, timestamp)
            )
        ''')
        await db.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON accepted_scooters (timestamp);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_scooter ON accepted_scooters (scooter_number);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_service ON accepted_scooters (accepted_by_user_id, service);")
        await db.commit()

async def db_execute(query: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        cursor = await db.execute(query, params)
        await db.commit()
        return cursor.rowcount

async def db_fetch_all(query: str, params: tuple = ()) -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        cursor = await db.execute(query, params)
        return await cursor.fetchall()

async def db_write_batch(records_data: list[tuple]):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executemany('''
            INSERT OR IGNORE INTO accepted_scooters 
            (scooter_number, service, accepted_by_user_id, accepted_by_username, accepted_by_fullname, timestamp, chat_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', records_data)
        await db.commit()
