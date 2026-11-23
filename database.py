import psycopg2
from psycopg2 import pool
import logging
from config import DATABASE_URL

db_pool = None

def init_db_pool():
    global db_pool
    db_url = DATABASE_URL
    
    # 1. Corrección obligatoria de seguridad para el formato de URI en Render/Neon
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
        
    if db_url:
        try:
            db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, db_url)
        except Exception as e:
            logging.error(f"Error al inicializar el pool de BD: {e}")

def get_db_connection():
    if db_pool:
        return db_pool.getconn()
    return None

def execute_sql(query, params=None, fetch_all=False, fetch_one=False, commit=True):
    if not db_pool:
        logging.warning("Database pool not initialized.")
        return None
        
    conn, c, res = None, None, None
    try:
        # 2. Blindaje absoluto: Se solicita la conexión DENTRO del try
        conn = db_pool.getconn()
        c = conn.cursor()
        c.execute(query, params or ())
        
        if fetch_all: 
            res = c.fetchall()
        elif fetch_one: 
            res = c.fetchone()
            
        if commit: 
            conn.commit()
            
    except Exception as e:
        logging.error(f"SQL Error en query '{query}': {e}")
        if conn: 
            conn.rollback()
    finally:
        if c: 
            c.close()
        if conn: 
            db_pool.putconn(conn)
            
    return res
