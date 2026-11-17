import logging
import asyncio
import os
import re
import datetime
from zoneinfo import ZoneInfo
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg2
import dateparser
from dateparser.search import search_dates

import speech_recognition as sr
from pydub import AudioSegment
import pytesseract
from PIL import Image

from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# ==========================================
# 1. SEGURIDAD TOTAL (Variables de Entorno)
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MI_TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", 0))
ALLOWED_USERS = [MI_TELEGRAM_ID]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Estados para los menús manuales
T_DESCRIPCION, T_TIPO, T_PRIORIDAD, T_FECHA, T_NOTAS = range(5)
F_AREA, F_CONCEPTO, F_MONTO, F_METODO, F_FECHA = range(5, 10)

# ==========================================
# 2. SERVIDOR FANTASMA + HEALTH CHECK
# ==========================================
class ServidorFantasma(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot NLP Activo y Seguro!")

def iniciar_servidor():
    puerto = int(os.environ.get("PORT", 10000))
    servidor = HTTPServer(('0.0.0.0', puerto), ServidorFantasma)
    servidor.serve_forever()

# ==========================================
# 3. BASE DE DATOS (POSTGRESQL NEON)
# ==========================================
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tareas (id SERIAL PRIMARY KEY, descripcion TEXT, tipo TEXT, prioridad TEXT, fecha_original TEXT, fecha_calculada TEXT, notas TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finanzas (id SERIAL PRIMARY KEY, tipo TEXT, area TEXT, concepto TEXT, monto REAL, metodo TEXT, fecha TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS diccionario (palabra TEXT PRIMARY KEY, categoria TEXT)''')
    
    # Índices para que sea súper rápido
    c.execute("CREATE INDEX IF NOT EXISTS idx_tareas_fecha ON tareas(fecha_calculada)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_finanzas_fecha ON finanzas(fecha)")
    conn.commit()
    conn.close()

# ==========================================
# 4. ALERTAS AUTOMÁTICAS
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
        for t in tareas_hoy: mensaje += f"▫️ {t[0]} ({t[1]} - {t[2]})\n"
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=mensaje, parse_mode='Markdown')

async def alerta_nocturna(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="🌙 *Corte de caja.* \nMomento de revisar pendientes y desconectar. ¿Cómo te fue hoy?", parse_mode='Markdown')

# ==========================================
# 5. MOTOR NLP Y APRENDIZAJE AUTOMÁTICO
# ==========================================
PALABRAS_EMPRESA = {'viático': 3, 'vuelo': 2, 'gasolina': 2, 'uber': 2, 'cliente': 3, 'papelería': 2, 'reporte': 2, 'empresa': 3, 'proveedor': 3}
PALABRAS_PERSONAL = {'despensa': 3, 'luz': 3, 'agua': 3, 'internet': 3, 'cfe': 3, 'renta': 3, 'casa': 2, 'médico': 2, 'personal': 3, 'ropa': 2}
PALABRAS_GASTO = ['pagué', 'pague', 'compré', 'compre', 'gasté', 'gaste', 'costó', 'pagar', 'ticket', 'recibo']
PALABRAS_INGRESO = ['deposité', 'deposite', 'transferí', 'cobré', 'sueldo', 'pagaron', 'ingreso']

def cargar_diccionario_aprendido():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT palabra, categoria FROM diccionario")
    filas = c.fetchall()
    conn.close()
    
    emp = {f[0]: 3 for f in filas if f[1] == 'Empresa'}
    per = {f[0]: 3 for f in filas if f[1] == 'Personal'}
    return emp, per

def extraer_fecha(texto):
    fechas = search_dates(texto, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    if fechas: return fechas[0][1].strftime('%Y-%m-%d')
    return None

def analizar_texto_local(texto):
    texto_min = texto.lower()
    
    dyn_emp, dyn_per = cargar_diccionario_aprendido()
    dict_empresa = {**PALABRAS_EMPRESA, **dyn_emp}
    dict_personal = {**PALABRAS_PERSONAL, **dyn_per}
    
    score_emp = sum(peso for palabra, peso in dict_empresa.items() if palabra in texto_min)
    score_per = sum(peso for palabra, peso in dict_personal.items() if palabra in texto_min)
    
    es_gasto = any(p in texto_min for p in PALABRAS_GASTO)
    es_ingreso = any(p in texto_min for p in PALABRAS_INGRESO)
    
    match_monto = re.search(r'\$?\s*([\d,]+(?:\.\d{1,2})?)\b(?:\s*pesos?)?', texto_min)
    
    if (es_gasto or es_ingreso or '$' in texto_min or 'pesos' in texto_min) and match_monto:
        tipo_finanza = "Ingreso" if es_ingreso and not es_gasto else "Gasto"
        monto = float(match_monto.group(1).replace(',', ''))
        fecha_calc = extraer_fecha(texto) or datetime.date.today().strftime('%Y-%m-%d')
        
        concepto = re.sub(r'\$?\s*([\d,]+(?:\.\d{1,2})?)\b(?:\s*pesos?)?', '', texto_min, flags=re.IGNORECASE).strip()
        for p in PALABRAS_GASTO + PALABRAS_INGRESO:
            concepto = concepto.replace(p, "").strip()
        
        if score_emp == 0 and score_per == 0:
            area = "Desconocida"
        else:
            area = "Empresa" if score_emp > score_per else "Personal"
            
        return {"tipo_registro": "finanza", "tipo": tipo_finanza, "area": area, "concepto": concepto[:40], "monto": monto, "metodo": "Efectivo", "fecha": fecha_calc}
    
    if score_emp == 0 and score_per == 0: area = "Desconocida"
    else: area = "Empresa" if score_emp > score_per else "Personal"
    
    prioridad = "Urgente" if "urgente" in texto_min else "Rutina"
    fecha_calc = extraer_fecha(texto) or "Recurrente"
    return {"tipo_registro": "tarea", "descripcion": texto[:60], "tipo": area, "prioridad": prioridad, "fecha_original": texto, "fecha_calculada": fecha_calc, "notas": ""}

# ==========================================
# 6. ENTRADA DE DATOS NLP Y GUARDADO
# ==========================================
async def guardar_datos_nlp(bot, chat_id, datos):
    conn = get_db_connection()
    c = conn.cursor()
    if datos['tipo_registro'] == 'tarea':
        c.execute("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, notas) VALUES (%s, %s, %s, %s, %s, %s)",
                  (datos['descripcion'], datos['tipo'], datos['prioridad'], datos['fecha_original'], datos['fecha_calculada'], datos['notas']))
        mensaje = f"✅ *Tarea guardada*\n📌 {datos['descripcion']}\n⏰ Para: {datos['fecha_calculada']} ({datos['tipo']})"
    elif datos['tipo_registro'] == 'finanza':
        c.execute("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha) VALUES (%s, %s, %s, %s, %s, %s)",
                  (datos['tipo'], datos['area'], datos['concepto'], datos['monto'], datos['metodo'], datos['fecha']))
        mensaje = f"✅ *Finanza guardada*\n💰 {datos['tipo']}: ${datos['monto']} en {datos['concepto']} ({datos['area']})"
    conn.commit()
    conn.close()
    await bot.send_message(chat_id=chat_id, text=mensaje, parse_mode='Markdown', reply_markup=obtener_menu_principal())

async def manejar_resultado_analisis(update, context, datos):
    area = datos.get('area') or datos.get('tipo')
    if area == "Desconocida":
        context.user_data['pendiente_clasificar'] = datos
        palabra = datos['concepto'] if datos['tipo_registro'] == 'finanza' else datos['descripcion']
        teclado = [[InlineKeyboardButton("🏢 Empresa", callback_data='aprender_empresa')], [InlineKeyboardButton("👤 Personal", callback_data='aprender_personal')]]
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"🤔 No reconozco si *'{palabra}'* es de Empresa o Personal.\n\nClasifícalo para que yo lo aprenda:", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
    else:
        await guardar_datos_nlp(context.bot, MI_TELEGRAM_ID, datos)

async def procesar_texto_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    texto = update.message.text
    m = await update.message.reply_text("🧠 *Analizando...*", parse_mode='Markdown')
    datos = analizar_texto_local(texto)
    await manejar_resultado_analisis(update, context, datos)
    await m.delete()

async def procesar_audio_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("🎙️ *Escuchando...*", parse_mode='Markdown')
    try:
        archivo = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio:
            await archivo.download_to_drive(temp_audio.name)
            wav_path = temp_audio.name.replace(".ogg", ".wav")
            AudioSegment.from_ogg(temp_audio.name).export(wav_path, format="wav")
            
            r = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                texto_transcrito = r.recognize_google(r.record(source), language="es-MX")
            
            await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"🗣️ _\"{texto_transcrito}\"_", parse_mode='Markdown')
            datos = analizar_texto_local(texto_transcrito)
            await manejar_resultado_analisis(update, context, datos)
            os.remove(temp_audio.name)
            os.remove(wav_path)
    except Exception:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="⚠️ Audio no reconocido. Usa menú manual.")
    finally:
        await m.delete()

async def procesar_foto_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("📸 *Escaneando...*", parse_mode='Markdown')
    try:
        archivo = await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_foto:
            await archivo.download_to_drive(temp_foto.name)
            texto_extraido = pytesseract.image_to_string(Image.open(temp_foto.name), lang='spa')
            datos = analizar_texto_local(f"ticket pagué {texto_extraido}")
            await manejar_resultado_analisis(update, context, datos)
            os.remove(temp_foto.name)
    except Exception:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="⚠️ Foto no reconocida. Usa menú manual.")
    finally:
        await m.delete()

# ==========================================
# 7. MENÚS PRINCIPALES Y BALANCES
# ==========================================
def obtener_menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Tareas", callback_data='menu_tarea'), InlineKeyboardButton("💰 Finanzas", callback_data='menu_finanzas')],
        [InlineKeyboardButton("📅 Calendario", callback_data='ver_calendario'), InlineKeyboardButton("⏱️ Enfoque", callback_data='modo_enfoque')]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in ALLOWED_USERS: return ConversationHandler.END
    await update.message.reply_text("¡Hola! Háblame, mándame fotos, audios o usa el menú manual:", reply_markup=obtener_menu_principal())
    return ConversationHandler.END

async def manejador_botones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS: return
    await query.answer() 

    if query.data == 'menu_tarea':
        await query.edit_message_text("Módulo de Tareas:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Registrar manual", callback_data='tarea_nueva')], [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
    elif query.data == 'menu_finanzas':
        teclado = [
            [InlineKeyboardButton("💸 Gasto Manual", callback_data='finanza_gasto'), InlineKeyboardButton("💵 Ingreso Manual", callback_data='finanza_ingreso')],
            [InlineKeyboardButton("✏️ Borrar Recientes", callback_data='editar_recientes')],
            [InlineKeyboardButton("📊 Balance Histórico", callback_data='ver_balance_todo'), InlineKeyboardButton("📅 Mes actual", callback_data='ver_balance_mes')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]
        await query.edit_message_text("Módulo Financiero:", reply_markup=InlineKeyboardMarkup(teclado))
    
    # Motor de Aprendizaje Interactivo
    elif query.data.startswith('aprender_'):
        categoria = "Empresa" if query.data == 'aprender_empresa' else "Personal"
        datos = context.user_data.get('pendiente_clasificar')
        if datos:
            palabra_nueva = datos['concepto'] if datos['tipo_registro'] == 'finanza' else datos['descripcion']
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("INSERT INTO diccionario (palabra, categoria) VALUES (%s, %s) ON CONFLICT (palabra) DO NOTHING", (palabra_nueva, categoria))
            conn.commit()
            conn.close()
            
            datos['area' if datos['tipo_registro'] == 'finanza' else 'tipo'] = categoria
            await query.edit_message_text(f"🧠 ¡He aprendido que *'{palabra_nueva}'* es *{categoria}*!", parse_mode='Markdown')
            await guardar_datos_nlp(context.bot, MI_TELEGRAM_ID, datos)
            context.user_data.pop('pendiente_clasificar', None)
            
    # Borrar registros recientes
    elif query.data == 'editar_recientes':
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, concepto, monto, fecha FROM finanzas ORDER BY id DESC LIMIT 5")
        recientes = c.fetchall()
        conn.close()
        
        teclado = []
        for r in recientes:
            teclado.append([InlineKeyboardButton(f"❌ {r[3]} - {r[1][:15]} (${r[2]})", callback_data=f'borrar_f_{r[0]}')])
        teclado.append([InlineKeyboardButton("🔙 Volver", callback_data='menu_finanzas')])
        await query.edit_message_text("Selecciona un registro para ELIMINARLO:", reply_markup=InlineKeyboardMarkup(teclado))

    elif query.data.startswith('borrar_f_'):
        id_borrar = query.data.split('_')[2]
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("DELETE FROM finanzas WHERE id = %s", (id_borrar,))
        conn.commit()
        conn.close()
        await query.edit_message_text("✅ Registro eliminado con éxito.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data='volver_inicio')]]))

    # Balances y Calendarios
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

# ==========================================
# 8. FLUJOS DE REGISTRO MANUAL COMPLETO
# ==========================================
async def cancelar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("❌ Cancelado. Volviendo al menú...", reply_markup=obtener_menu_principal())
    return ConversationHandler.END

# --- FLUJO MANUAL TAREAS ---
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
    await query.edit_message_text("📅 *Fecha* (Ej. 'Mañana a las 5pm', 'Lunes'):", parse_mode='Markdown')
    return T_FECHA

async def capturar_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    texto_fecha = update.message.text
    context.user_data['nueva_tarea']['fecha_original'] = texto_fecha
    fecha_calculada = dateparser.parse(texto_fecha, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
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

# --- FLUJO MANUAL FINANZAS ---
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
# ARRANQUE DEL BOT Y ENSAMBLAJE DE RUTAS
# ==========================================
if __name__ == '__main__':
    if not TOKEN or not DATABASE_URL or MI_TELEGRAM_ID == 0:
        print("CRÍTICO: Faltan las variables de entorno en Render (BOT_TOKEN, DATABASE_URL, TELEGRAM_ID).")
        exit(1)
        
    init_db()
    threading.Thread(target=iniciar_servidor, daemon=True).start()
    
    app = ApplicationBuilder().token(TOKEN).build()

    # Configurar Alertas
    zona_horaria = ZoneInfo('America/Monterrey')
    app.job_queue.run_daily(alerta_matutina, time=datetime.time(hour=6, minute=0, tzinfo=zona_horaria))
    app.job_queue.run_daily(alerta_nocturna, time=datetime.time(hour=19, minute=0, tzinfo=zona_horaria))

    # Ensamblar Menús Manuales (ConversationHandlers)
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

    # Añadir Handlers (El orden importa: ConversationHandlers primero, texto libre NLP al final)
    app.add_handler(conv_handler_tarea)
    app.add_handler(conv_handler_finanzas)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(manejador_botones)) 
    
    # Handlers de NLP Local
    app.add_handler(MessageHandler(filters.VOICE, procesar_audio_local))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_foto_local))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_texto_libre)) 

    print("🤖 Súper Bot V7 INICIADO: NLP Aprendizaje + Menús Manuales Completos + Seguridad.")
    app.run_polling()
