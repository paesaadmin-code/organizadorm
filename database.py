import psycopg2
from psycopg2 import pool
import logging
from config import DATABASE_URL

db_pool = None

def init_db_pool():
    global db_pool
    if DATABASE_URL:
        db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def execute_sql(query, params=None, fetch_all=False, fetch_one=False, commit=True):
    if not db_pool:
        logging.warning("Database pool not initialized.")
        return None
    conn = db_pool.getconn()
    res, c = None, None
    try:
        c = conn.cursor()
        c.execute(query, params or ())
        if fetch_all: res = c.fetchall()
        elif fetch_one: res = c.fetchone()
        if commit: conn.commit()
    except Exception as e:
        logging.error(f"SQL Error: {e}")
        if conn: conn.rollback()
    finally:
        if c: c.close()
        if conn: db_pool.putconn(conn)
    return res
