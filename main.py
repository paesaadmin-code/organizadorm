import threading
import datetime
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
from config import TOKEN, DATABASE_URL, MY_TELEGRAM_ID, ALLOWED_USERS, LOCAL_TZ
from database import init_db_pool
from handlers import comandos, menus, tareas, finanzas, habitos, edicion
from services.nlp import init_spacy

def main():
    if not TOKEN or not DATABASE_URL or MY_TELEGRAM_ID == 0:
        print("Missing environment variables. Exiting.")
        return
        
    init_db_pool()
    init_spacy()
    
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Scheduled Jobs
    # app.job_queue.run_daily(morning_alert, time=datetime.time(hour=6, minute=0, tzinfo=LOCAL_TZ))
    
    # Handlers
    app.add_handler(CommandHandler("start", comandos.start))
    app.add_handler(CallbackQueryHandler(menus.button_handler))
    
    # Add Conversation Handlers for Tasks, Finances, Habits, Edits here
    # app.add_handler(tareas.conv_handler)
    # app.add_handler(finanzas.conv_handler)
    
    print("🤖 Modular Executive Bot INITIATED.")
    app.run_polling()

if __name__ == '__main__':
    main()
