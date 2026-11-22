from telegram import Update\nfrom telegram.ext import ContextTypes\n\nasync def start(update: Update, context: ContextTypes.DEFAULT_TYPE):\n    await update.message.reply_text('Bot Started!')
