import logging
import asyncio
import os
import re
import datetime
import csv
from zoneinfo import ZoneInfo
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
from psycopg2 import pool
import dateparser
from dateparser.search import search_dates
import speech_recognition as sr
from pydub import AudioSegment
import pytesseract
from PIL import Image
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

import spacy
try:
    nlp_spacy = spacy.load("en_core_web_md")
except Exception as e:
    logging.warning(f"No spaCy: {e}. Fallback heuristic NLP will be used.")
    nlp_spacy = None

# ==========================================
# 1. CONFIGURATION, SECURITY & MESSAGES
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MY_TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", 0))
ALLOWED_USERS = [MY_TELEGRAM_ID]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

T_DESCRIPTION, T_TYPE, T_PRIORITY, T_DATE, T_NOTES = range(5)
F_AREA, F_CONCEPT, F_AMOUNT, F_METHOD, F_DATE = range(5, 10)
E_FIELD, E_VALUE = range(10, 12)
H_NAME = 12
S_CONCEPT, S_AMOUNT, S_DATE = range(13, 16)

MSG_SUCCESS_TASK = "✅ *{cat_op} Registered*\n📌 {desc}\n⏰ Date: {date}\n📂 Area: {area}"
MSG_SUCCESS_FINANCE = "💳 *Financial Record*\n🏷️ {concept}\n💲 ${amount:,.2f}\n📊 Cat: {cat} | Account: {account}\n📅 Date: {date} ({status}){alert}"
MSG_ERROR = "⚠️ An error occurred. Try sending text or use the manual menu."
MSG_AUDIO_LONG = "⚠️ Audio is too long. Maximum 60 seconds due to memory limits."

# ==========================================
# 2. GHOST SERVER (HEALTH CHECK)
# ==========================================
class GhostServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok" if self.path == "/health" else b"Executive Bot V28 (Ultimate Merged) Active!")

def start_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(('0.0.0.0', port), GhostServer).serve_forever()

# ==========================================
# 3. DATABASE & PERSISTENCE
# ==========================================
db_pool = None
CACHE_DICTIONARY = {'BUSINESS': {}, 'PERSONAL': {}}
CACHE_ACCOUNTS = {}
CACHE_CATEGORIES = {}

def init_db_pool():
    global db_pool
    if DATABASE_URL: db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def execute_sql(query, params=None, fetch_all=False, fetch_one=False, commit=True):
    if not db_pool: return None
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

def init_db():
    tables = [
        "tasks (id SERIAL PRIMARY KEY, description TEXT, type TEXT, priority TEXT, original_date TEXT, calculated_date TEXT, notes TEXT, status TEXT DEFAULT 'Pending', completed_date TEXT, operational_category TEXT DEFAULT 'TASK')",
        "finances (id SERIAL PRIMARY KEY, type TEXT, area TEXT, concept TEXT, amount REAL, method TEXT, date TEXT, category TEXT DEFAULT 'General', subcategory TEXT DEFAULT 'General', account TEXT DEFAULT 'Cash', status TEXT DEFAULT 'Completed', is_reminder BOOLEAN DEFAULT FALSE)",
        "dictionary (word TEXT PRIMARY KEY, category TEXT)",
        "history (id SERIAL PRIMARY KEY, date TEXT, action TEXT, detail TEXT)",
        "operation_history (id SERIAL PRIMARY KEY, table_name TEXT, record_id INTEGER, action TEXT, date TEXT)",
        "config_accounts (name TEXT PRIMARY KEY, balance REAL DEFAULT 0)",
        "config_categories (name TEXT PRIMARY KEY, type TEXT, keywords TEXT DEFAULT '')",
        "budgets (category TEXT PRIMARY KEY, limit_amount REAL)",
        "errors (id SERIAL PRIMARY KEY, date TEXT, module TEXT, error TEXT)",
        "habits (id SERIAL PRIMARY KEY, name TEXT, streak INTEGER DEFAULT 0, last_date TEXT)",
        "habit_history (id SERIAL PRIMARY KEY, habit_id INTEGER, date TEXT)"
    ]
    for table in tables: execute_sql(f"CREATE TABLE IF NOT EXISTS {table}")
    
    for idx in ["idx_tasks_date ON tasks(calculated_date)", "idx_tasks_status ON tasks(status)", "idx_finances_date ON finances(date)", "idx_finances_status ON finances(status)", "idx_finances_reminder ON finances(is_reminder)"]:
        execute_sql(f"CREATE INDEX IF NOT EXISTS {idx}")
    
    if not execute_sql("SELECT 1 FROM config_accounts LIMIT 1", fetch_one=True):
        for c in ['Nu', 'Chase', 'Amex', 'Cash']: execute_sql("INSERT INTO config_accounts (name, balance) VALUES (%s, 0)", (c,))
    if not execute_sql("SELECT 1 FROM config_categories LIMIT 1", fetch_one=True):
        cats = [
            ('Utilities', 'Expense', 'LIGHT,WATER,INTERNET,PHONE,RENT,GAS'),
            ('Transport', 'Expense', 'UBER,TAXI,GAS,FLIGHT,TOLL,PARKING'),
            ('Food', 'Expense', 'GROCERIES,SUPERMARKET,RESTAURANT,DINNER,LUNCH,WALMART'),
            ('Payroll', 'Income', 'SALARY,PAYCHECK,INCOME,PAYMENT')
        ]
        for c, t, k in cats: execute_sql("INSERT INTO config_categories (name, type, keywords) VALUES (%s, %s, %s)", (c, t, k))
    reload_cache()

def reload_cache():
    global CACHE_DICTIONARY, CACHE_ACCOUNTS, CACHE_CATEGORIES
    dict_rows = execute_sql("SELECT word, category FROM dictionary", fetch_all=True) or []
    CACHE_DICTIONARY['BUSINESS'] = {f[0].upper(): 3 for f in dict_rows if f[1] == 'Business'}
    CACHE_DICTIONARY['PERSONAL'] = {f[0].upper(): 3 for f in dict_rows if f[1] == 'Personal'}
    
    acc_rows = execute_sql("SELECT name FROM config_accounts", fetch_all=True) or []
    CACHE_ACCOUNTS.clear()
    for f in acc_rows: CACHE_ACCOUNTS[f[0].upper()] = f[0]
    
    cat_rows = execute_sql("SELECT name, type, keywords FROM config_categories", fetch_all=True) or []
    CACHE_CATEGORIES.clear()
    for name, typ, keywords in cat_rows:
        CACHE_CATEGORIES[name.upper()] = name
        if keywords:
            for k in keywords.split(','): CACHE_CATEGORIES[k.strip().upper()] = name

def sync_db_balances():
    execute_sql("UPDATE config_accounts SET balance = 0")
    movements = execute_sql("SELECT type, amount, account FROM finances WHERE status='Completed' AND is_reminder = FALSE", fetch_all=True) or []
    for typ, amount, account in movements:
        operator = "+" if typ == "Income" else "-"
        execute_sql(f"UPDATE config_accounts SET balance = balance {operator} %s WHERE LOWER(name) = LOWER(%s)", (amount, account))

def log_operation(table, record_id, action):
    now_date = datetime.datetime.now(ZoneInfo('America/Monterrey')).isoformat()
    execute_sql("INSERT INTO operation_history (table_name, record_id, action, date) VALUES (%s, %s, %s, %s)", (table, record_id, action, now_date))

def log_audit(action, detail):
    now_date = datetime.datetime.now(ZoneInfo('America/Monterrey')).strftime('%Y-%m-%d %H:%M:%S')
    execute_sql("INSERT INTO history (date, action, detail) VALUES (%s, %s, %s)", (now_date, action, detail))

def log_error(module, error_msg):
    now_date = datetime.datetime.now(ZoneInfo('America/Monterrey')).strftime('%Y-%m-%d %H:%M:%S')
    execute_sql("INSERT INTO errors (date, module, error) VALUES (%s, %s, %s)", (now_date, module, error_msg))
    execute_sql("DELETE FROM errors WHERE id IN (SELECT id FROM errors ORDER BY id DESC OFFSET 5000)")

# ==========================================
# 4. COMMANDS, BACKUP, UNDO & UTILS
# ==========================================
async def generate_and_send_backup(context: ContextTypes.DEFAULT_TYPE):
    conn, c, files = None, None, []
    try:
        conn = get_db_connection()
        c = conn.cursor()
        for table in ['finances', 'tasks', 'budgets', 'errors', 'habits', 'habit_history']:
            c.execute(f"SELECT * FROM {table}")
            columns = [desc[0] for desc in c.description]
            data = c.fetchall()
            temp_path = f"/tmp/backup_{table}.csv"
            with open(temp_path, mode='w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                writer.writerows(data)
            files.append(temp_path)
    except Exception as e:
        log_error("BACKUP", str(e))
    finally:
        if c: c.close()
        if conn: db_pool.putconn(conn)
    
    if files:
        await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text="📦 *Automatic Backup Generated:*", parse_mode='Markdown')
        for file_path in files:
            with open(file_path, 'rb') as f: await context.bot.send_document(chat_id=MY_TELEGRAM_ID, document=f)
            os.remove(file_path)

async def command_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("⏳ Generating report and backups...")
    await generate_and_send_backup(context)
    try: await m.delete()
    except Exception: pass

async def command_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    last_op = execute_sql("SELECT id, table_name, record_id, action FROM operation_history ORDER BY id DESC LIMIT 1", fetch_one=True)
    if not last_op:
        return await update.message.reply_text("⚠️ No recent operations to undo.")
    
    hist_id, table, reg_id, action = last_op
    
    if table == 'finances':
        info = execute_sql("SELECT type, amount, account, status, is_reminder, concept FROM finances WHERE id = %s", (reg_id,), fetch_one=True)
        if info:
            if action == 'INSERT':
                execute_sql("DELETE FROM finances WHERE id = %s", (reg_id,))
                sync_db_balances()
                await update.message.reply_text(f"✅ Deleted financial record: '{info[5]}'. Balances reverted.")
            elif action == 'PAYMENT_MADE':
                execute_sql("UPDATE finances SET status = 'Pending' WHERE id = %s", (reg_id,))
                sync_db_balances()
                await update.message.reply_text(f"✅ Payment '{info[5]}' reverted to Pending status. Balance reverted.")
    else:
        info = execute_sql("SELECT description FROM tasks WHERE id = %s", (reg_id,), fetch_one=True)
        if action == 'INSERT':
            execute_sql("DELETE FROM tasks WHERE id = %s", (reg_id,))
            if info: await update.message.reply_text(f"✅ Deleted task/routine: '{info[0]}'.")
        
    execute_sql("DELETE FROM operation_history WHERE id = %s", (hist_id,))

async def command_recalculate_balances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    sync_db_balances()
    await update.message.reply_text("✅ Balances recalculated correctly from the history of real movements.")

async def command_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    name = " ".join(context.args)
    if name:
        execute_sql("INSERT INTO config_accounts (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))
        reload_cache()
        await update.message.reply_text(f"✅ Account or payment method '{name}' added successfully.")

async def command_set_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    try:
        category = context.args[0]
        limit = float(context.args[1])
        execute_sql("INSERT INTO budgets (category, limit_amount) VALUES (%s, %s) ON CONFLICT (category) DO UPDATE SET limit_amount = EXCLUDED.limit_amount", (category.capitalize(), limit))
        await update.message.reply_text(f"✅ Budget of ${limit:,.2f} set for '{category}'.")
    except Exception as e:
        log_error("BUDGET", str(e))
        await update.message.reply_text("⚠️ Correct usage: `/budget Food 5000`", parse_mode='Markdown')

async def command_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    today = datetime.date.today().strftime('%Y-%m-%d')
    curr_month = datetime.datetime.now().strftime('%Y-%m')
    
    tasks = execute_sql("SELECT description, priority FROM tasks WHERE calculated_date <= %s AND status = 'Pending'", (today,), fetch_all=True) or []
    payments = execute_sql("SELECT concept, amount, date, is_reminder FROM finances WHERE date <= %s AND status = 'Pending'", (today,), fetch_all=True) or []
    fins = execute_sql("SELECT type, amount FROM finances WHERE date LIKE %s AND status = 'Completed' AND is_reminder = FALSE", (f"{curr_month}-%",), fetch_all=True) or []
    accounts = execute_sql("SELECT name, balance FROM config_accounts WHERE balance != 0", fetch_all=True) or []
    
    inc = sum(f[1] for f in fins if f[0] == 'Income')
    exp = sum(f[1] for f in fins if f[0] == 'Expense')
    total_balance = sum(c[1] for c in accounts)
    urgents = sum(1 for t in tasks if t[1] == 'Urgent')

    msg = f"📊 *EXECUTIVE DASHBOARD ({today})*\n\n"
    msg += f"💰 *Total Balance:* ${total_balance:,.2f}\n"
    msg += f"📈 *Income this month:* ${inc:,.2f}\n📉 *Expenses this month:* ${exp:,.2f}\n⚖️ *Monthly Balance:* ${(inc - exp):,.2f}\n\n"
    msg += f"📝 *Pending today:* {len(tasks)}\n⚠️ *Urgent:* {urgents}\n"
    
    if payments:
        msg += "\n🚨 *Upcoming/Overdue payments:*\n" + "".join([f"   {'🔴' if p[2] < today else '🔔'} {p[0]} - {'(Reminder)' if p[3] else f'${p[1]:,.2f}'}\n" for p in payments[:3]])
            
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_menu())

# ==========================================
# 5. SMART NLP & EXTRACTION (spaCy)
# ==========================================
WORDS_BUSINESS_BASE = {'PERDIEM': 3, 'FLIGHT': 2, 'GAS': 2, 'UBER': 2, 'CLIENT': 3, 'BUSINESS': 3, 'PROVIDER': 3, 'TAX': 3}
WORDS_PERSONAL_BASE = {'GROCERIES': 3, 'LIGHT': 3, 'WATER': 3, 'INTERNET': 3, 'RENT': 3, 'HOUSE': 2, 'DOCTOR': 2, 'PERSONAL': 3, 'CLOTHES': 2}
WORDS_EXPENSE = ['PAID', 'BOUGHT', 'SPENT', 'COST', 'PAY', 'TICKET', 'RECEIPT']
WORDS_INCOME = ['DEPOSITED', 'TRANSFERRED', 'EARNED', 'SALARY', 'INCOME', 'RECEIVED']
STOPWORDS = WORDS_EXPENSE + WORDS_INCOME + ['HOW', 'MUCH', 'WHAT', 'TELL', 'ME', 'HAVE', 'AND', 'ARE', 'IS', 'AM', 'WILL', 'DO', 'THE', 'A', 'AN', 'TOMORROW', 'TODAY', 'YESTERDAY', 'MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY', 'IN', 'FOR', 'OF', 'TO', 'BY', 'WITH', 'DOLLARS', 'USD', 'THIS', 'EVERY', 'ALL']

def extract_spacy_entities(text):
    if not nlp_spacy: return None, None
    doc = nlp_spacy(text)
    verbs = [token.lemma_.upper() for token in doc if token.pos_ == "VERB"]
    objects = [token.text for token in doc if token.dep_ in ('dobj', 'nsubj', 'pobj', 'compound') and not token.is_stop]
    clean_obj = " ".join(objects).title() if objects else None
    return verbs, clean_obj

def get_learning_word(text):
    if not text: return None
    verbs, spacy_obj = extract_spacy_entities(text)
    if spacy_obj: return spacy_obj.split()[0].upper()
    
    invalid_words = {'DAY','TODAY','YESTERDAY','TOMORROW','PAID','BOUGHT','SPENT','INVOICE','PURCHASE','DEPOSIT','TRANSFERRED','RECORD','GENERAL'}
    words = re.findall(r'\b[A-Z]+\b', text.upper())
    words = [w for w in words if len(w) >= 3 and w not in invalid_words]
    return words[0] if words else None

def process_financial_impact(amount, typ, account_name, subcat, status):
    if status != 'Completed' or amount == 0.0: return ""
    sync_db_balances()
    if typ == "Expense":
        limit = execute_sql("SELECT limit_amount FROM budgets WHERE LOWER(category) = %s", (subcat.lower(),), fetch_one=True)
        if limit:
            curr_month = datetime.datetime.now().strftime('%Y-%m')
            spent = execute_sql("SELECT SUM(amount) FROM finances WHERE subcategory = %s AND date LIKE %s AND status = 'Completed' AND is_reminder = FALSE", (subcat, f"{curr_month}-%"), fetch_one=True)[0] or 0
            if spent >= limit[0] * 0.85:
                return f"\n⚠️ *Budget at {(spent / limit[0]) * 100:.0f}%!* You've spent ${spent:,.0f} of ${limit[0]:,.0f} in {subcat}."
    return ""

def extract_date(text):
    dates = search_dates(text, languages=['en'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    return dates[0][1].strftime('%Y-%m-%d') if dates else None

def calculate_next_routine(text, current_date_str):
    curr_date = datetime.datetime.strptime(current_date_str, "%Y-%m-%d")
    text_upper = text.upper()
    days = {'MONDAY':0, 'TUESDAY':1, 'WEDNESDAY':2, 'THURSDAY':3, 'FRIDAY':4, 'SATURDAY':5, 'SUNDAY':6}
    for day_str, day_idx in days.items():
        if day_str in text_upper:
            days_ahead = (day_idx - curr_date.weekday() + 7) % 7
            return (curr_date + datetime.timedelta(days=days_ahead if days_ahead != 0 else 7)).strftime('%Y-%m-%d')
    if 'WEEK' in text_upper: return (curr_date + datetime.timedelta(days=7)).strftime('%Y-%m-%d')
    elif 'MONTH' in text_upper: return (curr_date + datetime.timedelta(days=30)).strftime('%Y-%m-%d')
    else: return (curr_date + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

def answer_smart_query(text_min):
    resp = "🤖 *Analytical Response:*\n\n"
    curr_year, curr_month = datetime.datetime.now().strftime('%Y'), datetime.datetime.now().strftime('%Y-%m')
    f_date, f_str = "", "historically"
    if "this year" in text_min: f_date, f_str = f"AND date LIKE '{curr_year}-%'", "this year"
    elif "this month" in text_min: f_date, f_str = f"AND date LIKE '{curr_month}-%'", "this month"
    
    if "money" in text_min or "balance" in text_min or "accounts" in text_min:
        res = execute_sql("SELECT name, balance FROM config_accounts ORDER BY balance DESC", fetch_all=True)
        return resp + f"💰 *Current Balances:*\n" + "".join([f"▫️ {r[0]}: ${r[1]:,.2f}\n" for r in res])
    elif "spent on" in text_min:
        word = re.sub(r'(this year|this month|how much)', '', text_min.split("on ")[-1]).strip().replace("?", "")
        res = execute_sql(f"SELECT SUM(amount) FROM finances WHERE type = 'Expense' AND status = 'Completed' AND is_reminder = FALSE {f_date} AND (LOWER(concept) LIKE %s OR LOWER(subcategory) LIKE %s)", (f"%{word}%", f"%{word}%"), fetch_one=True)
        return resp + f"You have spent **${(res[0] or 0):,.2f}** on '{word.capitalize()}' {f_str}."
    return False

def analyze_local_text(text):
    text_upper, text_min = text.upper(), text.lower()
    
    if any(q in text_min for q in ["how much", "what", "tell me", "show me", "which"]):
        resp = answer_smart_query(text_min)
        if resp: return {"record_type": "query", "response": resp}

    dict_biz, dict_per = {**WORDS_BUSINESS_BASE, **CACHE_DICTIONARY.get('BUSINESS', {})}, {**WORDS_PERSONAL_BASE, **CACHE_DICTIONARY.get('PERSONAL', {})}
    score_biz, score_per = sum(p for w, p in dict_biz.items() if w in text_upper), sum(p for w, p in dict_per.items() if w in text_upper)
    area = "Unknown" if score_biz == 0 and score_per == 0 else ("Business" if score_biz > score_per else "Personal")
    
    is_expense = any(p in text_upper for p in WORDS_EXPENSE)
    is_income = any(p in text_upper for p in WORDS_INCOME)
    match_amount = re.search(r'\$?\s*([\d,]+(?:\.\d{1,2})?)\b', text_upper)
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    
    spacy_verbs, spacy_obj = extract_spacy_entities(text)
    clean_word = re.sub(r'\$?\s*([\d,]+(?:\.\d{1,2})?)\b', '', text_upper).strip()
    clean_word = re.sub(r'[^\w\s]', ' ', clean_word)
    for w in STOPWORDS + list(CACHE_ACCOUNTS.keys()): clean_word = re.sub(rf'\b{w}\b', '', clean_word)
    clean_word = clean_word.strip().title()
    final_concept = spacy_obj if spacy_obj else (clean_word or "General record")
    
    if is_expense or is_income or match_amount or (spacy_verbs and any(v in ['PAY', 'BUY', 'DEPOSIT', 'CHARGE', 'SPEND'] for v in spacy_verbs)):
        fin_type = "Income" if is_income and not is_expense else "Expense"
        amount = float(match_amount.group(1).replace(',', '')) if match_amount else 0.0
        calc_date = extract_date(text) or today_str
        
        f_status = 'Pending' if (calc_date > today_str or amount == 0.0) else 'Completed'
        detected_acc = 'Cash'
        for c_key, c_orig in CACHE_ACCOUNTS.items(): 
            if c_key in text_upper: detected_acc = c_orig; break
            
        subcat = 'General'
        for c_key, c_orig in CACHE_CATEGORIES.items():
            if c_key in text_upper: subcat = c_orig; break
        
        return {"record_type": "finance", "type": fin_type, "area": area, "concept": final_concept[:40], "amount": amount, "method": detected_acc, "date": calc_date, "status": f_status, "subcategory": subcat, "is_reminder": (amount == 0.0), "keyword": clean_word}
    
    is_routine = bool(re.search(r'(EVERY|DAILY|ALWAYS)', text_upper))
    return {"record_type": "task", "description": text[:60].capitalize(), "type": area, "priority": "Urgent" if "URGENT" in text_upper else "Routine", "original_date": text, "calculated_date": extract_date(text) or today_str, "operational_category": "ROUTINE" if is_routine else "TASK", "keyword": clean_word}

# ==========================================
# 6. DATA INGESTION & FLOW
# ==========================================
async def save_nlp_data(bot, chat_id, data, context=None):
    if data.get('record_type') == 'query':
        await bot.send_message(chat_id=chat_id, text=data['response'], parse_mode='Markdown')
        return

    if data.get('amount', 0) > 10000 and not data.get('confirmed'):
        data['original_record_type'] = data['record_type']
        data['record_type'] = 'high_amount_confirm'
        if context: context.user_data['pending_high_amount'] = data
        await bot.send_message(chat_id=chat_id, text=f"⚠️ *Attention:* Transaction for **${data['amount']:,.2f}**. Confirm?", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Confirm ${data['amount']:,.2f}", callback_data="confirm_high")]]))
        return

    if data['record_type'] == 'task':
        res = execute_sql("INSERT INTO tasks (description, type, priority, original_date, calculated_date, operational_category) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                  (data['description'], data['type'], data['priority'], data['original_date'], data['calculated_date'], data['operational_category']), fetch_one=True)
        if res: log_operation('tasks', res[0], 'INSERT')
        msg = MSG_SUCCESS_TASK.format(cat_op=data['operational_category'], desc=data['description'], date=data['calculated_date'], area=data['type'])
        
    elif data['record_type'] == 'finance':
        f_status, subcat, acc = data.get('status', 'Completed'), data.get('subcategory', 'General'), data.get('method', 'Cash')
        is_rem_fin = data.get('is_reminder', False)
        res = execute_sql("INSERT INTO finances (type, area, concept, amount, method, date, category, status, subcategory, account, is_reminder) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                  (data['type'], data['area'], data['concept'], data['amount'], acc, data['date'], 'General', f_status, subcat, acc, is_rem_fin), fetch_one=True)
        if res: log_operation('finances', res[0], 'INSERT')
        alert_budget = process_financial_impact(data['amount'], data['type'], acc, subcat, f_status)
        emoji = "⏳ Pending" if f_status == 'Pending' else "✅ Completed"
        msg = MSG_SUCCESS_FINANCE.format(concept=data['concept'], amount=data['amount'], cat=subcat, account=acc, date=data['date'], status=emoji, alert=alert_budget)
        if is_rem_fin: msg = msg.replace("💲 $0.00", "🔔 (Financial Reminder)")
        
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown', reply_markup=get_main_menu())

async def handle_analysis_result(update, context, data):
    if data.get('area') == "Unknown":
        context.user_data['pending_classification'] = data
        text_learn = data.get('keyword') or data.get('concept') or data.get('description')
        word = get_learning_word(text_learn)
        if not word:
            data['area' if data.get('record_type') == 'finance' else 'type'] = 'Personal'
            await save_nlp_data(context.bot, MY_TELEGRAM_ID, data, context)
            return
        keyboard = [[InlineKeyboardButton("🏢 Business", callback_data='learn_business')], [InlineKeyboardButton("👤 Personal", callback_data='learn_personal')]]
        await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"🤔 I don't know if *'{word.title()}'* is Business or Personal.\nClassify it:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else: await save_nlp_data(context.bot, MY_TELEGRAM_ID, data, context)

async def process_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("🧠 *Processing...*", parse_mode='Markdown')
    await handle_analysis_result(update, context, analyze_local_text(update.message.text))
    try: await m.delete()
    except Exception: pass

async def process_local_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    if update.message.voice.duration > 60:
        await update.message.reply_text(MSG_AUDIO_LONG); return
    m = await update.message.reply_text("🎙️ *Transcribing...*", parse_mode='Markdown')
    temp_audio_path = wav_path = ""
    try:
        file = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio: temp_audio_path = temp_audio.name
        await file.download_to_drive(temp_audio_path)
        wav_path = temp_audio_path.replace(".ogg", ".wav")
        AudioSegment.from_ogg(temp_audio_path).export(wav_path, format="wav")
        with sr.AudioFile(wav_path) as source: text = sr.Recognizer().recognize_google(sr.Recognizer().record(source), language="en-US")
        if len(text.strip()) < 3: raise Exception("Empty audio")
        await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"🗣️ _\"{text}\"_", parse_mode='Markdown')
        await handle_analysis_result(update, context, analyze_local_text(text))
    except Exception as e:
        log_error("AUDIO", str(e))
        await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=MSG_ERROR)
    finally: 
        if temp_audio_path and os.path.exists(temp_audio_path): os.remove(temp_audio_path)
        if wav_path and os.path.exists(wav_path): os.remove(wav_path)
        try: await m.delete()
        except Exception: pass

async def process_local_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("📸 *Scanning OCR...*", parse_mode='Markdown')
    temp_photo_path = ""
    try:
        file = await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_photo: temp_photo_path = temp_photo.name
        await file.download_to_drive(temp_photo_path)
        img = Image.open(temp_photo_path).convert('L')
        img.thumbnail((1200, 1200)) 
        raw_text = pytesseract.image_to_string(img, lang='eng')
        match = re.search(r'(?:TOTAL|AMOUNT|NET|SUM)[\s\$:]*([\d,]+\.\d{2})', raw_text, re.IGNORECASE)
        if not match: raise Exception("Safe amount not detected.")
        await handle_analysis_result(update, context, analyze_local_text(f"receipt paid {match.group(1)} {raw_text[:300]}"))
    except Exception as e: 
        log_error("OCR", str(e))
        await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"⚠️ OCR Failed: Register manually.")
    finally: 
        if temp_photo_path and os.path.exists(temp_photo_path): os.remove(temp_photo_path)
        try: await m.delete()
        except Exception: pass

# ==========================================
# 7. INTERACTIVE EDITING & HABITS
# ==========================================
async def start_editing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    query = update.callback_query
    await query.answer()
    
    if query.data == 'edit_records':
        fins = execute_sql("SELECT id, concept, amount FROM finances ORDER BY id DESC LIMIT 3", fetch_all=True) or []
        tks = execute_sql("SELECT id, description, calculated_date FROM tasks ORDER BY id DESC LIMIT 3", fetch_all=True) or []
        keyboard = [[InlineKeyboardButton(f"💸 {f[1][:15]} (${f[2]:.0f})", callback_data=f"edit_f_{f[0]}")] for f in fins]
        keyboard.extend([[InlineKeyboardButton(f"📝 {t[1][:15]} ({t[2]})", callback_data=f"edit_t_{t[0]}")] for t in tks])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='back_home')])
        await query.edit_message_text("Select the record to edit:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith('edit_f_'):
        context.user_data['edit_id'], context.user_data['edit_table'] = query.data.split('_')[2], 'finances'
        keyboard = [[InlineKeyboardButton("Amount", callback_data="edfield_amount"), InlineKeyboardButton("Concept", callback_data="edfield_concept")],
                   [InlineKeyboardButton("Date", callback_data="edfield_date"), InlineKeyboardButton("Account", callback_data="edfield_account")],
                   [InlineKeyboardButton("🔙 Back", callback_data='edit_records')]]
        await query.edit_message_text(f"Editing Finance #{context.user_data['edit_id']}. What to change?", reply_markup=InlineKeyboardMarkup(keyboard))
        return E_FIELD
        
    elif query.data.startswith('edit_t_'):
        context.user_data['edit_id'], context.user_data['edit_table'] = query.data.split('_')[2], 'tasks'
        keyboard = [[InlineKeyboardButton("Description", callback_data="edfield_description"), InlineKeyboardButton("Date", callback_data="edfield_calculated_date")],
                   [InlineKeyboardButton("Priority", callback_data="edfield_priority"), InlineKeyboardButton("Status", callback_data="edfield_status")],
                   [InlineKeyboardButton("🔙 Back", callback_data='edit_records')]]
        await query.edit_message_text(f"Editing Task #{context.user_data['edit_id']}. What to change?", reply_markup=InlineKeyboardMarkup(keyboard))
        return E_FIELD

async def capture_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['edit_field'] = query.data.split('_')[1]
    await query.edit_message_text(f"Type the new value for *{context.user_data['edit_field']}*:", parse_mode='Markdown')
    return E_VALUE

async def save_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_val = update.message.text
    table, field, reg_id = context.user_data.get('edit_table'), context.user_data.get('edit_field'), context.user_data.get('edit_id')
    
    if table not in ['finances', 'tasks']: return ConversationHandler.END
    if table == 'finances' and field not in ['amount', 'concept', 'date', 'account']: return ConversationHandler.END
    if table == 'tasks' and field not in ['description', 'calculated_date', 'priority', 'status']: return ConversationHandler.END
    
    try:
        if field == 'amount': new_val = float(new_val.replace('$', '').replace(',', ''))
        elif field in ['date', 'calculated_date']:
            f_calc = dateparser.parse(new_val, languages=['en'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
            if f_calc: new_val = f_calc.strftime('%Y-%m-%d')
            else: raise Exception("Invalid Date")
            
        execute_sql(f"UPDATE {table} SET {field} = %s WHERE id = %s", (new_val, reg_id))
        log_operation(table, reg_id, f'UPDATE_{field}')
        if table == 'finances': sync_db_balances()
        
        await update.message.reply_text(f"✅ Record #{reg_id} updated. {field} = {new_val}", reply_markup=get_main_menu())
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error saving: {e}", reply_markup=get_main_menu())
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# 8. MENUS
# ==========================================
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Tasks", callback_data='menu_task'), InlineKeyboardButton("💰 Finances", callback_data='menu_finance')],
        [InlineKeyboardButton("📅 Dynamic Agenda", callback_data='view_calendar'), InlineKeyboardButton("🧘 Habits", callback_data='menu_habit')],
        [InlineKeyboardButton("⏱️ Focus Mode", callback_data='focus_mode'), InlineKeyboardButton("✏️ Edit Recent", callback_data='edit_records')]
    ])

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS: return
    await query.answer() 

    if query.data == 'confirm_high':
        data = context.user_data.pop('pending_high_amount', None)
        if data:
            data['record_type'], data['confirmed'] = data['original_record_type'], True
            await save_nlp_data(context.bot, MY_TELEGRAM_ID, data, context)
            await query.edit_message_text("✅ High amount confirmed.")

    # --- HABITS MENU ---
    elif query.data == 'menu_habit':
        habits = execute_sql("SELECT id, name, streak, last_date FROM habits ORDER BY id ASC", fetch_all=True) or []
        keyboard = []
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        for h in habits:
            h_id, h_name, h_streak, h_last = h
            icon = "✅" if h_last == today_str else "⬜"
            keyboard.append([InlineKeyboardButton(f"{icon} {h_name} (Streak: {h_streak})", callback_data=f'check_habit_{h_id}')])
        keyboard.append([InlineKeyboardButton("📊 View Stats", callback_data='stats_habits')])
        keyboard.append([InlineKeyboardButton("➕ New Habit", callback_data='habit_new')])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='back_home')])
        await query.edit_message_text("🧘 *Habit Tracker:*\nTap a habit to mark it as completed today.", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith('check_habit_'):
        h_id = query.data.split('_')[2]
        res = execute_sql("SELECT streak, last_date, name FROM habits WHERE id = %s", (h_id,), fetch_one=True)
        msg = "Error reading habit."
        if res:
            streak, last_date, name = res
            today_date = datetime.date.today()
            today_str = today_date.strftime('%Y-%m-%d')
            yesterday_str = (today_date - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

            if last_date == today_str:
                msg = f"You already completed '{name}' today. Keep it up!"
            else:
                new_streak = streak + 1 if last_date == yesterday_str else 1
                execute_sql("UPDATE habits SET streak = %s, last_date = %s WHERE id = %s", (new_streak, today_str, h_id))
                execute_sql("INSERT INTO habit_history (habit_id, date) VALUES (%s, %s)", (h_id, today_str))
                msg = f"🎉 Habit '{name}' completed!\n🔥 Current streak: {new_streak} days."
        await query.edit_message_text(f"{msg}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Habits", callback_data='menu_habit')]]))

    elif query.data == 'stats_habits':
        today = datetime.date.today()
        monday = (today - datetime.timedelta(days=today.weekday())).strftime('%Y-%m-%d')
        sunday = (today + datetime.timedelta(days=6 - today.weekday())).strftime('%Y-%m-%d')
        curr_month = today.strftime('%Y-%m')
        
        habits = execute_sql("SELECT id, name FROM habits", fetch_all=True) or []
        msg = "📊 *Habit Statistics*\n\n"
        for h_id, h_name in habits:
            weekly = execute_sql("SELECT COUNT(*) FROM habit_history WHERE habit_id = %s AND date BETWEEN %s AND %s", (h_id, monday, sunday), fetch_one=True)[0]
            monthly = execute_sql("SELECT COUNT(*) FROM habit_history WHERE habit_id = %s AND date LIKE %s", (h_id, f"{curr_month}-%"), fetch_one=True)[0]
            msg += f"▫️ *{h_name}:*\n   This Week: {weekly} days\n   This Month: {monthly} days\n\n"
            
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Habits", callback_data='menu_habit')]]))

    # --- TASKS MENU ---
    elif query.data == 'menu_task':
        await query.edit_message_text("📝 *Agenda:*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Register Manual", callback_data='task_new')], [InlineKeyboardButton("✅ View Pending / Routines", callback_data='list_pending')], [InlineKeyboardButton("🔙 Back", callback_data='back_home')]]))
        
    elif query.data == 'list_pending':
        pending = execute_sql("SELECT id, description, operational_category FROM tasks WHERE status = 'Pending' ORDER BY calculated_date ASC LIMIT 8", fetch_all=True) or []
        if not pending: return await query.edit_message_text("🎉 Everything is up to date.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='menu_task')]]))
        keyboard = [[InlineKeyboardButton(f"{'🔁' if p[2]=='ROUTINE' else '✅'} {p[1][:25]}", callback_data=f'complete_t_{p[0]}')] for p in pending]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='menu_task')])
        await query.edit_message_text("Select to COMPLETE:", reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data.startswith('complete_t_'):
        t_id = query.data.split('_')[2]
        info = execute_sql("SELECT operational_category, original_date, calculated_date FROM tasks WHERE id = %s", (t_id,), fetch_one=True)
        if info:
            if info[0] == 'ROUTINE':
                next_date = calculate_next_routine(info[1], info[2])
                execute_sql("UPDATE tasks SET calculated_date = %s WHERE id = %s", (next_date, t_id))
                await query.edit_message_text(f"🔁 Routine completed. Next: {next_date}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data='back_home')]]))
            else:
                execute_sql("UPDATE tasks SET status = 'Completed', completed_date = %s WHERE id = %s", (datetime.date.today().strftime('%Y-%m-%d'), t_id))
                log_audit("COMPLETE_TASK", f"Task ID {t_id} completed")
                await query.edit_message_text("🎉 Task completed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data='back_home')]]))

    elif query.data == 'view_calendar':
        tasks = execute_sql("SELECT description, calculated_date, operational_category FROM tasks WHERE status = 'Pending'", fetch_all=True) or []
        next_7 = [datetime.date.today() + datetime.timedelta(days=i) for i in range(7)]
        days_eng = {0: 'Monday', 1: 'Tuesday', 2: 'Wednesday', 3: 'Thursday', 4: 'Friday', 5: 'Saturday', 6: 'Sunday'}
        msg = "📅 *AGENDA (Next 7 Days):*\n\n"
        for d in next_7:
            d_str = d.strftime('%Y-%m-%d')
            t_day = [t for t in tasks if t[1] == d_str]
            if t_day: msg += f"🗓️ *{days_eng[d.weekday()]}, {d_str}*\n" + "".join([f"   ├ ▫️ {t[0]}\n" for t in t_day]) + "\n"
        await query.edit_message_text(msg if "🗓️" in msg else "Your agenda is up to date.", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_home')]]))

    # --- FINANCES MENU ---
    elif query.data == 'menu_finance':
        keyboard = [
            [InlineKeyboardButton("💸 Manual Expense", callback_data='finance_expense'), InlineKeyboardButton("💵 Manual Income", callback_data='finance_income')],
            [InlineKeyboardButton("✅ Pay Pending", callback_data='list_pending_payments')],
            [InlineKeyboardButton("🧾 Pay Utility", callback_data='finance_utility')],
            [InlineKeyboardButton("📊 Historic Balance", callback_data='view_balance_all'), InlineKeyboardButton("📅 Current Month", callback_data='view_balance_month')],
            [InlineKeyboardButton("✏️ Edit / Delete", callback_data='edit_recent_f')],
            [InlineKeyboardButton("⬇️ Export CSV", callback_data='export_csv'), InlineKeyboardButton("🔙 Back", callback_data='back_home')]
        ]
        await query.edit_message_text("💰 *Finances:*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data == 'export_csv':
        await query.edit_message_text("⏳ Generating report...")
        await command_export(update, context)

    elif query.data == 'list_pending_payments':
        payments = execute_sql("SELECT id, concept, amount, date, is_reminder FROM finances WHERE status = 'Pending' ORDER BY date ASC LIMIT 8", fetch_all=True) or []
        if not payments: return await query.edit_message_text("🎉 No pending payments.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='menu_finance')]]))
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        keyboard = [[InlineKeyboardButton(f"{'🔴' if p[3] < today_str else '⏳'} {'Reminder' if p[4] else f'${p[2]:.0f}'}: {p[1][:12]}", callback_data=f'pay_f_{p[0]}')] for p in payments]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='menu_finance')])
        await query.edit_message_text("*(🔴 = Overdue)*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        
    elif query.data.startswith('pay_f_'):
        p_id = query.data.split('_')[2]
        info = execute_sql("SELECT amount, type, account, subcategory, status, is_reminder, concept FROM finances WHERE id = %s", (p_id,), fetch_one=True)
        if info and info[4] == 'Pending':
            if info[5]: 
                execute_sql("DELETE FROM finances WHERE id = %s", (p_id,))
                await query.edit_message_text(f"🔔 Reminder deleted.\nTo pay it, send me the exact amount.", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data='back_home')]]))
            else:
                execute_sql("UPDATE finances SET status = 'Completed', date = %s WHERE id = %s", (datetime.date.today().strftime('%Y-%m-%d'), p_id))
                log_operation('finances', p_id, 'PAYMENT_MADE')
                log_audit("PAYMENT_MADE", f"Finance ID {p_id} paid")
                alert = process_financial_impact(info[0], info[1], info[2], info[3], 'Completed')
                await query.edit_message_text(f"💸 Payment completed.{alert}", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data='back_home')]]))

    elif query.data.startswith('view_balance_'):
        filter_type = query.data.split('_')[2]
        today_str = datetime.date.today().strftime('%Y-%m-%d')
        if filter_type == 'month':
            curr_month = datetime.datetime.now().strftime('%Y-%m')
            records = execute_sql("SELECT type, area, amount, concept, date, status FROM finances WHERE date LIKE %s ORDER BY date ASC", (f"{curr_month}-%",), fetch_all=True) or []
            title = f"📊 *MONTHLY BALANCE ({curr_month})*\n\n"
        else:
            records = execute_sql("SELECT type, area, amount, concept, date, status FROM finances ORDER BY date ASC", fetch_all=True) or []
            title = "📊 *HISTORICAL BALANCE*\n\n"
            
        in_biz = out_biz = in_per = out_per = 0
        pending_list, exp_list = [], []
        
        for r in records:
            if r[5] == 'Pending': pending_list.append(r)
            else: 
                if r[0] == 'Income' and r[1] == 'Business': in_biz += r[2]
                elif r[0] == 'Expense' and r[1] == 'Business': out_biz += r[2]
                elif r[0] == 'Income' and r[1] == 'Personal': in_per += r[2]
                elif r[0] == 'Expense' and r[1] == 'Personal': out_per += r[2]
                if r[0] == 'Expense': exp_list.append(r)
                
        msg = title + f"🏢 *BUSINESS:*\n   ├ 📈 In: ${in_biz:,.2f} | 📉 Out: ${out_biz:,.2f}\n   └ ⚖️ Balance: ${(in_biz - out_biz):,.2f}\n\n"
        msg += f"👤 *PERSONAL:*\n   ├ 📈 In: ${in_per:,.2f} | 📉 Out: ${out_per:,.2f}\n   └ ⚖️ Balance: ${(in_per - out_per):,.2f}\n\n"
        
        acc_res = execute_sql("SELECT name, balance FROM config_accounts WHERE balance != 0", fetch_all=True) or []
        if acc_res: msg += "💰 *Current Balances:*\n" + "".join([f"   ▫️ {c[0]}: ${c[1]:,.2f}\n" for c in acc_res])
                
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='menu_finance')]]))

    elif query.data.startswith('learn_'):
        category = "Business" if query.data == 'learn_business' else "Personal"
        data = context.user_data.get('pending_classification')
        if data:
            word = get_learning_word(data.get('keyword') or data.get('concept') or data.get('description'))
            if word:
                execute_sql("INSERT INTO dictionary (word, category) VALUES (%s, %s) ON CONFLICT DO NOTHING", (word.upper(), category))
                reload_cache()
                data['area' if data.get('record_type') == 'finance' else 'type'] = category
                await save_nlp_data(context.bot, MY_TELEGRAM_ID, data, context)
            else: await query.edit_message_text("⚠️ Invalid word.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_home')]]))
            context.user_data.pop('pending_classification', None)
            
    elif query.data == 'focus_mode':
        keyboard = [[InlineKeyboardButton("25m", callback_data='start_focus_25'), InlineKeyboardButton("45m", callback_data='start_focus_45'), InlineKeyboardButton("60m", callback_data='start_focus_60')], [InlineKeyboardButton("🔙 Back", callback_data='back_home')]]
        await query.edit_message_text("⏱️ *Focus Mode*\nChoose time:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    elif query.data.startswith('start_focus_'):
        minutes = int(query.data.split('_')[2])
        await query.edit_message_text(f"⏱️ *Focus active: {minutes} minutes.*\nGet to work!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ End", callback_data='cancel_focus')]]), parse_mode='Markdown')
        async def temp():
            await asyncio.sleep(minutes * 60)
            await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"🔔 *TIME UP ({minutes} min)!* Take a break.", parse_mode='Markdown')
        context.user_data['focus_task'] = asyncio.create_task(temp())
    elif query.data == 'cancel_focus':
        if 'focus_task' in context.user_data: context.user_data['focus_task'].cancel()
        await query.edit_message_text("⏹️ *Focus stopped.*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='back_home')]]))
    
    # --- DELETE & UI CLEANUP ---
    elif query.data == 'edit_recent_f':
        fins = execute_sql("SELECT id, concept, amount FROM finances ORDER BY id DESC LIMIT 5", fetch_all=True) or []
        keyboard = [[InlineKeyboardButton(f"❌ Del {f[1][:10]} (${f[2]:.0f})", callback_data=f"del_f_{f[0]}"), InlineKeyboardButton("✏️ Edit", callback_data=f"edit_f_{f[0]}")] for f in fins]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='menu_finance')])
        await query.edit_message_text("Select the action for the recent financial records:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith('del_f_'):
        del_id = query.data.split('_')[2]
        info = execute_sql("SELECT type, amount, account, status FROM finances WHERE id = %s", (del_id,), fetch_one=True)
        if info and info[3] == 'Completed':
            op = "-" if info[0] == "Income" else "+"
            execute_sql(f"UPDATE config_accounts SET balance = balance {op} %s WHERE LOWER(name) = %s", (info[1], info[2].lower()))
        execute_sql("DELETE FROM finances WHERE id = %s", (del_id,))
        await query.edit_message_text(f"✅ Financial record #{del_id} permanently deleted.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data='menu_finance')]]))
        
    elif query.data == 'back_home':
        await query.edit_message_text("Main Menu:", reply_markup=get_main_menu())

# ==========================================
# 9. MANUAL REGISTRATION
# ==========================================
async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled.", reply_markup=get_main_menu())
    return ConversationHandler.END

# ---- HABITS MANUAL ----
async def start_habit_registration(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    await u.callback_query.edit_message_text("🧘 *Type the name of the new habit*\n(e.g., 'Go to gym', 'Read 15 mins'):", parse_mode='Markdown')
    return H_NAME

async def capture_habit(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    name = u.message.text
    execute_sql("INSERT INTO habits (name, streak, last_date) VALUES (%s, 0, '')", (name,))
    await u.message.reply_text(f"✅ Habit '{name}' created. Good luck!", reply_markup=get_main_menu())
    return ConversationHandler.END

# ---- TASKS MANUAL ----
async def start_task_registration(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['new_task'] = {}
    await u.callback_query.edit_message_text("📝 *Description:*", parse_mode='Markdown')
    return T_DESCRIPTION
async def capture_description(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    c.user_data['new_task']['description'] = u.message.text
    await u.message.reply_text("📂 *Category:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Business", callback_data='typ_bus'), InlineKeyboardButton("Personal", callback_data='typ_per')]]), parse_mode='Markdown')
    return T_TYPE
async def capture_type(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['new_task']['type'] = "Business" if u.callback_query.data == 'typ_bus' else "Personal"
    await u.callback_query.edit_message_text("⚡ *Priority:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Urgent", callback_data='prio_urg'), InlineKeyboardButton("Routine", callback_data='prio_rout')]]), parse_mode='Markdown')
    return T_PRIORITY
async def capture_priority(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['new_task']['priority'] = "Urgent" if u.callback_query.data == 'prio_urg' else "Routine"
    await u.callback_query.edit_message_text("📅 *Date:*", parse_mode='Markdown')
    return T_DATE
async def capture_date(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    f_calc = dateparser.parse(u.message.text, languages=['en'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    c.user_data['new_task']['original_date'] = u.message.text
    c.user_data['new_task']['calculated_date'] = f_calc.strftime('%Y-%m-%d') if f_calc else "Recurring"
    await u.message.reply_text("🗒️ *Notes:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Skip", callback_data='no_notes')]]), parse_mode='Markdown')
    return T_NOTES
async def capture_notes(u: Update, cx: ContextTypes.DEFAULT_TYPE) -> int:
    task = cx.user_data['new_task']
    cat_op = "ROUTINE" if task['priority'] == "Routine" else "TASK"
    res = execute_sql("INSERT INTO tasks (description, type, priority, original_date, calculated_date, notes, status, operational_category) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
              (task['description'], task['type'], task['priority'], task['original_date'], task['calculated_date'], "None" if u.callback_query else u.message.text, 'Pending', cat_op), fetch_one=True)
    if res: log_operation('tasks', res[0], 'INSERT')
    msg = MSG_SUCCESS_TASK.format(cat_op=cat_op, desc=task['description'], date=task['calculated_date'], area=task['type'])
    await (u.callback_query.edit_message_text if u.callback_query else u.message.reply_text)(msg, parse_mode='Markdown', reply_markup=get_main_menu())
    return ConversationHandler.END

# ---- FINANCES MANUAL ----
async def start_finance(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['new_finance'] = {'type': "Expense" if u.callback_query.data == 'finance_expense' else "Income"}
    await u.callback_query.edit_message_text("💰 *Business or Personal?*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Business", callback_data='f_bus'), InlineKeyboardButton("Personal", callback_data='f_per')]]), parse_mode='Markdown')
    return F_AREA
async def capture_f_area(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['new_finance']['area'] = "Business" if u.callback_query.data == 'f_bus' else "Personal"
    await u.callback_query.edit_message_text("📝 *Concept:*", parse_mode='Markdown')
    return F_CONCEPT
async def capture_f_concept(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    c.user_data['new_finance']['concept'] = u.message.text
    await u.message.reply_text("💵 *Numeric amount:*", parse_mode='Markdown')
    return F_AMOUNT
async def capture_f_amount(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    try: c.user_data['new_finance']['amount'] = float(u.message.text)
    except ValueError:
        await u.message.reply_text("⚠️ Numbers only:")
        return F_AMOUNT 
    if c.user_data['new_finance']['type'] == "Expense":
        keyboard, accs = [], list(CACHE_ACCOUNTS.values())
        for i in range(0, len(accs), 2): keyboard.append([InlineKeyboardButton(acc, callback_data=f'f_acc_{acc}') for acc in accs[i:i+2]])
        await u.message.reply_text("💳 *Account:*", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return F_METHOD
    c.user_data['new_finance']['method'] = "N/A"
    await u.message.reply_text("📅 *Date:*", parse_mode='Markdown')
    return F_DATE
async def capture_f_method(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['new_finance']['method'] = u.callback_query.data.replace('f_acc_', '')
    await u.callback_query.edit_message_text("📅 *Date:*", parse_mode='Markdown')
    return F_DATE
async def capture_f_date(u: Update, cx: ContextTypes.DEFAULT_TYPE) -> int:
    f = cx.user_data['new_finance']
    fc = dateparser.parse(u.message.text, languages=['en'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    ff = fc.strftime('%Y-%m-%d') if fc else datetime.datetime.now().strftime('%Y-%m-%d')
    est = 'Pending' if ff > datetime.datetime.now().strftime('%Y-%m-%d') else 'Completed'
    res = execute_sql("INSERT INTO finances (type, area, concept, amount, method, date, category, status, subcategory, account, is_reminder) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                (f['type'], f['area'], f['concept'], f['amount'], f['method'], ff, 'General', est, 'General', f['method'], False), fetch_one=True)
    if res: log_operation('finances', res[0], 'INSERT')
    alert = process_financial_impact(f['amount'], f['type'], f['method'], 'General', est)
    msg = MSG_SUCCESS_FINANCE.format(concept=f['concept'].title(), amount=f['amount'], cat="General", account=f['method'], date=ff, status="⏳ Pending" if est == 'Pending' else "✅ Completed", alert=alert)
    await u.message.reply_text(msg, parse_mode='Markdown', reply_markup=get_main_menu())
    return ConversationHandler.END

# ---- UTILITIES MANUAL ----
async def start_utility(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['new_utility'] = {}
    await u.callback_query.edit_message_text("🧾 *Utilities / Services*\nType the concept (e.g. 'Electric bill', 'Internet'):", parse_mode='Markdown')
    return S_CONCEPT

async def capture_s_concept(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    c.user_data['new_utility']['concept'] = u.message.text
    await u.message.reply_text("💵 *Utility amount (number):*", parse_mode='Markdown')
    return S_AMOUNT

async def capture_s_amount(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    try: 
        c.user_data['new_utility']['amount'] = float(u.message.text.replace('$', '').replace(',', ''))
    except ValueError:
        await u.message.reply_text("⚠️ Numbers only.")
        return S_AMOUNT
    await u.message.reply_text("📅 *Date (e.g. 'today', 'tomorrow', 'Dec 15'):*", parse_mode='Markdown')
    return S_DATE

async def capture_s_date(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    srv = c.user_data['new_utility']
    fc = dateparser.parse(u.message.text, languages=['en'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    ff = fc.strftime('%Y-%m-%d') if fc else datetime.date.today().strftime('%Y-%m-%d')
    est = 'Pending' if ff > datetime.date.today().strftime('%Y-%m-%d') else 'Completed'
    
    execute_sql("INSERT INTO finances (type, area, concept, amount, method, date, category, status, subcategory, account, is_reminder) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                ('Expense', 'Unknown', srv['concept'], srv['amount'], 'Cash', ff, 'General', est, 'Utilities', 'Cash', False))
    if est == 'Completed':
        execute_sql("UPDATE config_accounts SET balance = balance - %s WHERE LOWER(name) = 'cash'", (srv['amount'],))
        
    await u.message.reply_text(f"🧾 *Utility Registered*\n🏷️ {srv['concept']}\n💲 ${srv['amount']:,.2f}\n📅 {ff} ({est})", parse_mode='Markdown', reply_markup=get_main_menu())
    return ConversationHandler.END

# ==========================================
# 10. ALERTS & SCHEDULING
# ==========================================
async def morning_alert(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.date.today()
    yesterday = (today - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    today_str = today.strftime('%Y-%m-%d')

    exp_yest = execute_sql("SELECT SUM(amount) FROM finances WHERE date = %s AND type = 'Expense' AND status = 'Completed'", (yesterday,), fetch_one=True)[0] or 0
    task_yest = execute_sql("SELECT COUNT(*) FROM tasks WHERE completed_date = %s", (yesterday,), fetch_one=True)[0] or 0
    tasks_today = execute_sql("SELECT description, type FROM tasks WHERE calculated_date <= %s AND status = 'Pending'", (today_str,), fetch_all=True) or []
    habits_today = execute_sql("SELECT name, last_date FROM habits", fetch_all=True) or []
    payments = execute_sql("SELECT concept, amount, date FROM finances WHERE date <= %s AND status = 'Pending'", (today_str,), fetch_all=True) or []

    msg = f"☀️ *Morning Executive Brief*\n\n"
    msg += f"📊 *Yesterday's Performance:*\n   ▫️ Tasks Completed: {task_yest}\n   ▫️ Total Spent: ${exp_yest:,.2f}\n\n"
    
    if tasks_today:
        msg += f"🎯 *Today's Agenda ({len(tasks_today)} pending):*\n"
        for t in tasks_today: msg += f"   ▫️ {t[0]}\n"
    else:
        msg += f"🎯 *Today's Agenda:*\n   ▫️ Clear, no pending tasks.\n"

    pending_habits = [h[0] for h in habits_today if h[1] != today_str]
    if pending_habits:
        msg += f"\n🧘 *Habits to complete today:*\n"
        for h in pending_habits: msg += f"   ▫️ {h}\n"

    if payments:
        msg += "\n🚨 *Due today:*\n" + "".join([f"   {'🔴' if p[2] < today_str else '🔔'} {p[0]} (${p[1]:,.2f})\n" for p in payments])

    await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=msg, parse_mode='Markdown', reply_markup=get_main_menu())

async def night_alert(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text="🌙 *End of day.* Check your pending items, complete your habits, and get some rest.", parse_mode='Markdown', reply_markup=get_main_menu())

async def weekly_alert(context: ContextTypes.DEFAULT_TYPE):
    past_7, today_str = (datetime.date.today() - datetime.timedelta(days=7)).strftime('%Y-%m-%d'), datetime.date.today().strftime('%Y-%m-%d')
    created = (execute_sql("SELECT COUNT(*) FROM tasks WHERE calculated_date BETWEEN %s AND %s", (past_7, today_str), fetch_one=True) or [0])[0]
    completed = (execute_sql("SELECT COUNT(*) FROM tasks WHERE completed_date BETWEEN %s AND %s", (past_7, today_str), fetch_one=True) or [0])[0]
    fins = execute_sql("SELECT type, amount FROM finances WHERE date BETWEEN %s AND %s AND status = 'Completed' AND is_reminder = FALSE", (past_7, today_str), fetch_all=True) or []
    inc, exp = sum(f[1] for f in fins if f[0] == 'Income'), sum(f[1] for f in fins if f[0] == 'Expense')
    msg = f"📅 *WEEKLY SUMMARY*\n\n📝 *Productivity:*\n   ├ New: {created}\n   └ ✅ Completed: {completed}\n\n💰 *Finances:*\n   ├ 📈 In: ${inc:,.2f} | 📉 Out: ${exp:,.2f}\n   └ ⚖️ Balance: ${(inc - exp):,.2f}\n"
    await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=msg, parse_mode='Markdown')
    await generate_and_send_backup(context)

# ==========================================
# 11. BOT STARTUP
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in ALLOWED_USERS: return
    await update.message.reply_text("Hello! Talk to me or use the menu:", reply_markup=get_main_menu())

if __name__ == '__main__':
    if not TOKEN or not DATABASE_URL or MY_TELEGRAM_ID == 0: exit(1)
        
    init_db_pool()
    init_db()
    
    threading.Thread(target=start_server, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()

    tz = ZoneInfo('America/Monterrey')
    app.job_queue.run_daily(morning_alert, time=datetime.time(hour=6, minute=0, tzinfo=tz))
    app.job_queue.run_daily(night_alert, time=datetime.time(hour=19, minute=0, tzinfo=tz))
    app.job_queue.run_daily(weekly_alert, time=datetime.time(hour=21, minute=0, tzinfo=tz), days=(6,))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("undo", command_undo))
    app.add_handler(CommandHandler("export", command_export))
    app.add_handler(CommandHandler("add_account", command_add_account))
    app.add_handler(CommandHandler("budget", command_set_budget))
    app.add_handler(CommandHandler("recalculate_balances", command_recalculate_balances))
    app.add_handler(CommandHandler("summary", command_summary))
    
    conv_edit = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_editing, pattern='^edit_')],
        states={E_FIELD: [CallbackQueryHandler(capture_edit_field, pattern='^edfield_')], E_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_edit)]},
        fallbacks=[CommandHandler('cancel', lambda u, c: ConversationHandler.END)]
    )
    
    conv_task = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_task_registration, pattern='^task_new$')],
        states={T_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_description)], T_TYPE: [CallbackQueryHandler(capture_type, pattern='^typ_')], T_PRIORITY: [CallbackQueryHandler(capture_priority, pattern='^prio_')], T_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_date)], T_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_notes), CallbackQueryHandler(capture_notes, pattern='^no_notes$')]},
        fallbacks=[CommandHandler('cancel', cancel_registration)], per_message=False
    )
    
    conv_finance = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_finance, pattern='^finance_expense$|^finance_income$')],
        states={F_AREA: [CallbackQueryHandler(capture_f_area, pattern='^f_')], F_CONCEPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_f_concept)], F_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_f_amount)], F_METHOD: [CallbackQueryHandler(capture_f_method, pattern='^f_acc_')], F_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_f_date)]},
        fallbacks=[CommandHandler('cancel', cancel_registration)], per_message=False
    )

    conv_habit = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_habit_registration, pattern='^habit_new$')],
        states={H_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_habit)]},
        fallbacks=[CommandHandler('cancel', cancel_registration)], per_message=False
    )

    conv_utility = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_utility, pattern='^finance_utility$')],
        states={
            S_CONCEPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_s_concept)],
            S_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_s_amount)],
            S_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, capture_s_date)]
        },
        fallbacks=[CommandHandler('cancel', cancel_registration)], per_message=False
    )

    app.add_handler(conv_edit)
    app.add_handler(conv_task)
    app.add_handler(conv_finance)
    app.add_handler(conv_habit)
    app.add_handler(conv_utility)
    app.add_handler(CallbackQueryHandler(start_editing, pattern='^edit_records$'))
    app.add_handler(CallbackQueryHandler(button_handler)) 
    
    app.add_handler(MessageHandler(filters.VOICE, process_local_audio))
    app.add_handler(MessageHandler(filters.PHOTO, process_local_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, process_free_text)) 

    print("🤖 Executive Bot V28 ENGLISH INITIATED: Full Production Ready with Utilities & Habits.")
    app.run_polling()
