import logging
import asyncio
import sqlite3
import datetime
from zoneinfo import ZoneInfo
import dateparser
import json
import os
import tempfile

from google import genai
from google.genai import types
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# --- CONFIGURACIÓN DE CLAVES ---
TOKEN = '9156918216:AAG4dC_F7iB3SGiq1IsXrevJAbFIHD0iLIo'
MI_TELEGRAM_ID = 6612216260
GEMINI_API_KEY = 'AQ.Ab8RN6K_dcsTxsYsc_y4yke47Kogk-ZcpTGHnBrncQLaC9FbrQ'

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

datos_usuario = {}
T_DESCRIPCION, T_TIPO, T_PRIORIDAD, T_FECHA, T_NOTAS = range(5)
F_AREA, F_CONCEPTO, F_MONTO, F_METODO, F_FECHA = range(5, 10)

# --- CONFIGURACIÓN DE IA (NUEVA VERSIÓN GENAI) ---
cliente_ia = genai.Client(api_key=GEMINI_API_KEY)

instruccion_sistema = """
Eres un asistente personal organizador inteligente. Analiza la entrada del usuario (texto, audio o ticket).
Toma en cuenta su "Planificador de tareas" para entender su contexto corporativo y personal.
Clasifica la entrada como 'tarea' o 'finanza' y devuelve ÚNICAMENTE un objeto JSON válido sin texto adicional ni formato markdown.
Formato Tarea: {"tipo_registro": "tarea", "descripcion": "string", "tipo": "Empresa" o "Personal", "prioridad": "Urgente" o "Rutina", "fecha_original": "string", "notas": "string"}
Formato Finanza: {"tipo_registro": "finanza", "tipo": "Gasto" o "Ingreso", "area": "Empresa" o "Personal", "concepto": "string", "monto": numero_float, "metodo": "Tarjeta" o "Efectivo", "fecha": "string"}
"""
config_ia = types.GenerateContentConfig(system_instruction=instruccion_sistema)

# ==========================================
# 1. BASE DE DATOS
# ==========================================
def init_db():
    conn = sqlite3.connect('mi_vida.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tareas (id INTEGER PRIMARY KEY AUTOINCREMENT, descripcion TEXT, tipo TEXT, prioridad TEXT, fecha_original TEXT, fecha_calculada DATE, notas TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finanzas (id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT, area TEXT, concepto TEXT, monto REAL, metodo TEXT, fecha DATE)''')
    conn.commit()
    conn.close()

init_db()

def obtener_menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Tareas (Menú)", callback_data='menu_tarea')],
        [InlineKeyboardButton("💰 Finanzas (Menú)", callback_data='menu_finanzas')],
        [InlineKeyboardButton("📅 Ver Calendario", callback_data='ver_calendario')],
        [InlineKeyboardButton("⏱️ Modo Enfoque", callback_data='modo_enfoque')]
    ])

# ==========================================
# 2. MOTOR DE ALERTAS AUTOMÁTICAS (NATIVO)
# ==========================================
async def alerta_matutina(context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.date.today().strftime('%Y-%m-%d')
    conn = sqlite3.connect('mi_vida.db')
    c = conn.cursor()
    c.execute("SELECT descripcion, tipo, prioridad FROM tareas WHERE fecha_calculada = ?", (hoy,))
    tareas_hoy = c.fetchall()
    conn.close()

    mensaje = "☀️ *Buenos días.* Aquí está tu lista para hoy:\n\n"
    if not tareas_hoy:
        mensaje += "No tienes actividades programadas. ¡Aprovecha el día!"
    else:
        for t in tareas_hoy:
            mensaje += f"▫️ {t[0]} ({t[1]} - {t[2]})\n"
        mensaje += "\n🤖 *Consejo de tu IA:* Enfócate en las tareas Urgentes primero para asegurar el cumplimiento del Planificador."

    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=mensaje, parse_mode='Markdown')

async def alerta_nocturna(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="🌙 *Corte de caja.* \nMomento de revisar pendientes y desconectar. ¿Cómo te fue hoy?", parse_mode='Markdown')

# ==========================================
# 3. PROCESAMIENTO DE IA (TEXTO, AUDIO Y FOTOS)
# ==========================================
async def procesar_json_ia(bot, chat_id, respuesta_ia):
    try:
        texto_json = respuesta_ia.replace('```json', '').replace('```', '').strip()
        datos = json.loads(texto_json)
        
        conn = sqlite3.connect('mi_vida.db')
        c = conn.cursor()
        
        if datos.get('tipo_registro') == 'tarea':
            fecha_calc = dateparser.parse(datos['fecha_original'], languages=['es'])
            fecha_str = fecha_calc.strftime('%Y-%m-%d') if fecha_calc else "Recurrente"
            c.execute("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, notas) VALUES (?, ?, ?, ?, ?, ?)",
                      (datos['descripcion'], datos['tipo'], datos['prioridad'], datos['fecha_original'], fecha_str, datos.get('notas', '')))
            mensaje = f"✅ *Tarea guardada con IA*\n📌 {datos['descripcion']}\n⏰ Para: {fecha_str} ({datos['tipo']})"
            
        elif datos.get('tipo_registro') == 'finanza':
            fecha_calc = dateparser.parse(datos['fecha'], languages=['es'])
            fecha_str = fecha_calc.strftime('%Y-%m-%d') if fecha_calc else datetime.date.today().strftime('%Y-%m-%d')
            c.execute("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha) VALUES (?, ?, ?, ?, ?, ?)",
                      (datos['tipo'], datos['area'], datos['concepto'], datos['monto'], datos['metodo'], fecha_str))
            mensaje = f"✅ *Finanza guardada con IA*\n💰 {datos['tipo']}: ${datos['monto']} en {datos['concepto']} ({datos['area']})"
        else:
            mensaje = "⚠️ La IA no pudo clasificar correctamente el mensaje."

        conn.commit()
        conn.close()
        await bot.send_message(chat_id=chat_id, text=mensaje, parse_mode='Markdown', reply_markup=obtener_menu_principal())

    except Exception as e:
        await bot.send_message(chat_id=chat_id, text=f"❌ Error al procesar con IA. Usa el menú manual.\nDetalle: {e}")

async def procesar_texto_libre_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_TELEGRAM_ID: return
    texto = update.message.text
    if datos_usuario.get('esperando_nombre'):
        datos_usuario['nombre'] = texto
        datos_usuario['esperando_nombre'] = False
        await update.message.reply_text(f"¡Listo, {texto}! Aquí tienes tu menú:", reply_markup=obtener_menu_principal())
        return

    m = await update.message.reply_text("🧠 *Pensando...*", parse_mode='Markdown')
    respuesta = cliente_ia.models.generate_content(model='gemini-1.5-flash', contents=texto, config=config_ia)
    await procesar_json_ia(context.bot, MI_TELEGRAM_ID, respuesta.text)
    await m.delete()

async def procesar_audio_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_TELEGRAM_ID: return
    m = await update.message.reply_text("🎙️ *Transcribiendo con IA...*", parse_mode='Markdown')
    
    archivo = await context.bot.get_file(update.message.voice.file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio:
        await archivo.download_to_drive(temp_audio.name)
        
        archivo_gemini = cliente_ia.files.upload(file=temp_audio.name, config={'mime_type': 'audio/ogg'})
        respuesta = cliente_ia.models.generate_content(
            model='gemini-1.5-flash',
            contents=[archivo_gemini, "Analiza este audio y extrae los datos."],
            config=config_ia
        )
        await procesar_json_ia(context.bot, MI_TELEGRAM_ID, respuesta.text)
        
        cliente_ia.files.delete(name=archivo_gemini.name)
        os.remove(temp_audio.name)
    await m.delete()

async def procesar_foto_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_TELEGRAM_ID: return
    m = await update.message.reply_text("📸 *Analizando ticket con IA...*", parse_mode='Markdown')
    
    archivo = await context.bot.get_file(update.message.photo[-1].file_id)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_foto:
        await archivo.download_to_drive(temp_foto.name)
        
        archivo_gemini = cliente_ia.files.upload(file=temp_foto.name, config={'mime_type': 'image/jpeg'})
        respuesta = cliente_ia.models.generate_content(
            model='gemini-1.5-flash',
            contents=[archivo_gemini, "Lee este ticket o foto y extrae los datos del gasto."],
            config=config_ia
        )
        await procesar_json_ia(context.bot, MI_TELEGRAM_ID, respuesta.text)
        
        cliente_ia.files.delete(name=archivo_gemini.name)
        os.remove(temp_foto.name)
    await m.delete()

# ==========================================
# 4. FLUJOS MANUALES
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != MI_TELEGRAM_ID: return ConversationHandler.END
    if not datos_usuario.get('nombre'):
        datos_usuario['esperando_nombre'] = True
        await update.message.reply_text("¡Hola! Antes de iniciar, ¿cómo quieres que me dirija a ti?")
        return ConversationHandler.END
    await update.message.reply_text(f"¡Qué tal, {datos_usuario['nombre']}!\nPuedes enviarme audios, fotos de tickets, escribir directo, o usar el menú:", reply_markup=obtener_menu_principal())
    return ConversationHandler.END

async def manejador_botones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != MI_TELEGRAM_ID: return
    await query.answer() 

    if query.data == 'menu_tarea':
        await query.edit_message_text("Módulo de Tareas. ¿Qué haremos?", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Registrar manual", callback_data='tarea_nueva')], [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
    elif query.data == 'menu_finanzas':
        teclado = [
            [InlineKeyboardButton("💸 Gasto Manual", callback_data='finanza_gasto'), InlineKeyboardButton("💵 Ingreso Manual", callback_data='finanza_ingreso')],
            [InlineKeyboardButton("📊 Balance Histórico", callback_data='ver_balance_todo')],
            [InlineKeyboardButton("📅 Resumen del Mes", callback_data='ver_balance_mes')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]
        await query.edit_message_text("Módulo Financiero:", reply_markup=InlineKeyboardMarkup(teclado))
    elif query.data == 'ver_calendario':
        conn = sqlite3.connect('mi_vida.db')
        c = conn.cursor()
        c.execute("SELECT descripcion, tipo, prioridad, fecha_calculada FROM tareas ORDER BY fecha_calculada ASC")
        tareas = c.fetchall()
        conn.close()
        if not tareas: mensaje = "📅 *Tu Calendario está vacío.*"
        else:
            tareas_por_dia = defaultdict(list)
            for t in tareas: tareas_por_dia[t[3] if t[3] else "Recurrentes"].append(t)
            mensaje = "📅 *TU AGENDA:*\n\n"
            for dia, lista_tareas in tareas_por_dia.items():
                mensaje += f"🗓️ *{dia}*\n"
                for t in lista_tareas: mensaje += f"   ├ ▫️ {t[0]} ({t[1]} - {t[2]})\n"
        await query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
    elif query.data.startswith('ver_balance_'):
        filtro = query.data.split('_')[2]
        conn = sqlite3.connect('mi_vida.db')
        c = conn.cursor()
        if filtro == 'mes':
            mes_actual = datetime.datetime.now().strftime('%Y-%m')
            c.execute("SELECT tipo, area, monto FROM finanzas WHERE strftime('%Y-%m', fecha) = ?", (mes_actual,))
            titulo = f"📊 *BALANCE DEL MES ({mes_actual})*\n\n"
        else:
            c.execute("SELECT tipo, area, monto FROM finanzas")
            titulo = "📊 *BALANCE HISTÓRICO*\n\n"
        registros = c.fetchall()
        conn.close()
        ing_emp = sum(r[2] for r in registros if r[0] == 'Ingreso' and r[1] == 'Empresa')
        gas_emp = sum(r[2] for r in registros if r[0] == 'Gasto' and r[1] == 'Empresa')
        ing_per = sum(r[2] for r in registros if r[0] == 'Ingreso' and r[1] == 'Personal')
        gas_per = sum(r[2] for r in registros if r[0] == 'Gasto' and r[1] == 'Personal')
        mensaje = titulo + f"🏢 *EMPRESA:*\n   ├ Ingresado: ${ing_emp}\n   ├ Gastado: ${gas_emp}\n   └ 🟢 A favor: ${max(0, ing_emp - gas_emp)} | 🔴 Te deben: ${max(0, gas_emp - ing_emp)}\n\n👤 *PERSONAL:*\n   ├ Ingresado: ${ing_per}\n   ├ Gastado: ${gas_per}\n   └ 🟢 A favor: ${max(0, ing_per - gas_per)} | 🔴 Faltante: ${max(0, gas_per - ing_per)}"
        await query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='menu_finanzas')]]))
    elif query.data == 'modo_enfoque':
        teclado = [[InlineKeyboardButton("25m", callback_data='start_enfoque_25'), InlineKeyboardButton("45m", callback_data='start_enfoque_45'), InlineKeyboardButton("60m", callback_data='start_enfoque_60')], [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]
        await query.edit_message_text("⏱️ *Modo Enfoque*\nElige el tiempo de trabajo profundo:", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
    elif query.data.startswith('start_enfoque_'):
        minutos = int(query.data.split('_')[2])
        await query.edit_message_text(f"⏱️ *Enfoque activo: {minutos} minutos.*\n¡Silencia distracciones!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ Finalizar por Urgencia", callback_data='cancelar_enfoque')]]), parse_mode='Markdown')
        async def temp():
            await asyncio.sleep(minutos * 60)
            await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"🔔 *¡TIEMPO CUMPLIDO ({minutos} min)!* Tómate un descanso.", parse_mode='Markdown')
        context.user_data['tarea_enfoque'] = asyncio.create_task(temp())
    elif query.data == 'cancelar_enfoque':
        if 'tarea_enfoque' in context.user_data: context.user_data['tarea_enfoque'].cancel()
        await query.edit_message_text("⏹️ *Enfoque detenido.*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
    elif query.data == 'volver_inicio':
        await query.edit_message_text("Menú Principal:", reply_markup=obtener_menu_principal())

# ==========================================
# ARRANQUE DEL BOT
# ==========================================
if __name__ == '__main__':
    app = ApplicationBuilder().token(TOKEN).build()

    # 1. Configurar el Reloj Interno usando JobQueue de Telegram (Eliminamos APScheduler)
    zona_horaria = ZoneInfo('America/Monterrey')
    app.job_queue.run_daily(alerta_matutina, time=datetime.time(hour=6, minute=0, tzinfo=zona_horaria))
    app.job_queue.run_daily(alerta_nocturna, time=datetime.time(hour=19, minute=0, tzinfo=zona_horaria))

    # 2. Manejadores de IA
    app.add_handler(MessageHandler(filters.VOICE, procesar_audio_ia))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_foto_ia))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_texto_libre_ia)) 

    # 3. Manejadores Manuales
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(manejador_botones)) 

    print("🤖 Súper Bot iniciado: DB Local + IA Actualizada + Alertas JobQueue.")
    app.run_polling()
