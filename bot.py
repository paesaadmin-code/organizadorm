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
    nlp_spacy = spacy.load("es_core_news_md")
except Exception as e:
    logging.warning(f"No spaCy: {e}. Se usará NLP heurístico de respaldo.")
    nlp_spacy = None

# ==========================================
# 1. CONFIGURACIÓN Y MENSAJES
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
MI_TELEGRAM_ID = int(os.getenv("TELEGRAM_ID", 0))
ALLOWED_USERS = [MI_TELEGRAM_ID]

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

T_DESCRIPCION, T_TIPO, T_PRIORIDAD, T_FECHA, T_NOTAS = range(5)
F_AREA, F_CONCEPTO, F_MONTO, F_METODO, F_FECHA = range(5, 10)
E_CAMPO, E_VALOR = range(10, 12)

MSG_EXITO_TAREA = "✅ *{cat_op} Registrada*\n📌 {desc}\n⏰ Fecha: {fecha}\n📂 Área: {area}"
MSG_EXITO_FINANZA = "💳 *Registro Financiero*\n🏷️ {concepto}\n💲 ${monto:,.2f}\n📊 Cat: {cat} | Cuenta: {cuenta}\n📅 Fecha: {fecha} ({estado}){alerta}"
MSG_ERROR = "⚠️ Hubo un error. Intenta con texto o usa el menú manual."
MSG_AUDIO_LARGO = "⚠️ El audio es muy largo. Máximo 60 segundos por límite de memoria."

# ==========================================
# 2. SERVIDOR FANTASMA
# ==========================================
class ServidorFantasma(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok" if self.path == "/health" else b"Bot Ejecutivo V28 Activo!")

def iniciar_servidor():
    HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 10000))), ServidorFantasma).serve_forever()

# ==========================================
# 3. BASE DE DATOS Y PERSISTENCIA
# ==========================================
db_pool = None
CACHE_DICCIONARIO, CACHE_CUENTAS, CACHE_CATEGORIAS = {'EMPRESA': {}, 'PERSONAL': {}}, {}, {}

def init_db_pool():
    global db_pool
    if DATABASE_URL: db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def ejecutar_sql(query, params=None, fetch_all=False, fetch_one=False, commit=True):
    if not db_pool: return None
    conn, c, res = db_pool.getconn(), None, None
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
    tablas = [
        "tareas (id SERIAL PRIMARY KEY, descripcion TEXT, tipo TEXT, prioridad TEXT, fecha_original TEXT, fecha_calculada TEXT, notas TEXT, estado TEXT DEFAULT 'Pendiente', fecha_completado TEXT, categoria_operativa TEXT DEFAULT 'TAREA')",
        "finanzas (id SERIAL PRIMARY KEY, tipo TEXT, area TEXT, concepto TEXT, monto REAL, metodo TEXT, fecha TEXT, categoria TEXT DEFAULT 'General', subcategoria TEXT DEFAULT 'General', cuenta TEXT DEFAULT 'Efectivo', estado TEXT DEFAULT 'Completado', es_recordatorio BOOLEAN DEFAULT FALSE)",
        "diccionario (palabra TEXT PRIMARY KEY, categoria TEXT)",
        "historial_operaciones (id SERIAL PRIMARY KEY, tabla TEXT, registro_id INTEGER, accion TEXT, fecha TEXT)",
        "config_cuentas (nombre TEXT PRIMARY KEY, saldo REAL DEFAULT 0)",
        "config_categorias (nombre TEXT PRIMARY KEY, tipo TEXT, palabras_clave TEXT DEFAULT '')",
        "presupuestos (categoria TEXT PRIMARY KEY, limite REAL)",
        "errores (id SERIAL PRIMARY KEY, fecha TEXT, modulo TEXT, error TEXT)"
    ]
    for t in tablas: ejecutar_sql(f"CREATE TABLE IF NOT EXISTS {t}")
    for idx in ["idx_tareas_fecha ON tareas(fecha_calculada)", "idx_tareas_estado ON tareas(estado)", "idx_finanzas_fecha ON finanzas(fecha)", "idx_finanzas_estado ON finanzas(estado)", "idx_finanzas_recordatorio ON finanzas(es_recordatorio)"]:
        ejecutar_sql(f"CREATE INDEX IF NOT EXISTS {idx}")
    
    if not ejecutar_sql("SELECT 1 FROM config_cuentas LIMIT 1", fetch_one=True):
        for c in ['Nu', 'BBVA', 'Banorte', 'Efectivo']: ejecutar_sql("INSERT INTO config_cuentas (nombre, saldo) VALUES (%s, 0)", (c,))
    if not ejecutar_sql("SELECT 1 FROM config_categorias LIMIT 1", fetch_one=True):
        cats = [('Servicios', 'Gasto', 'LUZ,AGUA,INTERNET,CFE,TELCEL,RENTA,GAS,TELÉFONO'), ('Transporte', 'Gasto', 'UBER,TAXI,GASOLINA,VUELO,CASETA,ESTACIONAMIENTO'), ('Alimentación', 'Gasto', 'DESPENSA,SUPER,RESTAURANTE,COMIDA,OXXO,WALMART,SORIANA,CENA'), ('Nómina', 'Ingreso', 'SUELDO,SALARIO,QUINCENA,PAGO')]
        for c, t, p in cats: ejecutar_sql("INSERT INTO config_categorias (nombre, tipo, palabras_clave) VALUES (%s, %s, %s)", (c, t, p))
    recargar_cache()

def recargar_cache():
    global CACHE_DICCIONARIO, CACHE_CUENTAS, CACHE_CATEGORIAS
    filas_dicc = ejecutar_sql("SELECT palabra, categoria FROM diccionario", fetch_all=True) or []
    CACHE_DICCIONARIO['EMPRESA'] = {f[0].upper(): 3 for f in filas_dicc if f[1] == 'Empresa'}
    CACHE_DICCIONARIO['PERSONAL'] = {f[0].upper(): 3 for f in filas_dicc if f[1] == 'Personal'}
    
    CACHE_CUENTAS.clear()
    for f in (ejecutar_sql("SELECT nombre FROM config_cuentas", fetch_all=True) or []): CACHE_CUENTAS[f[0].upper()] = f[0]
    
    CACHE_CATEGORIAS.clear()
    for nom, tip, pals in (ejecutar_sql("SELECT nombre, tipo, palabras_clave FROM config_categorias", fetch_all=True) or []):
        CACHE_CATEGORIAS[nom.upper()] = nom
        if pals: 
            for p in pals.split(','): CACHE_CATEGORIAS[p.strip().upper()] = nom

def sincronizar_saldos_db():
    ejecutar_sql("UPDATE config_cuentas SET saldo = 0")
    for tipo, monto, cuenta in (ejecutar_sql("SELECT tipo, monto, cuenta FROM finanzas WHERE estado='Completado' AND es_recordatorio = FALSE", fetch_all=True) or []):
        ejecutar_sql(f"UPDATE config_cuentas SET saldo = saldo {'+' if tipo == 'Ingreso' else '-'} %s WHERE LOWER(nombre) = LOWER(%s)", (monto, cuenta))

def registrar_operacion(tabla, registro_id, accion):
    ejecutar_sql("INSERT INTO historial_operaciones (tabla, registro_id, accion, fecha) VALUES (%s, %s, %s, %s)", (tabla, registro_id, accion, datetime.datetime.now(ZoneInfo('America/Monterrey')).isoformat()))

def registrar_error(modulo, error_msg):
    ejecutar_sql("INSERT INTO errores (fecha, modulo, error) VALUES (%s, %s, %s)", (datetime.datetime.now(ZoneInfo('America/Monterrey')).strftime('%Y-%m-%d %H:%M:%S'), modulo, error_msg))
    ejecutar_sql("DELETE FROM errores WHERE id IN (SELECT id FROM errores ORDER BY id DESC OFFSET 5000)")

# ==========================================
# 4. BACKUP Y DESHACER
# ==========================================
async def generar_y_enviar_backup(context: ContextTypes.DEFAULT_TYPE):
    conn, c, archivos = None, None, []
    try:
        conn = get_db_connection()
        c = conn.cursor()
        for tabla in ['finanzas', 'tareas', 'presupuestos', 'errores']:
            c.execute(f"SELECT * FROM {tabla}")
            columnas = [desc[0] for desc in c.description]
            path_temp = f"/tmp/backup_{tabla}.csv"
            with open(path_temp, mode='w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(columnas)
                writer.writerows(c.fetchall())
            archivos.append(path_temp)
    except Exception as e: registrar_error("BACKUP", str(e))
    finally:
        if c: c.close()
        if conn: db_pool.putconn(conn)
    if archivos:
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="📦 *Respaldo Generado:*", parse_mode='Markdown')
        for file_path in archivos:
            with open(file_path, 'rb') as f: await context.bot.send_document(chat_id=MI_TELEGRAM_ID, document=f)
            os.remove(file_path)

async def comando_exportar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("⏳ Generando reporte...")
    await generar_y_enviar_backup(context)
    try: await m.delete()
    except: pass

async def comando_deshacer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    ultimo = ejecutar_sql("SELECT id, tabla, registro_id, accion FROM historial_operaciones ORDER BY id DESC LIMIT 1", fetch_one=True)
    if not ultimo: return await update.message.reply_text("⚠️ No hay registros recientes para deshacer.")
    
    h_id, tabla, reg_id, accion = ultimo
    if tabla == 'finanzas':
        info = ejecutar_sql("SELECT tipo, monto, cuenta, estado, es_recordatorio, concepto FROM finanzas WHERE id = %s", (reg_id,), fetch_one=True)
        if info:
            if accion == 'INSERT':
                ejecutar_sql("DELETE FROM finanzas WHERE id = %s", (reg_id,))
                sincronizar_saldos_db()
                await update.message.reply_text(f"✅ Se borró el registro financiero: '{info[5]}'. Saldos revertidos.")
            elif accion == 'PAGO_REALIZADO':
                ejecutar_sql("UPDATE finanzas SET estado = 'Pendiente' WHERE id = %s", (reg_id,))
                sincronizar_saldos_db()
                await update.message.reply_text(f"✅ El pago '{info[5]}' volvió a estado Pendiente. Saldo revertido.")
    else:
        info = ejecutar_sql("SELECT descripcion FROM tareas WHERE id = %s", (reg_id,), fetch_one=True)
        if accion == 'INSERT':
            ejecutar_sql("DELETE FROM tareas WHERE id = %s", (reg_id,))
            if info: await update.message.reply_text(f"✅ Se borró la tarea/rutina: '{info[0]}'.")
    ejecutar_sql("DELETE FROM historial_operaciones WHERE id = %s", (h_id,))

# ==========================================
# 5. NLP INTELIGENTE Y EXTRACCIÓN (spaCy)
# ==========================================
PALABRAS_EMPRESA_BASE = {'VIÁTICO': 3, 'VUELO': 2, 'GASOLINA': 2, 'UBER': 2, 'CLIENTE': 3, 'EMPRESA': 3, 'PROVEEDOR': 3, 'SAT': 3}
PALABRAS_PERSONAL_BASE = {'DESPENSA': 3, 'LUZ': 3, 'AGUA': 3, 'INTERNET': 3, 'CFE': 3, 'RENTA': 3, 'CASA': 2, 'MÉDICO': 2, 'PERSONAL': 3, 'ROPA': 2}
PALABRAS_GASTO = ['PAGUÉ', 'PAGUE', 'COMPRÉ', 'COMPRE', 'GASTÉ', 'GASTE', 'COSTÓ', 'PAGAR', 'TICKET', 'RECIBO']
PALABRAS_INGRESO = ['DEPOSITÉ', 'DEPOSITE', 'TRANSFERÍ', 'COBRÉ', 'COBRE', 'SUELDO', 'PAGARON', 'INGRESO']
STOPWORDS = PALABRAS_GASTO + PALABRAS_INGRESO + ['CUANTO', 'QUÉ', 'DIME', 'TENGO', 'QUE', 'Y', 'SON', 'ES', 'VOY', 'VA', 'HACER', 'ME', 'LE', 'SE', 'LO', 'DEL', 'AL', 'EL', 'LA', 'LOS', 'LAS', 'UN', 'UNA', 'MAÑANA', 'HOY', 'AYER', 'LUNES', 'MARTES', 'MIERCOLES', 'JUEVES', 'VIERNES', 'SABADO', 'DOMINGO', 'EN', 'PARA', 'DE', 'A', 'POR', 'CON', 'PESOS', 'MXN', 'ESTE', 'ESTA', 'YA', 'DÍA', 'DIA', 'CADA', 'TODOS']

def extraer_entidades_spacy(texto):
    if not nlp_spacy: return None, None
    doc = nlp_spacy(texto)
    verbos = [token.lemma_.upper() for token in doc if token.pos_ == "VERB"]
    objetos = [token.text for token in doc if token.dep_ in ('obj', 'nsubj', 'obl', 'flat') and not token.is_stop]
    return verbos, " ".join(objetos).title() if objetos else None

def obtener_palabra_aprendizaje(texto):
    if not texto: return None
    try:
        if nlp_spacy:
            _, objeto = extraer_entidades_spacy(texto)
            if objeto:
                palabras_objeto = re.findall(r'\b[A-ZÁÉÍÓÚÑ]+\b', objeto.upper())
                for palabra in palabras_objeto:
                    if palabra not in {'PAGAR', 'PAGO', 'FACTURA', 'COMPRA', 'GENERAL'}:
                        return palabra
    except Exception: pass

    palabras_invalidas = {'DIA','HOY','AYER','MAÑANA','PASADO','PAGAR','PAGUE','COMPRE','GASTE','FACTURA','COMPRA','DEPOSITO','TRANSFERI','GENERAL'}
    palabras = re.findall(r'\b[A-ZÁÉÍÓÚÑ]+\b', texto.upper())
    palabras = [p for p in palabras if len(p) >= 3 and p not in palabras_invalidas]
    return palabras[0] if palabras else None

def procesar_impacto_financiero(monto, tipo, cuenta, subcat, estado):
    if estado != 'Completado' or monto == 0.0: return ""
    sincronizar_saldos_db()
    if tipo == "Gasto":
        limite = ejecutar_sql("SELECT limite FROM presupuestos WHERE LOWER(categoria) = %s", (subcat.lower(),), fetch_one=True)
        if limite:
            gastado = ejecutar_sql("SELECT SUM(monto) FROM finanzas WHERE subcategoria = %s AND fecha LIKE %s AND estado = 'Completado' AND es_recordatorio = FALSE", (subcat, f"{datetime.datetime.now().strftime('%Y-%m')}-%"), fetch_one=True)[0] or 0
            if gastado >= limite[0] * 0.85: return f"\n⚠️ *¡Presupuesto al {(gastado / limite[0]) * 100:.0f}%!* Has gastado ${gastado:,.0f} de ${limite[0]:,.0f} en {subcat}."
    return ""

def extraer_fecha(texto):
    fechas = search_dates(texto, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    return fechas[0][1].strftime('%Y-%m-%d') if fechas else None

def calcular_siguiente_rutina(texto, fecha_actual_str):
    f_actual, texto_upper = datetime.datetime.strptime(fecha_actual_str, "%Y-%m-%d"), texto.upper()
    dias = {'LUNES':0, 'MARTES':1, 'MIÉRCOLES':2, 'MIERCOLES':2, 'JUEVES':3, 'VIERNES':4, 'SÁBADO':5, 'SABADO':5, 'DOMINGO':6}
    for d_str, d_idx in dias.items():
        if d_str in texto_upper:
            d_adelante = (d_idx - f_actual.weekday() + 7) % 7
            return (f_actual + datetime.timedelta(days=d_adelante if d_adelante != 0 else 7)).strftime('%Y-%m-%d')
    return (f_actual + datetime.timedelta(days=7 if 'SEMANA' in texto_upper else 30 if 'MES' in texto_upper else 1)).strftime('%Y-%m-%d')

def responder_consulta_inteligente(texto_min):
    resp, f_fecha, f_str = "🤖 *Respuesta Analítica:*\n\n", "", "históricamente"
    if "este año" in texto_min: f_fecha, f_str = f"AND fecha LIKE '{datetime.datetime.now().strftime('%Y')}-%'", "este año"
    elif "este mes" in texto_min: f_fecha, f_str = f"AND fecha LIKE '{datetime.datetime.now().strftime('%Y-%m')}-%'", "este mes"
    
    if any(k in texto_min for k in ["dinero", "saldo", "cuentas"]):
        res = ejecutar_sql("SELECT nombre, saldo FROM config_cuentas ORDER BY saldo DESC", fetch_all=True)
        return resp + f"💰 *Saldos Actuales:*\n" + "".join([f"▫️ {r[0]}: ${r[1]:,.2f}\n" for r in res])
    elif "gasté en" in texto_min or "gaste en" in texto_min:
        palabra = re.sub(r'(este año|este mes|cuanto|cuánto)', '', texto_min.split("en ")[-1]).strip().replace("?", "")
        res = ejecutar_sql(f"SELECT SUM(monto) FROM finanzas WHERE tipo = 'Gasto' AND estado = 'Completado' AND es_recordatorio = FALSE {f_fecha} AND (LOWER(concepto) LIKE %s OR LOWER(subcategoria) LIKE %s)", (f"%{palabra}%", f"%{palabra}%"), fetch_one=True)
        return resp + f"Has gastado **${(res[0] or 0):,.2f}** en '{palabra.capitalize()}' {f_str}."
    return False

def analizar_texto_local(texto):
    texto_upper, texto_min = texto.upper(), texto.lower()
    PATRONES_CONSULTA = ['cuanto', 'cuánto', 'que', 'qué', 'dime', 'muéstrame', 'muestrame', 'cual', 'cuál', 'saldo', 'gaste', 'gasté', 'balance', 'cuentas', 'promedio', 'pendientes']
    
    if any(patron in texto_min for patron in PATRONES_CONSULTA):
        resp = responder_consulta_inteligente(texto_min)
        if resp: return {"tipo_registro": "consulta", "respuesta": resp}

    dict_emp, dict_per = {**PALABRAS_EMPRESA_BASE, **CACHE_DICCIONARIO.get('EMPRESA', {})}, {**PALABRAS_PERSONAL_BASE, **CACHE_DICCIONARIO.get('PERSONAL', {})}
    score_emp, score_per = sum(p for w, p in dict_emp.items() if w in texto_upper), sum(p for w, p in dict_per.items() if w in texto_upper)
    area = "Desconocida" if score_emp == 0 and score_per == 0 else ("Empresa" if score_emp > score_per else "Personal")
    
    es_gasto, es_ingreso = any(p in texto_upper for p in PALABRAS_GASTO), any(p in texto_upper for p in PALABRAS_INGRESO)
    match_monto = re.search(r'\$?\s*([\d,]+(?:\.\d{1,2})?)\b', texto_upper)
    hoy_str = datetime.date.today().strftime('%Y-%m-%d')
    
    verbos_spacy, objeto_spacy = extraer_entidades_spacy(texto)
    palabra_limpia = re.sub(r'\$?\s*([\d,]+(?:\.\d{1,2})?)\b', '', texto_upper).strip()
    palabra_limpia = re.sub(r'[^\w\s]', ' ', palabra_limpia)
    for w in STOPWORDS + list(CACHE_CUENTAS.keys()): palabra_limpia = re.sub(rf'\b{w}\b', '', palabra_limpia).strip()
    concepto_final = objeto_spacy if objeto_spacy else (palabra_limpia.title() or "Registro general")
    
    if es_gasto or es_ingreso or match_monto or (verbos_spacy and any(v in ['PAGAR', 'COMPRAR', 'DEPOSITAR', 'COBRAR', 'GASTAR'] for v in verbos_spacy)):
        tipo_finanza = "Ingreso" if es_ingreso and not es_gasto else "Gasto"
        monto = float(match_monto.group(1).replace(',', '')) if match_monto else 0.0
        fecha_calc = extraer_fecha(texto) or hoy_str
        cuenta_detectada = 'Efectivo'
        for c_key, c_orig in CACHE_CUENTAS.items(): 
            if c_key in texto_upper: cuenta_detectada = c_orig; break
        subcat = 'General'
        for c_key, c_orig in CACHE_CATEGORIAS.items():
            if c_key in texto_upper: subcat = c_orig; break
        return {
            "tipo_registro": "finanza", "tipo": tipo_finanza, "area": area, 
            "concepto": concepto_final[:40], "monto": monto, "metodo": cuenta_detectada, "fecha": fecha_calc, 
            "estado": 'Pendiente' if (fecha_calc > hoy_str or monto == 0.0) else 'Completado', "subcategoria": subcat, 
            "es_recordatorio": (monto == 0.0), "palabra_clave": palabra_limpia
        }
    
    es_rutina = bool(re.search(r'(CADA|TODOS LOS|DIARIO|SIEMPRE)', texto_upper))
    return {
        "tipo_registro": "tarea", "descripcion": texto[:60].capitalize(), "tipo": area, "prioridad": "Urgente" if "URGENTE" in texto_upper else "Rutina", 
        "fecha_original": texto, "fecha_calculada": extraer_fecha(texto) or hoy_str, "categoria_operativa": "RUTINA" if es_rutina else "TAREA", "palabra_clave": palabra_limpia
    }

# ==========================================
# 6. ENTRADA Y FLUJO DE DATOS
# ==========================================
async def guardar_datos_nlp(bot, chat_id, datos, context=None):
    if datos.get('tipo_registro') == 'consulta':
        return await bot.send_message(chat_id=chat_id, text=datos['respuesta'], parse_mode='Markdown')

    if datos.get('monto', 0) > 10000 and not datos.get('confirmado'):
        datos['tipo_registro_original'], datos['tipo_registro'] = datos['tipo_registro'], 'confirmacion_alta'
        if context: context.user_data['pending_alta'] = datos
        return await bot.send_message(chat_id=chat_id, text=f"⚠️ *Atención:* Movimiento por **${datos['monto']:,.2f}**. ¿Confirmas?", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Confirmar ${datos['monto']:,.2f}", callback_data="confirmar_alto")]]))

    if datos['tipo_registro'] == 'tarea':
        res = ejecutar_sql("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, categoria_operativa) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                  (datos['descripcion'], datos['tipo'], datos['prioridad'], datos['fecha_original'], datos['fecha_calculada'], datos['categoria_operativa']), fetch_one=True)
        if res: registrar_operacion('tareas', res[0], 'INSERT')
        msg = MSG_EXITO_TAREA.format(cat_op=datos['categoria_operativa'], desc=datos['descripcion'], fecha=datos['fecha_calculada'], area=datos['tipo'])
        
    elif datos['tipo_registro'] == 'finanza':
        est, subcat, cta, es_rec = datos.get('estado', 'Completado'), datos.get('subcategoria', 'General'), datos.get('metodo', 'Efectivo'), datos.get('es_recordatorio', False)
        res = ejecutar_sql("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha, categoria, estado, subcategoria, cuenta, es_recordatorio) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                  (datos['tipo'], datos['area'], datos['concepto'], datos['monto'], cta, datos['fecha'], 'General', est, subcat, cta, es_rec), fetch_one=True)
        if res: registrar_operacion('finanzas', res[0], 'INSERT')
        
        alerta = procesar_impacto_financiero(datos['monto'], datos['tipo'], cta, subcat, est)
        msg = MSG_EXITO_FINANZA.format(concepto=datos['concepto'], monto=datos['monto'], cat=subcat, cuenta=cta, fecha=datos['fecha'], estado="⏳ Pendiente" if est == 'Pendiente' else "✅ Completado", alerta=alerta)
        if es_rec: msg = msg.replace("💲 $0.00", "🔔 (Recordatorio)")
        
    await bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown', reply_markup=obtener_menu_principal())

async def manejar_resultado_analisis(update, context, datos):
    if datos.get('area') == "Desconocida":
        context.user_data['pendiente_clasificar'] = datos
        palabra = obtener_palabra_aprendizaje(datos.get('palabra_clave') or datos.get('concepto') or datos.get('descripcion'))
        if not palabra:
            datos['area' if datos.get('tipo_registro') == 'finanza' else 'tipo'] = 'Personal'
            return await guardar_datos_nlp(context.bot, MI_TELEGRAM_ID, datos, context)
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"🤔 No reconozco si *'{palabra.title()}'* es Empresa o Personal.\nClasifícalo:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏢 Empresa", callback_data='aprender_empresa')], [InlineKeyboardButton("👤 Personal", callback_data='aprender_personal')]]), parse_mode='Markdown')
    else:
        await guardar_datos_nlp(context.bot, MI_TELEGRAM_ID, datos, context)

async def procesar_texto_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("🧠 *Procesando...*", parse_mode='Markdown')
    await manejar_resultado_analisis(update, context, analizar_texto_local(update.message.text))
    try: await m.delete()
    except: pass

async def procesar_audio_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    if update.message.voice.duration > 60: return await update.message.reply_text(MSG_AUDIO_LARGO)
    m = await update.message.reply_text("🎙️ *Transcribiendo...*", parse_mode='Markdown')
    tmp_ogg, tmp_wav = "", ""
    try:
        archivo = await context.bot.get_file(update.message.voice.file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as f: tmp_ogg = f.name
        await archivo.download_to_drive(tmp_ogg)
        tmp_wav = tmp_ogg.replace(".ogg", ".wav")
        AudioSegment.from_ogg(tmp_ogg).export(tmp_wav, format="wav")
        with sr.AudioFile(tmp_wav) as source: texto = sr.Recognizer().recognize_google(sr.Recognizer().record(source), language="es-MX")
        if len(texto.strip()) < 3: raise Exception("Audio vacío")
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"🗣️ _\"{texto}\"_", parse_mode='Markdown')
        await manejar_resultado_analisis(update, context, analizar_texto_local(texto))
    except Exception as e:
        registrar_error("AUDIO", str(e))
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=MSG_ERROR)
    finally:
        if tmp_ogg and os.path.exists(tmp_ogg): os.remove(tmp_ogg)
        if tmp_wav and os.path.exists(tmp_wav): os.remove(tmp_wav)
        try: await m.delete()
        except: pass

async def procesar_foto_local(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    m = await update.message.reply_text("📸 *Escaneando OCR...*", parse_mode='Markdown')
    tmp_jpg = ""
    try:
        archivo = await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as f: tmp_jpg = f.name
        await archivo.download_to_drive(tmp_jpg)
        img = Image.open(tmp_jpg).convert('L')
        img.thumbnail((1200, 1200)) 
        texto_crudo = pytesseract.image_to_string(img, lang='spa')
        match = re.search(r'(?:TOTAL|IMPORTE|NETO|SUMA)[\s\$:]*([\d,]+\.\d{2})', texto_crudo, re.IGNORECASE)
        if not match: raise Exception("Monto no detectado.")
        await manejar_resultado_analisis(update, context, analizar_texto_local(f"ticket pagué {match.group(1)} {texto_crudo[:300]}"))
    except Exception as e: 
        registrar_error("OCR", str(e))
        await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=f"⚠️ OCR Falló: Regístralo manualmente.")
    finally: 
        if tmp_jpg and os.path.exists(tmp_jpg): os.remove(tmp_jpg)
        try: await m.delete()
        except: pass

# ==========================================
# 7. EDICIÓN INTERACTIVA (WHITELIST Y DATEPARSER)
# ==========================================
async def iniciar_edicion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS: return
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith('edit_f_'):
        context.user_data['edit_id'], context.user_data['edit_tabla'] = query.data.split('_')[2], 'finanzas'
        await query.edit_message_text(f"Editando Finanza #{context.user_data['edit_id']}. ¿Qué cambias?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Monto", callback_data="edcampo_monto"), InlineKeyboardButton("Concepto", callback_data="edcampo_concepto")],
            [InlineKeyboardButton("Fecha", callback_data="edcampo_fecha"), InlineKeyboardButton("Cuenta", callback_data="edcampo_cuenta")],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]))
        return E_CAMPO
        
    elif query.data.startswith('edit_t_'):
        context.user_data['edit_id'], context.user_data['edit_tabla'] = query.data.split('_')[2], 'tareas'
        await query.edit_message_text(f"Editando Tarea #{context.user_data['edit_id']}. ¿Qué cambias?", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Descripción", callback_data="edcampo_descripcion"), InlineKeyboardButton("Fecha", callback_data="edcampo_fecha_calculada")],
            [InlineKeyboardButton("Prioridad", callback_data="edcampo_prioridad"), InlineKeyboardButton("Estado", callback_data="edcampo_estado")],
            [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]))
        return E_CAMPO

async def capturar_campo_edicion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['edit_campo'] = query.data.split('_')[1]
    await query.edit_message_text(f"Escribe el nuevo valor para *{context.user_data['edit_campo']}*:", parse_mode='Markdown')
    return E_VALOR

async def guardar_edicion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nuevo_valor, tabla, campo, reg_id = update.message.text, context.user_data.get('edit_tabla'), context.user_data.get('edit_campo'), context.user_data.get('edit_id')
    if tabla not in ['finanzas', 'tareas']: return ConversationHandler.END
    if tabla == 'finanzas' and campo not in ['monto', 'concepto', 'fecha', 'cuenta']: return ConversationHandler.END
    if tabla == 'tareas' and campo not in ['descripcion', 'fecha_calculada', 'prioridad', 'estado']: return ConversationHandler.END
    
    try:
        if campo == 'monto': nuevo_valor = float(nuevo_valor.replace('$', '').replace(',', ''))
        elif campo in ['fecha', 'fecha_calculada']:
            f_calc = dateparser.parse(nuevo_valor, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
            if f_calc: nuevo_valor = f_calc.strftime('%Y-%m-%d')
            else: raise Exception("Fecha inválida")
            
        ejecutar_sql(f"UPDATE {tabla} SET {campo} = %s WHERE id = %s", (nuevo_valor, reg_id))
        registrar_operacion(tabla, reg_id, f'UPDATE_{campo}')
        if tabla == 'finanzas': sincronizar_saldos_db()
        await update.message.reply_text(f"✅ Registro #{reg_id} actualizado.", reply_markup=obtener_menu_principal())
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {e}", reply_markup=obtener_menu_principal())
    context.user_data.clear()
    return ConversationHandler.END

# ==========================================
# 8. MENÚS, DASHBOARDS Y ALARMAS
# ==========================================
def obtener_menu_principal():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Agenda/Rutinas", callback_data='menu_tarea'), InlineKeyboardButton("💰 Finanzas", callback_data='menu_finanzas')],
        [InlineKeyboardButton("⏱️ Modo Enfoque", callback_data='modo_enfoque'), InlineKeyboardButton("✏️ Editar Recientes", callback_data='editar_registros')]
    ])

async def manejador_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ALLOWED_USERS: return
    await query.answer() 

    if query.data == 'confirmar_alto':
        datos = context.user_data.pop('pending_alta', None)
        if datos:
            datos['tipo_registro'], datos['confirmado'] = datos['tipo_registro_original'], True
            await guardar_datos_nlp(context.bot, MI_TELEGRAM_ID, datos, context)
            await query.edit_message_text("✅ Monto alto confirmado.")

    elif query.data == 'editar_registros':
        fins = ejecutar_sql("SELECT id, concepto, monto FROM finanzas ORDER BY id DESC LIMIT 3", fetch_all=True) or []
        tars = ejecutar_sql("SELECT id, descripcion, fecha_calculada FROM tareas ORDER BY id DESC LIMIT 3", fetch_all=True) or []
        teclado = [[InlineKeyboardButton(f"💸 {f[1][:15]} (${f[2]:.0f})", callback_data=f"edit_f_{f[0]}")] for f in fins]
        teclado.extend([[InlineKeyboardButton(f"📝 {t[1][:15]} ({t[2]})", callback_data=f"edit_t_{t[0]}")] for t in tars])
        teclado.append([InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')])
        await query.edit_message_text("Selecciona el registro a editar:", reply_markup=InlineKeyboardMarkup(teclado))

    elif query.data == 'menu_tarea':
        await query.edit_message_text("📝 *Agenda:*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Registrar Manual", callback_data='tarea_nueva')], [InlineKeyboardButton("✅ Ver Pendientes / Rutinas", callback_data='listar_pendientes')], [InlineKeyboardButton("📅 Calendario 7 Días", callback_data='ver_calendario')], [InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
        
    elif query.data == 'listar_pendientes':
        pendientes = ejecutar_sql("SELECT id, descripcion, categoria_operativa FROM tareas WHERE estado = 'Pendiente' ORDER BY fecha_calculada ASC LIMIT 8", fetch_all=True) or []
        if not pendientes: return await query.edit_message_text("🎉 Todo al día.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='menu_tarea')]]))
        teclado = [[InlineKeyboardButton(f"{'🔁' if p[2]=='RUTINA' else '✅'} {p[1][:25]}", callback_data=f'completar_t_{p[0]}')] for p in pendientes]
        teclado.append([InlineKeyboardButton("🔙 Volver", callback_data='menu_tarea')])
        await query.edit_message_text("Selecciona para COMPLETAR:", reply_markup=InlineKeyboardMarkup(teclado))
        
    elif query.data.startswith('completar_t_'):
        id_tarea = query.data.split('_')[2]
        info = ejecutar_sql("SELECT categoria_operativa, fecha_original, fecha_calculada FROM tareas WHERE id = %s", (id_tarea,), fetch_one=True)
        if info:
            if info[0] == 'RUTINA':
                siguiente_fecha = calcular_siguiente_rutina(info[1], info[2])
                ejecutar_sql("UPDATE tareas SET fecha_calculada = %s WHERE id = %s", (siguiente_fecha, id_tarea))
                await query.edit_message_text(f"🔁 Rutina completada. Próxima: {siguiente_fecha}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú", callback_data='volver_inicio')]]))
            else:
                ejecutar_sql("UPDATE tareas SET estado = 'Completada', fecha_completado = %s WHERE id = %s", (datetime.date.today().strftime('%Y-%m-%d'), id_tarea))
                await query.edit_message_text("🎉 Tarea completada.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú", callback_data='volver_inicio')]]))

    elif query.data == 'ver_calendario':
        tareas = ejecutar_sql("SELECT descripcion, fecha_calculada, categoria_operativa FROM tareas WHERE estado = 'Pendiente'", fetch_all=True) or []
        proximos_7 = [datetime.date.today() + datetime.timedelta(days=i) for i in range(7)]
        dias_esp = {0: 'lunes', 1: 'martes', 2: 'miércoles', 3: 'jueves', 4: 'viernes', 5: 'sábado', 6: 'domingo'}
        mensaje = "📅 *AGENDA:*\n\n"
        for dia_d in proximos_7:
            d_str = dia_d.strftime('%Y-%m-%d')
            t_dia = [t for t in tareas if t[1] == d_str]
            if t_dia: mensaje += f"🗓️ *{dias_esp[dia_d.weekday()].capitalize()}, {d_str}*\n" + "".join([f"   ├ ▫️ {t[0]}\n" for t in t_dia]) + "\n"
        await query.edit_message_text(mensaje if "🗓️" in mensaje else "Tu agenda está al día.", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))

    elif query.data == 'menu_finanzas':
        await query.edit_message_text("💰 *Finanzas:*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Gasto Manual", callback_data='finanza_gasto'), InlineKeyboardButton("💵 Ingreso Manual", callback_data='finanza_ingreso')],
            [InlineKeyboardButton("✅ Pagar Pendientes", callback_data='listar_pagos_pendientes')],
            [InlineKeyboardButton("📊 Balance Mensual", callback_data='ver_balance_mes')],
            [InlineKeyboardButton("⬇️ Exportar CSV", callback_data='exportar_csv'), InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]
        ]))
        
    elif query.data == 'listar_pagos_pendientes':
        pagos = ejecutar_sql("SELECT id, concepto, monto, fecha, es_recordatorio FROM finanzas WHERE estado = 'Pendiente' ORDER BY fecha ASC LIMIT 8", fetch_all=True) or []
        if not pagos: return await query.edit_message_text("🎉 Sin pendientes.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='menu_finanzas')]]))
        hoy_str = datetime.date.today().strftime('%Y-%m-%d')
        teclado = [[InlineKeyboardButton(f"{'🔴' if p[3] < hoy_str else '⏳'} {'Recordatorio' if p[4] else f'${p[2]:.0f}'}: {p[1][:12]}", callback_data=f'pagar_f_{p[0]}')] for p in pagos]
        teclado.append([InlineKeyboardButton("🔙 Volver", callback_data='menu_finanzas')])
        await query.edit_message_text("*(🔴 = Vencido)*", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(teclado))
        
    elif query.data.startswith('pagar_f_'):
        id_pago = query.data.split('_')[2]
        info = ejecutar_sql("SELECT monto, tipo, cuenta, subcategoria, estado, es_recordatorio, concepto FROM finanzas WHERE id = %s", (id_pago,), fetch_one=True)
        if info and info[4] == 'Pendiente':
            if info[5]: 
                ejecutar_sql("DELETE FROM finanzas WHERE id = %s", (id_pago,))
                await query.edit_message_text(f"🔔 Borré el recordatorio sin monto.\nPara pagarlo, dictame el importe exacto.", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú", callback_data='volver_inicio')]]))
            else:
                ejecutar_sql("UPDATE finanzas SET estado = 'Completado', fecha = %s WHERE id = %s", (datetime.date.today().strftime('%Y-%m-%d'), id_pago))
                registrar_operacion('finanzas', id_pago, 'PAGO_REALIZADO')
                alerta = procesar_impacto_financiero(info[0], info[1], info[2], info[3], 'Completado')
                await query.edit_message_text(f"💸 Pago realizado.{alerta}", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menú", callback_data='volver_inicio')]]))

    elif query.data.startswith('ver_balance_'):
        mes_actual = datetime.datetime.now().strftime('%Y-%m')
        registros = ejecutar_sql("SELECT tipo, area, monto, concepto, fecha, estado, subcategoria, es_recordatorio FROM finanzas WHERE fecha LIKE %s ORDER BY fecha ASC", (f"{mes_actual}-%",), fetch_all=True) or []
        ing_emp = gas_emp = ing_per = gas_per = 0
        for r in registros:
            if r[5] == 'Completado' and not r[7]: 
                if r[0] == 'Ingreso' and r[1] == 'Empresa': ing_emp += r[2]
                elif r[0] == 'Gasto' and r[1] == 'Empresa': gas_emp += r[2]
                elif r[0] == 'Ingreso' and r[1] == 'Personal': ing_per += r[2]
                elif r[0] == 'Gasto' and r[1] == 'Personal': gas_per += r[2]
                
        mensaje = f"📊 *BALANCE DEL MES*\n\n🏢 *EMPRESA:*\n   ├ 📈 In: ${ing_emp:,.2f} | 📉 Out: ${gas_emp:,.2f}\n   └ ⚖️ Balance: ${(ing_emp - gas_emp):,.2f}\n\n👤 *PERSONAL:*\n   ├ 📈 In: ${ing_per:,.2f} | 📉 Out: ${gas_per:,.2f}\n   └ ⚖️ Balance: ${(ing_per - gas_per):,.2f}\n\n"
        cuentas_res = ejecutar_sql("SELECT nombre, saldo FROM config_cuentas WHERE saldo != 0", fetch_all=True) or []
        if cuentas_res: mensaje += "💰 *Saldos Actuales:*\n" + "".join([f"   ▫️ {c[0]}: ${c[1]:,.2f}\n" for c in cuentas_res])
        await query.edit_message_text(mensaje, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='menu_finanzas')]]))

    elif query.data.startswith('aprender_'):
        categoria = "Empresa" if query.data == 'aprender_empresa' else "Personal"
        datos = context.user_data.get('pendiente_clasificar')
        if datos:
            palabra = obtener_palabra_aprendizaje(datos.get('palabra_clave') or datos.get('concepto') or datos.get('descripcion'))
            if palabra:
                ejecutar_sql("INSERT INTO diccionario (palabra, categoria) VALUES (%s, %s) ON CONFLICT DO NOTHING", (palabra.upper(), categoria))
                recargar_cache()
                datos['area' if datos.get('tipo_registro') == 'finanza' else 'tipo'] = categoria
                await guardar_datos_nlp(context.bot, MI_TELEGRAM_ID, datos, context)
            else: await query.edit_message_text("⚠️ Palabra inválida.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data='volver_inicio')]]))
            context.user_data.pop('pendiente_clasificar', None)
            
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
# 9. REGISTROS MANUALES
# ==========================================
async def cancelar_registro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelado.", reply_markup=obtener_menu_principal())
    return ConversationHandler.END

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
    await u.callback_query.edit_message_text("📅 *Fecha:*", parse_mode='Markdown')
    return T_FECHA
async def capturar_fecha(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    f_calc = dateparser.parse(u.message.text, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    c.user_data['nueva_tarea']['fecha_original'] = u.message.text
    c.user_data['nueva_tarea']['fecha_calculada'] = f_calc.strftime('%Y-%m-%d') if f_calc else "Recurrente"
    await u.message.reply_text("🗒️ *Notas:*", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Saltar", callback_data='sin_notas')]]), parse_mode='Markdown')
    return T_NOTAS
async def capturar_notas(u: Update, cx: ContextTypes.DEFAULT_TYPE) -> int:
    tarea = cx.user_data['nueva_tarea']
    cat_op = "RUTINA" if tarea['prioridad'] == "Rutina" else "TAREA"
    res = ejecutar_sql("INSERT INTO tareas (descripcion, tipo, prioridad, fecha_original, fecha_calculada, notas, estado, categoria_operativa) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
              (tarea['descripcion'], tarea['tipo'], tarea['prioridad'], tarea['fecha_original'], tarea['fecha_calculada'], "Ninguna" if u.callback_query else u.message.text, 'Pendiente', cat_op), fetch_one=True)
    if res: registrar_operacion('tareas', res[0], 'INSERT')
    msg = MSG_EXITO_TAREA.format(cat_op=cat_op, desc=tarea['descripcion'], fecha=tarea['fecha_calculada'], area=tarea['tipo'])
    await (u.callback_query.edit_message_text if u.callback_query else u.message.reply_text)(msg, parse_mode='Markdown', reply_markup=obtener_menu_principal())
    return ConversationHandler.END

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
        await u.message.reply_text("⚠️ Solo números:")
        return F_MONTO 
    if c.user_data['nueva_finanza']['tipo'] == "Gasto":
        teclado, cuentas = [], list(CACHE_CUENTAS.values())
        for i in range(0, len(cuentas), 2): teclado.append([InlineKeyboardButton(cta, callback_data=f'f_cta_{cta}') for cta in cuentas[i:i+2]])
        await u.message.reply_text("💳 *Cuenta:*", reply_markup=InlineKeyboardMarkup(teclado), parse_mode='Markdown')
        return F_METODO
    c.user_data['nueva_finanza']['metodo'] = "N/A"
    await u.message.reply_text("📅 *Fecha:*", parse_mode='Markdown')
    return F_FECHA
async def capturar_f_metodo(u: Update, c: ContextTypes.DEFAULT_TYPE) -> int:
    await u.callback_query.answer()
    c.user_data['nueva_finanza']['metodo'] = u.callback_query.data.replace('f_cta_', '')
    await u.callback_query.edit_message_text("📅 *Fecha:*", parse_mode='Markdown')
    return F_FECHA
async def capturar_f_fecha(u: Update, cx: ContextTypes.DEFAULT_TYPE) -> int:
    f = cx.user_data['nueva_finanza']
    fc = dateparser.parse(u.message.text, languages=['es'], settings={'PREFER_DATES_FROM': 'future', 'TIMEZONE': 'America/Monterrey'})
    ff = fc.strftime('%Y-%m-%d') if fc else datetime.datetime.now().strftime('%Y-%m-%d')
    est = 'Pendiente' if ff > datetime.datetime.now().strftime('%Y-%m-%d') else 'Completado'
    res = ejecutar_sql("INSERT INTO finanzas (tipo, area, concepto, monto, metodo, fecha, categoria, estado, subcategoria, cuenta, es_recordatorio) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id", 
                (f['tipo'], f['area'], f['concepto'], f['monto'], f['metodo'], ff, 'General', est, 'General', f['metodo'], False), fetch_one=True)
    if res: registrar_operacion('finanzas', res[0], 'INSERT')
    alerta = procesar_impacto_financiero(f['monto'], f['tipo'], f['metodo'], 'General', est)
    msg = MSG_EXITO_FINANZA.format(concepto=f['concepto'].title(), monto=f['monto'], cat="General", cuenta=f['metodo'], fecha=ff, estado="⏳ Pendiente" if est == 'Pendiente' else "✅ Completado", alerta=alerta)
    await u.message.reply_text(msg, parse_mode='Markdown', reply_markup=obtener_menu_principal())
    return ConversationHandler.END

async def alerta_matutina(context: ContextTypes.DEFAULT_TYPE):
    hoy = datetime.date.today().strftime('%Y-%m-%d')
    tareas = ejecutar_sql("SELECT descripcion, prioridad FROM tareas WHERE fecha_calculada <= %s AND estado = 'Pendiente'", (hoy,), fetch_all=True) or []
    pagos = ejecutar_sql("SELECT concepto, monto, fecha, es_recordatorio FROM finanzas WHERE fecha <= %s AND estado = 'Pendiente'", (hoy,), fetch_all=True) or []
    fins = ejecutar_sql("SELECT tipo, monto FROM finanzas WHERE fecha LIKE %s AND estado = 'Completado' AND es_recordatorio = FALSE", (f"{datetime.datetime.now().strftime('%Y-%m')}-%",), fetch_all=True) or []
    ing, gas = sum(f[1] for f in fins if f[0] == 'Ingreso'), sum(f[1] for f in fins if f[0] == 'Gasto')
    mensaje = f"☀️ *DASHBOARD ({hoy})*\n\n📅 *Tareas hoy:* {len(tareas)} (⚠️ {sum(1 for t in tareas if t[1] == 'Urgente')} Urgentes)\n⚖️ *Balance mensual:* ${(ing - gas):,.2f}\n"
    if pagos: mensaje += "\n🚨 *Vencimientos de hoy:*\n" + "".join([f"   {'🔴' if p[2] < hoy else '🔔'} {p[0]} ({'Recordatorio' if p[3] else f'${p[1]:,.2f}'})\n" for p in pagos])
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=mensaje, parse_mode='Markdown', reply_markup=obtener_menu_principal())

async def alerta_nocturna(context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text="🌙 *Corte de caja.* Revisa pendientes y descansa.", parse_mode='Markdown', reply_markup=obtener_menu_principal())

async def alerta_semanal(context: ContextTypes.DEFAULT_TYPE):
    hace_7, hoy_str = (datetime.date.today() - datetime.timedelta(days=7)).strftime('%Y-%m-%d'), datetime.date.today().strftime('%Y-%m-%d')
    creadas = (ejecutar_sql("SELECT COUNT(*) FROM tareas WHERE fecha_calculada BETWEEN %s AND %s", (hace_7, hoy_str), fetch_one=True) or [0])[0]
    comp = (ejecutar_sql("SELECT COUNT(*) FROM tareas WHERE fecha_completado BETWEEN %s AND %s", (hace_7, hoy_str), fetch_one=True) or [0])[0]
    fins = ejecutar_sql("SELECT tipo, monto FROM finanzas WHERE fecha BETWEEN %s AND %s AND estado = 'Completado' AND es_recordatorio = FALSE", (hace_7, hoy_str), fetch_all=True) or []
    ing, gas = sum(f[1] for f in fins if f[0] == 'Ingreso'), sum(f[1] for f in fins if f[0] == 'Gasto')
    mensaje = f"📅 *CORTE SEMANAL*\n\n📝 *Productividad:*\n   ├ Nuevas: {creadas}\n   └ ✅ Terminadas: {comp}\n\n💰 *Finanzas:*\n   ├ 📈 In: ${ing:,.2f} | 📉 Out: ${gas:,.2f}\n   └ ⚖️ Balance: ${(ing - gas):,.2f}\n"
    await context.bot.send_message(chat_id=MI_TELEGRAM_ID, text=mensaje, parse_mode='Markdown')
    await generar_y_enviar_backup(context)

# ==========================================
# 10. ARRANQUE DEL BOT
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id not in ALLOWED_USERS: return
    await update.message.reply_text("¡Hola! Háblame o usa el menú:", reply_markup=obtener_menu_principal())

if __name__ == '__main__':
    if not TOKEN or not DATABASE_URL or MI_TELEGRAM_ID == 0: exit(1)
        
    init_db_pool()
    init_db()
    
    threading.Thread(target=iniciar_servidor, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()

    zona = ZoneInfo('America/Monterrey')
    app.job_queue.run_daily(alerta_matutina, time=datetime.time(hour=6, minute=0, tzinfo=zona))
    app.job_queue.run_daily(alerta_nocturna, time=datetime.time(hour=19, minute=0, tzinfo=zona))
    app.job_queue.run_daily(alerta_semanal, time=datetime.time(hour=21, minute=0, tzinfo=zona), days=(6,))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("deshacer", comando_deshacer))
    app.add_handler(CommandHandler("add_cuenta", add_cuenta))
    app.add_handler(CommandHandler("presupuesto", set_presupuesto))
    app.add_handler(CommandHandler("recalcular_saldos", recalcular_saldos))
    
    conv_edicion = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_edicion, pattern='^edit_')],
        states={E_CAMPO: [CallbackQueryHandler(capturar_campo_edicion, pattern='^edcampo_')], E_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, guardar_edicion)]},
        fallbacks=[CommandHandler('cancelar', lambda u, c: ConversationHandler.END)]
    )
    
    conv_handler_tarea = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_registro_tarea, pattern='^tarea_nueva$')],
        states={T_DESCRIPCION: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_descripcion)], T_TIPO: [CallbackQueryHandler(capturar_tipo, pattern='^tipo_')], T_PRIORIDAD: [CallbackQueryHandler(capturar_prioridad, pattern='^prio_')], T_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_fecha)], T_NOTAS: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_notas), CallbackQueryHandler(capturar_notas, pattern='^sin_notas$')]},
        fallbacks=[CommandHandler('cancelar', cancelar_registro)], per_message=False
    )
    
    conv_handler_finanzas = ConversationHandler(
        entry_points=[CallbackQueryHandler(iniciar_finanzas, pattern='^finanza_gasto$|^finanza_ingreso$')],
        states={F_AREA: [CallbackQueryHandler(capturar_f_area, pattern='^f_')], F_CONCEPTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_concepto)], F_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_monto)], F_METODO: [CallbackQueryHandler(capturar_f_metodo, pattern='^f_cta_')], F_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, capturar_f_fecha)]},
        fallbacks=[CommandHandler('cancelar', cancelar_registro)], per_message=False
    )

    app.add_handler(conv_edicion)
    app.add_handler(conv_handler_tarea)
    app.add_handler(conv_handler_finanzas)
    app.add_handler(CallbackQueryHandler(manejador_botones)) 
    
    app.add_handler(MessageHandler(filters.VOICE, procesar_audio_local))
    app.add_handler(MessageHandler(filters.PHOTO, procesar_foto_local))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, procesar_texto_libre)) 

    print("🤖 Súper Bot V28 INICIADO: Full Producción con Correcciones Finales Aplicadas.")
    app.run_polling()
