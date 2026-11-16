import logging
import asyncio
import psycopg2
import datetime
from zoneinfo import ZoneInfo
import dateparser
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from google import genai
from google.genai import types
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# --- CONFIGURACIÓN DE CLAVES ---
TOKEN = '9156918216:AAG4dC_F7iB3SGiq1IsXrevJAbFIHD0iLIo'
MI_TELEGRAM_ID = 6612216260
GEMINI_API_KEY = 'AQ.Ab8RN6K_dcsTxsYsc_y4yke47Kogk-ZcpTGHnBrncQLaC9FbrQ'
DATABASE_URL = 'postgresql://neondb_owner:npg_zPubRs28rxml@ep-plain-bar-bfya12gv-pooler.c-5.us-west-2.aws.neon.tech/neondb?sslmode=require'

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

datos_usuario = {}

# Estados para los menús manuales
T_DESCRIPCION, T_TIPO, T_PRIORIDAD, T_FECHA, T_NOTAS = range(5)
F_AREA, F_CONCEPTO, F_MONTO, F_METODO, F_FECHA = range(5, 10)

# ==========================================
# SERVIDOR FANTASMA PARA RENDER (GRATIS)
# ==========================================
class ServidorFantasma(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot de Telegram Activo y Escuchando!")

def iniciar_servidor():
    puerto = int(os.environ.get("PORT", 10000))
    servidor = HTTPServer(('0.0.0.0', puerto), ServidorFantasma)
    servidor.serve_forever()

# ==========================================
# CONFIGURACIÓN DE IA CON MEMORIA
# ==========================================
try:
    with open('Planificador de tareas.txt', 'r', encoding='utf-8') as f:
        planificador_contexto = f.read()
except FileNotFoundError:
    planificador_contexto = "El usuario no ha subido el archivo. Actúa con sentido común."

cliente_ia = genai.Client(api_key=GEMINI_API_KEY)

instruccion_sistema = f"""
Eres un asistente personal organizador inteligente con memoria de conversación. Analiza la entrada del usuario (texto, audio o ticket).
Toma en cuenta su documento "Planificador de tareas" para entender su contexto:

--- INICIO PLANIFICADOR ---
{planificador_contexto}
--- FIN PLANIFICADOR ---

REGLA 1 (APRENDIZAJE): Si el usuario te pide registrar algo que es ambiguo, le falta el monto/fecha, o no encaja en las categorías del Planificador, NO generes JSON. En su lugar, respóndele en texto plano con una pregunta breve y directa para aclarar la duda.
REGLA 2 (GUARDADO): Si ya tienes toda la información clara, devuelve ÚNICAMENTE un objeto JSON válido, sin comillas invertidas ni texto adicional.

Formato Tarea: {{"tipo_registro": "tarea", "descripcion": "string", "tipo": "Empresa" o "Personal", "prioridad": "Urgente" o "Rutina", "fecha_original": "string", "notas": "string"}}
Formato Finanza: {{"tipo_registro": "finanza", "tipo": "Gasto" o "Ingreso", "area": "Empresa" o "Personal", "concepto": "string", "monto": numero_float, "metodo": "Tarjeta" o "Efectivo", "fecha": "string"}}
"""
config_ia = types.GenerateContentConfig(system_instruction=instruccion_sistema)
chat_ia = cliente_ia.chats.create(model='gemini-3.8-flash', config=config_ia)

# ==========================================
# BASE DE DATOS (POSTGRESQL NEON)
# ==========================================
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tareas (id SERIAL PRIMARY KEY, descripcion TEXT, tipo TEXT, prioridad TEXT, fecha_original TEXT, fecha_calculada TEXT, notas TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finanzas (id SERIAL PRIMARY KEY, tipo TEXT, area TEXT, concepto TEXT, monto REAL, metodo TEXT, fecha TEXT)''')
    conn.commit()
    conn.close()

init_db()

def obtener_menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Tareas (Manual)", callback_data='menu_tarea')],
        [InlineKeyboardButton("💰 Finanzas (Manual)", callback_data='menu_finanzas')],
        [InlineKeyboardButton("📅 Ver Calendario", callback_data='ver_calendario')],
        [InlineKeyboardButton("⏱️ Modo Enfoque", callback_data='modo_enfoque')]
    ])

# ==========================================
# MOTOR DE ALERTAS AUTOMÁTICAS
# ==========================================
async def alerta_matutina(context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.date.today().strftime('%Y-%m-%d')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT descripcion, tipo, prioridad FROM tareas WHERE fecha_calculada = %s", (hoy,))
    tareas_hoy = c.fetchall()
    conn.close()

    mensaje = "☀️ *Buenos días.* Aquí está tu lista para hoy:\n\n"
    if not tareas_hoy:
        mensaje += "No tienes actividades programadas. ¡Aprovecha el día!"
    else:
        for t in tareas_hoy:
            mensaje += f"▫️ {t[0]} ({t[1]} - {t[2]})\n"
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=mensaje, parse_mode='Markdown')

async def alerta_nocturna(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="🌙 *Corte de caja.* \nMomento de revisar pendientes y desconectar. ¿Cómo te fue hoy?", parse_mode='Markdown')

# ==========================================
# PROCESAMIENTO DE IA CON MANEJO DE ERRORES
# ==========================================
async def procesar_respuesta_ia(bot, chat_id, respuesta_texto):
    texto_limpio = respuesta_texto.replace('```json', '').replace('```', '').strip()
    
    if texto_limpio.startswith('{') and texto_limpio.endswith('}'):
        try:
            datos = json.loads(texto_limpio)
            conn = get_db_connection()
            c = conn.cursor()
            
            if datos.get('tipo_registro') == 'tarea':
                fecha_calc = dateparser.parse(datos.get('fecha_original', ''), languages=['es'])
                fecha_str = fecha_calc.strftime('%Y-%m-%d') if fecha_calc else "Recurrente"
                c.execute("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, notas) VALUES (%s, %s, %s, %s, %s, %s)",
                          (datos.get('descripcion'), datos.get('tipo'), datos.get('prioridad'), datos.get('fecha_original'), fecha_str, datos.get('notas', '')))
                mensaje = f"✅ *Tarea guardada con IA*\n📌 {datos.get('descripcion')}\n⏰ Para: {fecha_str} ({datos.get('tipo')})"
                
            elif datos.get('tipo_registro') == 'finanza':
                fecha_calc = dateparser.parse(datos.get('fecha', ''), languages=['es'])
                fecha_str = fecha_calc.strftime('%Y-%m-%d') if fecha_calc else datetime.date.today().strftime('%Y-%m-%d')
                c.execute("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha) VALUES (%s, %s, %s, %s, %s, %s)",
                          (datos.get('tipo'), datos.get('area'), datos.get('concepto'), datos.get('monto'), datos.get('metodo'), fecha_str))
                mensaje = f"✅ *Finanza guardada con IA*\n💰 {datos.get('tipo')}: ${datos.get('monto')} en {datos.get('concepto')} ({datos.get('area')})"
            
            conn.commit()
            conn.close()
            await bot.send_message(chat_id=chat_id, text=mensaje, parse_mode='Markdown', reply_markup=obtener_menu_principal())
            return
        except Exception:
            pass
            
    await bot.send_message(chat_id=chat_id, text=f"🤖 {respuesta_texto}", parse_mode='Markdown')

async def procesar_texto_libre_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_TELEGRAM_ID: return
    texto = update.message.text
    m = await update.message.reply_text("🧠 *Pensando...*", parse_mode='Markdown')
    try:
        respuesta = chat_ia.send_message(texto)
        await procesar_respuesta_ia(context.bot, MI_TELEGRAM_ID, respuesta.text)
    except Exception as e:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="⏳ *Servidores de IA ocupados.* Por favor, usa los botones del Menú Manual.", parse_mode='Markdown', reply_markup=obtener_menu_principal())
    finally:
        await m.delete()

async def procesar_audio_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_TELEGRAM_ID: return
    m = await update.message.reply_text("🎙️ *Transcribiendo con IA...*", parse_mode='Markdown')
    try:
        archivo = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio:
            await archivo.download_to_drive(temp_audio.name)
            archivo_gemini = cliente_ia.files.upload(file=temp_audio.name, config={'mime_type': 'audio/ogg'})
            
            respuesta = chat_ia.send_message([archivo_gemini, "Extrae los datos de este audio. Si te falta información, pregúntame."])
            await procesar_respuesta_ia(context.bot, MI_TELEGRAM_ID, respuesta.text)
            
            cliente_ia.files.delete(name=archivo_gemini.name)
            os.remove(temp_audio.name)
    except Exception as e:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="⏳ *Servidores de IA ocupados.* Por favor, usa el Menú Manual.", parse_mode='Markdown', reply_markup=obtener_menu_principal())
    finally:
        await m.delete()

async def procesar_foto_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MI_TELEGRAM_ID: return
    m = await update.message.reply_text("📸 *Analizando ticket con IA...*", parse_mode='Markdown')
    try:
        archivo = await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_foto:
            await archivo.download_to_drive(temp_foto.name)
            archivo_gemini = cliente_ia.files.upload(file=temp_foto.name, config={'mime_type': 'image/jpeg'})
            
            respuesta = chat_ia.send_message([archivo_gemini, "Extrae los datos de este ticket. Si te falta saber si es de empresa o personal, pregúntame."])
            await procesar_respuesta_ia(context.bot, MI_TELEGRAM_ID, respuesta.text)
            
            cliente_ia.files.delete(name=archivo_gemini.name)
            os.remove(temp_foto.name)
    except Exception as e:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="⏳ *Servidores de IA ocupados.* Por favor, usa el Menú Manual.", parse_mode='Markdown', reply_markup=obtener_menu_principal())
    finally:
        await m.delete()

# ==========================================
# MENÚS GENERALES Y BALANCE
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != MI_TELEGRAM_ID: return ConversationHandler.END
    await update.message.reply_text("¡Hola! Háblame, mándame fotos o usa el menú manual:", reply_markup=obtener_menu_principal())
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
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT descripcion, tipo, prioridad, fecha_calculada FROM tareas ORDER BY fecha_calculada ASC")
        tareas = c.fetchall()
        conn.close()
        if not tareas: mensaje = "📅 *Tu Calendario está vacío.*"
        else:
            tareas_por_dia = defaultdict(list)
            for t in tareas: tareas_por_dia[t[3]].append(t)
            mensaje = "📅 *TU AGENDA:*\n\n"
            for dia, lista_tareas in tareas_por_dia.items():
                mensaje += f"🗓️ *{dia}*\n"
                for t in lista_tareas: mensaje += f"   ├ ▫️ {t[0]} ({t[1]} - {t[2]})\n"
        await query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
    elif query.data.startswith('ver_balance_'):
        filtro = query.data.split('_')[2]
        conn = get_db_connection()
        c = conn.cursor()
        if filtro == 'mes':
            mes_actual = datetime.datetime.now().strftime('%Y-%m')
            c.execute("SELECT tipo, area, monto FROM finanzas WHERE fecha LIKE %s", (f"{mes_actual}-%",))
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
        await query.edit_message_text("⏱️ *Modo Enfoque*\nElige el tiempo:", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
    elif query.data.startswith('start_enfoque_'):
        minutos = int(query.data.split('_')[2])
        await query.edit_message_text(f"⏱️ *Enfoque activo: {minutos} minutos.*\n¡A trabajar!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⏹️ Finalizar", callback_data='cancelar_enfoque')]]), parse_mode='Markdown')
        async def temp():
            await asyncio.sleep(minutos * 60)
            await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"🔔 *¡TIEMPO CUMPLIDO ({minutos} min)!* Descansa.", parse_mode='Markdown')
        context.user_data['tarea_enfoque'] = asyncio.create_task(temp())
    elif query.data == 'cancelar_enfoque':
        if 'tarea_enfoque' in context.user_data: context.user_data['tarea_enfoque'].cancel()
        await query.edit_message_text("⏹️ *Enfoque detenido.*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
    elif query.data == 'volver_inicio':
        await query.edit_message_text("Menú Principal:", reply_markup=obtener_menu_principal())

async def cancelar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Cancelado. Volviendo al menú...", reply_markup=obtener_menu_principal())
    return ConversationHandler.END

# ==========================================
# FLUJOS MANUALES COMPLETOS (Tareas)
# ==========================================
async def iniciar_registro_tarea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['nueva_tarea'] = {}
    await query.edit_message_text("📝 *Descripción* (Ej. 'Enviar reporte'):", parse_mode='Markdown')
    return T_DESCRIPCION

async def capturar_descripcion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['nueva_tarea']['descripcion'] = update.message.text
    teclado = [[InlineKeyboardButton("Empresa", callback_data='tipo_empresa'), InlineKeyboardButton("Personal", callback_data='tipo_personal')]]
    await update.message.reply_text("📂 *Categoría:*", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
    return T_TIPO

async def capturar_tipo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['nueva_tarea']['tipo'] = "Empresa" if query.data == 'tipo_empresa' else "Personal"
    teclado = [[InlineKeyboardButton("Urgente", callback_data='prio_urgente'), InlineKeyboardButton("Rutina", callback_data='prio_rutina')]]
    await query.edit_message_text("⚡ *Prioridad:*", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
    return T_PRIORIDAD

async def capturar_prioridad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['nueva_tarea']['prioridad'] = "Urgente" if query.data == 'prio_urgente' else "Rutina"
    await query.edit_message_text("📅 *Fecha* (Ej. 'Mañana a las 5pm', 'Lunes y Viernes'):", parse_mode='Markdown')
    return T_FECHA

async def capturar_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto_fecha = update.message.text
    context.user_data['nueva_tarea']['fecha_original'] = texto_fecha
    fecha_calculada = dateparser.parse(texto_fecha, languages=['es'])
    context.user_data['nueva_tarea']['fecha_calculada'] = fecha_calculada.strftime('%Y-%m-%d') if fecha_calculada else "Recurrente"
    teclado = [[InlineKeyboardButton("Saltar notas", callback_data='sin_notas')]]
    await update.message.reply_text("🗒️ *Notas (Opcional):*", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
    return T_NOTAS

async def capturar_notas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    notas = "Ninguna" if update.callback_query else update.message.text
    tarea = context.user_data['nueva_tarea']
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, notas) VALUES (%s, %s, %s, %s, %s, %s)",
              (tarea['descripcion'], tarea['tipo'], tarea['prioridad'], tarea['fecha_original'], tarea['fecha_calculada'], notas))
    conn.commit()
    conn.close()
    
    msg = f"✅ *Tarea guardada (Manual)*\n📌 {tarea['descripcion']}\n⏰ Para: {tarea['fecha_calculada']}"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode='Markdown', reply_markup=obtener_menu_principal())
    else:
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=obtener_menu_principal())
    return ConversationHandler.END

# ==========================================
# FLUJOS MANUALES COMPLETOS (Finanzas)
# ==========================================
async def iniciar_finanzas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['nueva_finanza'] = {'tipo': "Gasto" if query.data == 'finanza_gasto' else "Ingreso"}
    teclado = [[InlineKeyboardButton("Empresa", callback_data='f_empresa'), InlineKeyboardButton("Personal", callback_data='f_personal')]]
    await query.edit_message_text("💰 *¿Empresa o Personal?*", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
    return F_AREA

async def capturar_f_area(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['nueva_finanza']['area'] = "Empresa" if query.data == 'f_empresa' else "Personal"
    await query.edit_message_text("📝 *Concepto:*", parse_mode='Markdown')
    return F_CONCEPTO

async def capturar_f_concepto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['nueva_finanza']['concepto'] = update.message.text
    await update.message.reply_text("💵 *Monto numérico:*", parse_mode='Markdown')
    return F_MONTO

async def capturar_f_monto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: context.user_data['nueva_finanza']['monto'] = float(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Solo números:")
        return F_MONTO 
    
    if context.user_data['nueva_finanza']['tipo'] == "Gasto":
        teclado = [[InlineKeyboardButton("Tarjeta", callback_data='f_tarjeta'), InlineKeyboardButton("Efectivo", callback_data='f_efectivo')]]
        await update.message.reply_text("💳 *Método de Pago*", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
        return F_METODO
    else:
        context.user_data['nueva_finanza']['metodo'] = "N/A"
        await update.message.reply_text("📅 *Fecha (Ej. Ayer, 10 de nov):*", parse_mode='Markdown')
        return F_FECHA

async def capturar_f_metodo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['nueva_finanza']['metodo'] = "Tarjeta" if query.data == 'f_tarjeta' else "Efectivo"
    await query.edit_message_text("📅 *Fecha (Ej. Ayer, 10 de nov):*", parse_mode='Markdown')
    return F_FECHA

async def capturar_f_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    f = context.user_data['nueva_finanza']
    fecha_calc = dateparser.parse(update.message.text, languages=['es'])
    fecha_final = fecha_calc.strftime('%Y-%m-%d') if fecha_calc else datetime.datetime.now().strftime('%Y-%m-%d')
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha) VALUES (%s, %s, %s, %s, %s, %s)",
              (f['tipo'], f['area'], f['concepto'], f['monto'], f['metodo'], fecha_final))
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ *Finanza guardada (Manual)*", parse_mode='Markdown', reply_markup=obtener_menu_principal())
    return ConversationHandler.END

# ==========================================
# ARRANQUE DEL BOT
# ==========================================
if __name__ == '__main__':
    # 1. Encendemos servidor fantasma
    threading.Thread(target=iniciar_servidor, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()

    # 2. Configurar Alertas
    zona_horaria = ZoneInfo('America/Monterrey')
    app.job_queue.run_daily(alerta_matutina, time=datetime.time(hour=6, minute=0, tzinfo=zona_horaria))
    app.job_queue.run_daily(alerta_nocturna, time=datetime.time(hour=19, minute=0, tzinfo=zona_horaria))

    # 3. Ensamblar Menús Manuales (ConversationHandlers)
    conv_handler_tarea = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_registro_tarea, pattern='^tarea_nueva$')],
        states={
            T_DESCRIPCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_descripcion)],
            T_TIPO: [CallbackQueryHandler(capturar_tipo, pattern='^tipo_')],
            T_PRIORIDAD: [CallbackQueryHandler(capturar_prioridad, pattern='^prio_')],
            T_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_fecha)],
            T_NOTAS: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_notas), CallbackQueryHandler(capturar_notas, pattern='^sin_notas$')],
        },
        fallbacks=[CommandHandler('cancelar', cancelar_registro)],
        per_message=False
    )

    conv_handler_finanzas = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_finanzas, pattern='^finanza_gasto$|^finanza_ingreso$')],
        states={
            F_AREA: [CallbackQueryHandler(capturar_f_area, pattern='^f_')],
            F_CONCEPTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_concepto)],
            F_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_monto)],
            F_METODO: [CallbackQueryHandler(capturar_f_metodo, pattern='^f_')],
            F_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_fecha)],
        },
        fallbacks=[CommandHandler('cancelar', cancelar_registro)],
        per_message=False
    )

    # Añadir Handlers (El orden importa: ConversationHandlers primero, texto libre IA al final)
    app.add_handler(conv_handler_tarea)
    app.add_handler(conv_handler_finanzas)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(manejador_botones)) 
    
    # Handlers de IA
    app.add_handler(MessageHandler(filters.VOICE, procesar_audio_ia))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_foto_ia))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_texto_libre_ia)) 

    print("🤖 Súper Bot iniciado: IA Conversacional + Menú Manual Completo + BD en Nube.")
    app.run_polling()
