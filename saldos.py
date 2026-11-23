from database import execute_sql

def sync_db_balances():
    execute_sql("UPDATE config_accounts SET balance = 0")
    movements = execute_sql("SELECT type, amount, account FROM finances WHERE status='Completed' AND is_reminder = FALSE", fetch_all=True) or []
    for typ, amount, account in movements:
        operator = "+" if typ == "Income" else "-"
        execute_sql(f"UPDATE config_accounts SET balance = balance {operator} %s WHERE LOWER(name) = LOWER(%s)", (amount, account))
