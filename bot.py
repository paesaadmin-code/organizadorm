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

# ==========================================
# 1. SEGURIDAD TOTAL (Variables de Entorno)
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MI_TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", 0))
ALLOWED_USERS = [MI_TELEGRAM_ID]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

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
            self.wfile.write(b"Bot NLP V9 (Pool DB + Estados + Auditoria) Activo!")

def iniciar_servidor():
    puerto = int(os.environ.get("PORT", 10000))
    servidor = HTTPServer(('0.0.0.0', puerto), ServidorFantasma)
    servidor.serve_forever()

# ==========================================
# 3. BASE DE DATOS OPTIMIZADA (POOL + MIGRACIONES)
# ==========================================
db_pool = None

def init_db_pool():
    global db_pool
    if not DATABASE_URL: return
    db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    # 1. Tablas Base
    c.execute('''CREATE TABLE IF NOT EXISTS tareas (id SERIAL PRIMARY KEY, descripcion TEXT, tipo TEXT, prioridad TEXT, fecha_original TEXT, fecha_calculada TEXT, notas TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS finanzas (id SERIAL PRIMARY KEY, tipo TEXT, area TEXT, concepto TEXT, monto REAL, metodo TEXT, fecha TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS diccionario (palabra TEXT PRIMARY KEY, categoria TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS historial (id SERIAL PRIMARY KEY, fecha TEXT, accion TEXT, detalle TEXT)''')
    
    # 2. Actualizaciones seguras (si las tablas ya existían, añade las columnas nuevas sin borrar datos)
    c.execute('''ALTER TABLE tareas ADD COLUMN IF NOT EXISTS estado TEXT DEFAULT 'Pendiente' ''')
    c.execute('''ALTER TABLE tareas ADD COLUMN IF NOT EXISTS fecha_completado TEXT ''')
    c.execute('''ALTER TABLE finanzas ADD COLUMN IF NOT EXISTS categoria TEXT DEFAULT 'General' ''')
    
    # 3. Índices de velocidad
    c.execute("CREATE INDEX IF NOT EXISTS idx_tareas_fecha ON tareas(fecha_calculada)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_finanzas_fecha ON finanzas(fecha)")
    conn.commit()
    release_db_connection(conn)

def registrar_auditoria(accion, detalle):
    conn = get_db_connection()
    c = conn.cursor()
    fecha_ahora = datetime.datetime.now(ZoneInfo('America/Monterrey')).strftime('%Y-%m-%d %H:%M:%S')
    c.execute("INSERT INTO historial (fecha, accion, detalle) VALUES (%s, %s, %s)", (fecha_ahora, accion, detalle))
    conn.commit()
    release_db_connection(conn)

# ==========================================
# 4. MOTOR NLP Y APRENDIZAJE AUTOMÁTICO
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
    release_db_connection(conn)
    return {f[0]: 3 for f in filas if f[1] == 'Empresa'}, {f[0]: 3 for f in filas if f[1] == 'Personal'}

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
        for p in PALABRAS_GASTO + PALABRAS_INGRESO: concepto = concepto.replace(p, "").strip()
        
        area = "Desconocida" if score_emp == 0 and score_per == 0 else ("Empresa" if score_emp > score_per else "Personal")
        return {"tipo_registro": "finanza", "tipo": tipo_finanza, "area": area, "concepto": concepto[:40], "monto": monto, "metodo": "Efectivo", "fecha": fecha_calc}
    
    es_recurrente = bool(re.search(r'(cada|todos los|los)\s+(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|d[ií]as)', texto_min)) or (sum(1 for d in ['lunes', 'martes', 'miércoles', 'miercoles', 'jueves', 'viernes', 'sábado', 'sabado', 'domingo'] if d in texto_min) >= 2)
    
    area = "Desconocida" if score_emp == 0 and score_per == 0 else ("Empresa" if score_emp > score_per else "Personal")
    prioridad = "Urgente" if "urgente" in texto_min else "Rutina"
    fecha_calc = "Recurrente" if es_recurrente else (extraer_fecha(texto) or "Recurrente")
    
    return {"tipo_registro": "tarea", "descripcion": texto[:60], "tipo": area, "prioridad": prioridad, "fecha_original": texto, "fecha_calculada": fecha_calc, "notas": ""}

# ==========================================
# 5. MENÚS INTERACTIVOS, AGENDA Y BALANCES
# ==========================================
def obtener_menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Tareas", callback_data='menu_tarea'), InlineKeyboardButton("💰 Finanzas", callback_data='menu_finanzas')],
        [InlineKeyboardButton("📅 Agenda Dinámica", callback_data='ver_calendario'), InlineKeyboardButton("⏱️ Enfoque", callback_data='modo_enfoque')]
    ])

async def manejador_botones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS: return
    await query.answer() 

    # --- MENÚ TAREAS ---
    if query.data == 'menu_tarea':
        teclado = [
            [InlineKeyboardButton("➕ Registrar manual", callback_data='tarea_nueva')],
            [InlineKeyboardButton("✅ Completar Pendientes", callback_data='listar_pendientes')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]
        await query.edit_message_text("Módulo de Tareas:", reply_markup=InlineKeyboardMarkup(teclado))
        
    elif query.data == 'listar_pendientes':
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, descripcion, fecha_calculada FROM tareas WHERE estado = 'Pendiente' ORDER BY fecha_calculada ASC LIMIT 8")
        pendientes = c.fetchall()
        release_db_connection(conn)
        
        if not pendientes:
            await query.edit_message_text("🎉 No tienes tareas pendientes. ¡Todo al día!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='menu_tarea')]]))
            return
            
        teclado = [[InlineKeyboardButton(f"✅ {p[1][:20]}...", callback_data=f'completar_t_{p[0]}')] for p in pendientes]
        teclado.append([InlineKeyboardButton("🔙 Volver", callback_data='menu_tarea')])
        await query.edit_message_text("Selecciona una tarea para marcarla como COMPLETADA:", reply_markup=InlineKeyboardMarkup(teclado))
        
    elif query.data.startswith('completar_t_'):
        id_tarea = query.data.split('_')[2]
        conn = get_db_connection()
        c = conn.cursor()
        fecha_hoy = datetime.date.today().strftime('%Y-%m-%d')
        c.execute("UPDATE tareas SET estado = 'Completada', fecha_completado = %s WHERE id = %s", (fecha_hoy, id_tarea))
        conn.commit()
        release_db_connection(conn)
        registrar_auditoria("COMPLETAR_TAREA", f"Se completó la tarea ID {id_tarea}")
        
        await query.edit_message_text("🎉 ¡Excelente! Tarea marcada como completada.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data='volver_inicio')]]))

    # --- MENÚ FINANZAS ---
    elif query.data == 'menu_finanzas':
        teclado = [
            [InlineKeyboardButton("💸 Gasto Manual", callback_data='finanza_gasto'), InlineKeyboardButton("💵 Ingreso Manual", callback_data='finanza_ingreso')],
            [InlineKeyboardButton("✏️ Borrar Recientes", callback_data='editar_recientes')],
            [InlineKeyboardButton("📊 Balance Histórico", callback_data='ver_balance_todo'), InlineKeyboardButton("📅 Mes actual", callback_data='ver_balance_mes')],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]
        await query.edit_message_text("Módulo Financiero:", reply_markup=InlineKeyboardMarkup(teclado))
    
    # AGENDA DINÁMICA DE 7 DÍAS (Solo pendientes)
    elif query.data == 'ver_calendario':
        conn = get_db_connection()
        c = conn.cursor()
        # Solo traemos las tareas que NO están completadas
        c.execute("SELECT descripcion, tipo, prioridad, fecha_calculada, fecha_original FROM tareas WHERE estado = 'Pendiente'")
        tareas = c.fetchall()
        release_db_connection(conn)

        hoy_date = datetime.date.today()
        proximos_7 = [hoy_date + datetime.timedelta(days=i) for i in range(7)]
        dias_esp = {0: 'lunes', 1: 'martes', 2: 'miércoles', 3: 'jueves', 4: 'viernes', 5: 'sábado', 6: 'domingo'}
        
        mensaje = "📅 *AGENDA (Próximos 7 días):*\n\n"
        hay_tareas = False
        
        for dia_d in proximos_7:
            d_str = dia_d.strftime('%Y-%m-%d')
            nombre_dia = dias_esp[dia_d.weekday()]
            n_dia_sin_acento = nombre_dia.replace('é','e').replace('á','a')
            
            tareas_del_dia = []
            for t in tareas:
                f_calc = t[3]
                f_orig = t[4].lower()
                
                if f_calc == d_str:
                    tareas_del_dia.append(t)
                elif f_calc == "Recurrente":
                    if nombre_dia in f_orig or n_dia_sin_acento in f_orig or 'todos los días' in f_orig or 'diario' in f_orig or 'cada' in f_orig:
                        tareas_del_dia.append(t)
                        
            if tareas_del_dia:
                hay_tareas = True
                mensaje += f"🗓️ *{nombre_dia.capitalize()}, {d_str}*\n"
                for t in tareas_del_dia:
                    mensaje += f"   ├ ▫️ {t[0]} ({t[1]})\n"
                mensaje += "\n"
                
        if not hay_tareas: mensaje += "Tu agenda está vacía y al día para esta semana. ¡Buen trabajo!"
        await query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))

    # BALANCES Y PAGOS PENDIENTES
    elif query.data.startswith('ver_balance_'):
        filtro = query.data.split('_')[2]
        conn = get_db_connection()
        c = conn.cursor()
        hoy_str = datetime.date.today().strftime('%Y-%m-%d')
        
        if filtro == 'mes':
            mes_actual = datetime.datetime.now().strftime('%Y-%m')
            c.execute("SELECT tipo, area, monto, concepto, fecha FROM finanzas WHERE fecha LIKE %s ORDER BY fecha ASC", (f"{mes_actual}-%",))
            titulo = f"📊 *BALANCE DEL MES ({mes_actual})*\n\n"
        else:
            c.execute("SELECT tipo, area, monto, concepto, fecha FROM finanzas ORDER BY fecha ASC")
            titulo = "📊 *BALANCE HISTÓRICO*\n\n"
            
        registros = c.fetchall()
        release_db_connection(conn)
        
        ing_emp = gas_emp = ing_per = gas_per = 0
        pendientes = []
        lista_gastos = []
        
        for r in registros:
            r_tipo, r_area, r_monto, r_concepto, r_fecha = r[0], r[1], r[2], r[3], r[4]
            if r_fecha > hoy_str and r_tipo == 'Gasto':
                pendientes.append(r)
            elif r_fecha <= hoy_str: 
                if r_tipo == 'Ingreso' and r_area == 'Empresa': ing_emp += r_monto
                elif r_tipo == 'Gasto' and r_area == 'Empresa': gas_emp += r_monto
                elif r_tipo == 'Ingreso' and r_area == 'Personal': ing_per += r_monto
                elif r_tipo == 'Gasto' and r_area == 'Personal': gas_per += r_monto
                if r_tipo == 'Gasto': lista_gastos.append(r)
                
        mensaje = titulo + f"🏢 *EMPRESA:*\n   ├ 🟢 Ingreso: ${ing_emp:,.2f}\n   ├ 🔴 Gasto: ${gas_emp:,.2f}\n   └ ⚖️ Balance: ${(ing_emp - gas_emp):,.2f}\n\n"
        mensaje += f"👤 *PERSONAL:*\n   ├ 🟢 Ingreso: ${ing_per:,.2f}\n   ├ 🔴 Gasto: ${gas_per:,.2f}\n   └ ⚖️ Balance: ${(ing_per - gas_per):,.2f}\n\n"
        
        if lista_gastos:
            mensaje += "📋 *Últimos Gastos Realizados:*\n"
            for g in lista_gastos[-10:]:
                mensaje += f"   ▫️ {g[3]} - ${g[2]:,.2f} ({g[4]})\n"
                
        if pendientes:
            mensaje += "\n⏳ *PAGOS PENDIENTES (Futuros):*\n"
            for p in pendientes:
                mensaje += f"   🔸 {p[3]} - ${p[2]:,.2f} ({p[4]})\n"
                
        await query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='menu_finanzas')]]))

    # APRENDIZAJE INTERACTIVO
    elif query.data.startswith('aprender_'):
        categoria = "Empresa" if query.data == 'aprender_empresa' else "Personal"
        datos = context.user_data.get('pendiente_clasificar')
        if datos:
            palabra_nueva = datos['concepto'] if datos['tipo_registro'] == 'finanza' else datos['descripcion']
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("INSERT INTO diccionario (palabra, categoria) VALUES (%s, %s) ON CONFLICT (palabra) DO NOTHING", (palabra_nueva, categoria))
            conn.commit()
            release_db_connection(conn)
            
            datos['area' if datos['tipo_registro'] == 'finanza' else 'tipo'] = categoria
            await query.edit_message_text(f"🧠 ¡Aprendido! *'{palabra_nueva}'* ahora se registrará como *{categoria}*.", parse_mode='Markdown')
            await guardar_datos_nlp(context.bot, MI_TELEGRAM_ID, datos)
            context.user_data.pop('pendiente_clasificar', None)
            
    # EDITAR / BORRAR (Con Auditoría)
    elif query.data == 'editar_recientes':
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, concepto, monto, fecha FROM finanzas ORDER BY id DESC LIMIT 5")
        recientes = c.fetchall()
        release_db_connection(conn)
        
        teclado = [[InlineKeyboardButton(f"❌ {r[3]} - {r[1][:15]} (${r[2]})", callback_data=f'borrar_f_{r[0]}')] for r in recientes]
        teclado.append([InlineKeyboardButton("🔙 Volver", callback_data='menu_finanzas')])
        await query.edit_message_text("Selecciona un registro para ELIMINARLO:", reply_markup=InlineKeyboardMarkup(teclado))

    elif query.data.startswith('borrar_f_'):
        id_borrar = query.data.split('_')[2]
        conn = get_db_connection()
        c = conn.cursor()
        
        # Primero obtenemos info del registro para guardarlo en la auditoría
        c.execute("SELECT concepto, monto FROM finanzas WHERE id = %s", (id_borrar,))
        info = c.fetchone()
        
        c.execute("DELETE FROM finanzas WHERE id = %s", (id_borrar,))
        conn.commit()
        release_db_connection(conn)
        
        if info: registrar_auditoria("ELIMINAR_FINANZA", f"Eliminó ID {id_borrar}: {info[0]} (${info[1]})")
        await query.edit_message_text("✅ Registro eliminado de forma segura.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al Menú", callback_data='volver_inicio')]]))

    # MODO ENFOQUE
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
    
    # VOLVER INICIO
    elif query.data == 'volver_inicio':
        await query.edit_message_text("Menú Principal:", reply_markup=obtener_menu_principal())

# ==========================================
# 6. ENTRADA DE DATOS NLP, AUDIO Y FOTOS
# ==========================================
async def guardar_datos_nlp(bot, chat_id, datos):
    conn = get_db_connection()
    c = conn.cursor()
    if datos['tipo_registro'] == 'tarea':
        c.execute("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, notas, estado) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                  (datos['descripcion'], datos['tipo'], datos['prioridad'], datos['fecha_original'], datos['fecha_calculada'], datos['notas'], 'Pendiente'))
        mensaje = f"✅ *Tarea guardada*\n📌 {datos['descripcion']}\n⏰ Para: {datos['fecha_calculada']} ({datos['tipo']})"
    elif datos['tipo_registro'] == 'finanza':
        c.execute("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha, categoria) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                  (datos['tipo'], datos['area'], datos['concepto'], datos['monto'], datos['metodo'], datos['fecha'], 'General'))
        mensaje = f"✅ *Finanza guardada*\n💰 {datos['tipo']}: ${datos['monto']} en {datos['concepto']} ({datos['area']})"
    conn.commit()
    release_db_connection(conn)
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
    m = await update.message.reply_text("🧠 *Analizando...*", parse_mode='Markdown')
    datos = analizar_texto_local(update.message.text)
    await manejar_resultado_analisis(update, context, datos)
    await m.delete()

async def procesar_audio_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("🎙️ *Escuchando y Transcribiendo...*", parse_mode='Markdown')
    try:
        archivo = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_audio:
            await archivo.download_to_drive(temp_audio.name)
            wav_path = temp_audio.name.replace(".ogg", ".wav")
            AudioSegment.from_ogg(temp_audio.name).export(wav_path, format="wav")
            
            r = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                texto = r.recognize_google(r.record(source), language="es-MX")
            
            await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"🗣️ _\"{texto}\"_", parse_mode='Markdown')
            datos = analizar_texto_local(texto)
            await manejar_resultado_analisis(update, context, datos)
            os.remove(temp_audio.name)
            os.remove(wav_path)
    except Exception:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="⚠️ Audio no reconocido. Intenta con texto o usa el menú manual.")
    finally:
        await m.delete()

async def procesar_foto_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("📸 *Escaneando ticket con OCR...*", parse_mode='Markdown')
    try:
        archivo = await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as temp_foto:
            await archivo.download_to_drive(temp_foto.name)
            texto = pytesseract.image_to_string(Image.open(temp_foto.name), lang='spa')
            datos = analizar_texto_local(f"ticket pagué {texto}")
            await manejar_resultado_analisis(update, context, datos)
            os.remove(temp_foto.name)
    except Exception:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="⚠️ Foto no reconocida. Usa registro manual.")
    finally:
        await m.delete()

# ==========================================
# 7. FLUJOS DE REGISTRO MANUAL COMPLETOS
# ==========================================
async def cancelar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Registro cancelado.", reply_markup=obtener_menu_principal())
    return ConversationHandler.END

# ---- TAREAS MANUAL ----
async def iniciar_registro_tarea(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['nueva_tarea'] = {}
    await u.callback_query.edit_message_text("📝 *Descripción:*", parse_mode='Markdown')
    return T_DESCRIPCION
async def capturar_descripcion(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    c.user_data['nueva_tarea']['descripcion'] = u.message.text
    await u.message.reply_text("📂 *Categoría:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Empresa", callback_data='tipo_empresa'), InlineKeyboardButton("Personal", callback_data='tipo_personal')]]), parse_mode='Markdown')
    return T_TIPO
async def capturar_tipo(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['nueva_tarea']['tipo'] = "Empresa" if u.callback_query.data == 'tipo_empresa' else "Personal"
    await u.callback_query.edit_message_text("⚡ *Prioridad:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Urgente", callback_data='prio_urgente'), InlineKeyboardButton("Rutina", callback_data='prio_rutina')]]), parse_mode='Markdown')
    return T_PRIORIDAD
async def capturar_prioridad(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['nueva_tarea']['prioridad'] = "Urgente" if u.callback_query.data == 'prio_urgente' else "Rutina"
    await u.callback_query.edit_message_text("📅 *Fecha* (Ej. Mañana, Lunes, 25 Nov):", parse_mode='Markdown')
    return T_FECHA
async def capturar_fecha(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    f_calc = dateparser.parse(u.message.text, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    c.user_data['nueva_tarea']['fecha_original'] = u.message.text
    c.user_data['nueva_tarea']['fecha_calculada'] = f_calc.strftime('%Y-%m-%d') if f_calc else "Recurrente"
    await u.message.reply_text("🗒️ *Notas opcionales:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Saltar", callback_data='sin_notas')]]), parse_mode='Markdown')
    return T_NOTAS
async def capturar_notas(u: Update, cx: ContextTypes.DEFAULT_TYPE) -> int:
    tarea = cx.user_data['nueva_tarea']
    notas = "Ninguna" if u.callback_query else u.message.text
    conn = get_db_connection()
    conn.cursor().execute("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, notas, estado) VALUES (%s, %s, %s, %s, %s, %s, %s)",
              (tarea['descripcion'], tarea['tipo'], tarea['prioridad'], tarea['fecha_original'], tarea['fecha_calculada'], notas, 'Pendiente'))
    conn.commit()
    release_db_connection(conn)
    if u.callback_query: await u.callback_query.edit_message_text("✅ *Tarea guardada*", parse_mode='Markdown', reply_markup=obtener_menu_principal())
    else: await u.message.reply_text("✅ *Tarea guardada*", parse_mode='Markdown', reply_markup=obtener_menu_principal())
    return ConversationHandler.END

# ---- FINANZAS MANUAL ----
async def iniciar_finanzas(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['nueva_finanza'] = {'tipo': "Gasto" if u.callback_query.data == 'finanza_gasto' else "Ingreso"}
    await u.callback_query.edit_message_text("💰 *¿Empresa o Personal?*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Empresa", callback_data='f_empresa'), InlineKeyboardButton("Personal", callback_data='f_personal')]]), parse_mode='Markdown')
    return F_AREA
async def capturar_f_area(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['nueva_finanza']['area'] = "Empresa" if u.callback_query.data == 'f_empresa' else "Personal"
    await u.callback_query.edit_message_text("📝 *Concepto:*", parse_mode='Markdown')
    return F_CONCEPTO
async def capturar_f_concepto(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    c.user_data['nueva_finanza']['concepto'] = u.message.text
    await u.message.reply_text("💵 *Monto numérico:*", parse_mode='Markdown')
    return F_MONTO
async def capturar_f_monto(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    try: c.user_data['nueva_finanza']['monto'] = float(u.message.text)
    except ValueError:
        await u.message.reply_text("⚠️ Solo ingresa números:")
        return F_MONTO 
    if c.user_data['nueva_finanza']['tipo'] == "Gasto":
        await u.message.reply_text("💳 *Método de Pago:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Tarjeta", callback_data='f_tarjeta'), InlineKeyboardButton("Efectivo", callback_data='f_efectivo')]]), parse_mode='Markdown')
        return F_METODO
    c.user_data['nueva_finanza']['metodo'] = "N/A"
    await u.message.reply_text("📅 *Fecha:*", parse_mode='Markdown')
    return F_FECHA
async def capturar_f_metodo(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['nueva_finanza']['metodo'] = "Tarjeta" if u.callback_query.data == 'f_tarjeta' else "Efectivo"
    await u.callback_query.edit_message_text("📅 *Fecha:*", parse_mode='Markdown')
    return F_FECHA
async def capturar_f_fecha(u: Update, cx: ContextTypes.DEFAULT_TYPE) -> int:
    f = cx.user_data['nueva_finanza']
    fc = dateparser.parse(u.message.text, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    ff = fc.strftime('%Y-%m-%d') if fc else datetime.datetime.now().strftime('%Y-%m-%d')
    conn = get_db_connection()
    conn.cursor().execute("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha, categoria) VALUES (%s, %s, %s, %s, %s, %s, %s)", (f['tipo'], f['area'], f['concepto'], f['monto'], f['metodo'], ff, 'General'))
    conn.commit()
    release_db_connection(conn)
    await u.message.reply_text("✅ *Finanza guardada*", parse_mode='Markdown', reply_markup=obtener_menu_principal())
    return ConversationHandler.END

# ==========================================
# 8. ALERTAS DIARIAS
# ==========================================
async def alerta_matutina(context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.date.today().strftime('%Y-%m-%d')
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT descripcion, tipo, prioridad FROM tareas WHERE fecha_calculada = %s AND estado = 'Pendiente'", (hoy,))
    tareas_hoy = c.fetchall()
    release_db_connection(conn)

    mensaje = "☀️ *Buenos días.* Aquí está tu lista para hoy:\n\n"
    if not tareas_hoy:
        mensaje += "No tienes actividades programadas. ¡Aprovecha el día!"
    else:
        for t in tareas_hoy: mensaje += f"▫️ {t[0]} ({t[1]} - {t[2]})\n"
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=mensaje, parse_mode='Markdown')

async def alerta_nocturna(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="🌙 *Corte de caja.* \nMomento de revisar pendientes y desconectar. ¿Cómo te fue hoy?", parse_mode='Markdown')

# ==========================================
# 9. INICIO Y ARRANQUE DE LA APP
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in ALLOWED_USERS: return
    await update.message.reply_text("¡Hola! Háblame, mándame fotos, audios o usa el menú manual para registrar tu información:", reply_markup=obtener_menu_principal())

if __name__ == '__main__':
    if not TOKEN or not DATABASE_URL or MI_TELEGRAM_ID == 0:
        print("CRÍTICO: Faltan las variables de entorno en Render.")
        exit(1)
        
    init_db_pool()
    init_db()
    
    threading.Thread(target=iniciar_servidor, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()

    zona = ZoneInfo('America/Monterrey')
    app.job_queue.run_daily(alerta_matutina, time=datetime.time(hour=6, minute=0, tzinfo=zona))
    app.job_queue.run_daily(alerta_nocturna, time=datetime.time(hour=19, minute=0, tzinfo=zona))

    conv_handler_tarea = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_registro_tarea, pattern='^tarea_nueva$')],
        states={T_DESCRIPCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_descripcion)], T_TIPO: [CallbackQueryHandler(capturar_tipo, pattern='^tipo_')], T_PRIORIDAD: [CallbackQueryHandler(capturar_prioridad, pattern='^prio_')], T_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_fecha)], T_NOTAS: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_notas), CallbackQueryHandler(capturar_notas, pattern='^sin_notas$')]},
        fallbacks=[CommandHandler('cancelar', cancelar_registro)], per_message=False
    )

    conv_handler_finanzas = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_finanzas, pattern='^finanza_gasto$|^finanza_ingreso$')],
        states={F_AREA: [CallbackQueryHandler(capturar_f_area, pattern='^f_')], F_CONCEPTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_concepto)], F_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_monto)], F_METODO: [CallbackQueryHandler(capturar_f_metodo, pattern='^f_')], F_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_fecha)]},
        fallbacks=[CommandHandler('cancelar', cancelar_registro)], per_message=False
    )

    app.add_handler(conv_handler_tarea)
    app.add_handler(conv_handler_finanzas)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(manejador_botones)) 
    
    app.add_handler(MessageHandler(filters.VOICE, procesar_audio_local))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_foto_local))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_texto_libre)) 

    print("🤖 Súper Bot V9 INICIADO: Pool DB + Estados de Tareas implementados.")
    app.run_polling()
