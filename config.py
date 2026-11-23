import os
import ZoneInfo
import logging

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MY_TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", 0))
ALLOWED_USERS = [MY_TELEGRAM_ID]

LOCAL_TZ = ZoneInfo('America/Monterrey')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Conversation States
T_DESCRIPTION, T_TYPE, T_PRIORITY, T_DATE, T_NOTES = range(5)
F_AREA, F_CONCEPT, F_AMOUNT, F_METHOD, F_DATE = range(5, 10)
E_FIELD, E_VALUE = range(10, 12)
H_NAME = 12
S_CONCEPT, S_AMOUNT, S_DATE = range(13, 16)
