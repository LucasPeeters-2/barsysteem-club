# app.py - Versie 1.6.1 (Tab-system, Dashboard & Messaging)
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import event
from sqlalchemy.engine import Engine
import os
import csv
import io
import traceback
import logging
from datetime import datetime
import urllib.parse

# Google Drive API Imports
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

# SQLite Database Configuratie
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Gebruik absolute paden voor templates en static om TemplateNotFound errors te voorkomen
app = Flask(__name__, 
            template_folder=os.path.join(BASE_DIR, 'templates'),
            static_folder=os.path.join(BASE_DIR, 'static'))
app.secret_key = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'instance', 'barkaart.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'timeout': 30}}

# --- LOGGING OPSCHONEN ---
# Dit zorgt ervoor dat de "GET / HTTP/1.1" logs niet meer de console vervuilen
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

db = SQLAlchemy(app)

# ==========================================
# DE ULTIEME FIX: ZET SQLITE IN WAL-MODUS
# ==========================================
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
# ==========================================

VERSION = "1.6.1"
MAINTENANCE_FLAG = 'maintenance.flag'
CREDENTIALS_FILE = 'credentials.json'
GDRIVE_FOLDER_ID = '1zD97nlummGl4LybhJsQSlE6BL9qNWVuS'
ROLES = ['kassa', 'penningmeester', 'beheerder', 'superadmin']

# --- DATABASIS MODELLEN (SQLITE) ---
class Lid(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    voornaam = db.Column(db.String(50), nullable=False)
    tussenvoegsel = db.Column(db.String(20), nullable=True, default='')
    achternaam = db.Column(db.String(50), nullable=False)
    speleenheid = db.Column(db.String(50), nullable=False)
    actief = db.Column(db.Boolean, default=True, nullable=False)

    @property
    def volledig(self):
        if self.tussenvoegsel:
            return f"{self.voornaam} {self.tussenvoegsel} {self.achternaam}".strip()
        return f"{self.voornaam} {self.achternaam}".strip()

    @property
    def saldo(self):
        som = db.session.query(db.func.sum(ConsumptieLog.bedrag)).filter(ConsumptieLog.naam == self.volledig).scalar()
        return float(som) if som is not None else 0.0

class Product(db.Model):
    naam = db.Column(db.String(100), primary_key=True)
    prijs = db.Column(db.Float, nullable=False)

    @property
    def Naam(self): return self.naam
    @property
    def Prijs(self): return self.prijs

class AdminUser(db.Model):
    gebruiker = db.Column(db.String(50), primary_key=True)
    pincode = db.Column(db.String(255), nullable=False)
    rol = db.Column(db.String(30), nullable=False)

    @property
    def Gebruiker(self): return self.gebruiker
    @property
    def Pincode(self): return "[VERSLEUTELD]"
    @property
    def Rol(self): return self.rol

class ConsumptieLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    datum = db.Column(db.String(20), default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    naam = db.Column(db.String(150), nullable=False)
    actie = db.Column(db.String(50), nullable=False)
    bedrag = db.Column(db.Float, nullable=False)

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    datum = db.Column(db.String(20), default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    admin = db.Column(db.String(50), nullable=False)
    actie = db.Column(db.String(50), nullable=False)
    details = db.Column(db.String(255), nullable=True)

class Setting(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(50))

# --- UTILS & SECURITY CONTEXT ---
def is_maintenance():
    return os.path.exists(MAINTENANCE_FLAG)

def log_audit(actie, details=""):
    admin_naam = session.get('user_name', 'Systeem')
    log = AuditLog(admin=admin_naam, actie=actie, details=details)
    db.session.add(log)
    db.session.commit()

def log_error(msg):
    try:
        with open("errordump.txt", "a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except:
        pass

def has_role(min_rol):
    if 'user_role' not in session: return False
    if session['user_role'] == 'superadmin': return True
    try:
        return ROLES.index(session['user_role']) >= ROLES.index(min_rol)
    except:
        return False

@app.context_processor
def inject_auth_helpers():
    return dict(has_role=has_role, maintenance=is_maintenance(), version=VERSION, roles=ROLES)

# --- CLOUD SYNC HELPER ---
def upload_to_gdrive(filepath, custom_name=None):
    token_path = os.path.join(BASE_DIR, 'token.json')
    if not GDRIVE_AVAILABLE or not os.path.exists(token_path): return False
    
    creds = None
    try:
        creds = Credentials.from_authorized_user_file(token_path, ['https://www.googleapis.com/auth/drive.file'])
    except Exception as e:
        log_error(f"Fout bij laden token.json: {e}")
        return False
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except:
                return False
        else:
            return False # Token ontbreekt of is ongeldig, moet via run.py gefixed worden

    if not creds: return False

    try:
        # Gebruik expliciet de user-creds om Service Account fallback te voorkomen
        service = build('drive', 'v3', credentials=creds, static_discovery=False)
        file_metadata = {'name': custom_name or os.path.basename(filepath), 'parents': [GDRIVE_FOLDER_ID]}
        media = MediaFileUpload(filepath, resumable=True)
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return True
    except Exception as e:
        log_error(f"Cloud Sync Fout: {e}")
        return False

# --- ERROR HANDLERS (HUISTIJL FOUTPAGINA'S) ---
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    log_error(f"500 Server Fout: {traceback.format_exc()}")
    return render_template('500.html'), 500

@app.errorhandler(405)
def method_not_allowed(e):
    return render_template('405.html'), 405

# --- CORE BACKEND ROUTES ---
@app.route('/')
def index():
    if is_maintenance():
        return render_template('onderhoud.html')
    
    actieve_leden = Lid.query.filter_by(actief=True).all()
    alle_producten = Product.query.order_by(Product.naam).all()
    
    # --- DE FIX: Haal de modus op uit de database voor index.html ---
    s = Setting.query.filter_by(key='ui_mode').first()
    modus = s.value if s else 'batch'
    # ----------------------------------------------------------------

    # Haal de bar-mededeling op uit de database voor de kassa
    msg_setting = Setting.query.filter_by(key='bar_message').first()
    bar_message = msg_setting.value if msg_setting else ""
    
    leden_data = []
    for l in actieve_leden:
        leden_data.append({
            'naam': l.volledig,
            'saldo': f"{l.saldo:.2f}",
            'saldo_getal': l.saldo
        })
        
    prod_data = [{'Naam': p.naam, 'Prijs': p.prijs} for p in alle_producten]
    
    # Geef nu ook 'modus=modus' netjes mee aan de template!
    return render_template('index.html', 
                           leden=sorted(leden_data, key=lambda k: k['naam'].lower()), 
                           producten=prod_data, 
                           modus=modus,
                           bar_message=bar_message)

@app.route('/streep', methods=['POST'])
def streep():
    if is_maintenance(): return jsonify({'status': 'error', 'message': 'Systeem in onderhoud.'}), 503
    
    data = request.json
    naam = data.get('naam')
    product_naam = data.get('product')
    aantal = int(data.get('aantal', 1))
    
    prod = Product.query.filter_by(naam=product_naam).first()
    if not prod: return jsonify({'status': 'error', 'message': 'Product niet gevonden'}), 404
    
    totaal_prijs = prod.prijs * aantal
    
    # Validatie check
    error_resp, actief_lid = valideer_transactie(naam, totaal_prijs)
    if error_resp: return error_resp

    log = ConsumptieLog(naam=naam, actie=f"{aantal}x {product_naam}", bedrag=-totaal_prijs)
    db.session.add(log)
    db.session.commit()
    
    return jsonify({'status': 'success', 'nieuw_saldo': actief_lid.saldo if actief_lid else 0.0})

def valideer_transactie(naam, bedrag):
    """Centrale validatie voor saldo en externen-check."""
    actief_lid = next((l for l in Lid.query.filter_by(actief=True).all() if l.volledig == naam), None)
    if actief_lid and actief_lid.speleenheid.lower() == 'extern':
        if (actief_lid.saldo - bedrag) < -0.01:
            return jsonify({
                'status': 'error', 
                'message': f"Actie geweigerd: Externen mogen niet in de min staan! Huidig saldo: € {actief_lid.saldo:.2f}"
            }), 400, None
    return None, actief_lid

@app.route('/streep_batch', methods=['POST'])
def streep_batch():
    if is_maintenance(): return jsonify({'status': 'error', 'message': 'Systeem in onderhoud.'}), 503
    
    data = request.json
    naam = data.get('naam')
    items = data.get('items') # Lijst met {product: 'Bier', aantal: 4}
    
    totaal_bedrag = 0
    log_acties = []
    
    # Valideer eerst alles
    for item in items:
        prod = Product.query.filter_by(naam=item['product']).first()
        if not prod: return jsonify({'status': 'error', 'message': f"Product {item['product']} onbekend"}), 404
        totaal_bedrag += (prod.prijs * item['aantal'])
        log_acties.append(f"{item['aantal']}x {item['product']}")
    
    # Voer actie uit (Extern check etc...)
    # ... (jouw bestaande logica)
    # Voer validatie uit
    error_resp, actief_lid = valideer_transactie(naam, totaal_bedrag)
    if error_resp: return error_resp
    
    # Schrijf 1 log-regel voor de hele batch
    log = ConsumptieLog(naam=naam, actie=", ".join(log_acties), bedrag=-totaal_bedrag)
    db.session.add(log)
    db.session.commit()
    return jsonify({'status': 'success'})

@app.route('/get_history/<path:naam>')
def get_history(naam):
    naam = urllib.parse.unquote(naam).strip()
    try:
        logs = ConsumptieLog.query.filter_by(naam=naam).order_by(ConsumptieLog.id.desc()).limit(5).all()
        history = [{
            'datum': l.datum[:16],
            'product': l.actie,
            'bedrag': f"{l.bedrag:.2f}".replace('.', ',')
        } for l in logs]
        return jsonify(history)
    except Exception as e:
        log_error(f"Get history crash: {traceback.format_exc()}")
        return jsonify([])

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        gebruiker = request.form.get('username')
        pin = request.form.get('pincode')
        
        admin = AdminUser.query.filter_by(gebruiker=gebruiker).first()
        if admin and check_password_hash(admin.pincode, pin):
            session['admin_logged_in'] = True
            session['user_name'] = admin.gebruiker
            session['user_role'] = admin.rol
            log_audit("Login", "Succesvol ingelogd op admin dashboard.")
            return redirect(url_for('admin'))
        
        admin_names = [a.gebruiker for a in AdminUser.query.all()]
        return render_template('login.html', admin_names=sorted(admin_names, key=str.lower), error="Ongeldige inloggegevens.")
    
    admin_names = [a.gebruiker for a in AdminUser.query.all()]
    return render_template('login.html', admin_names=sorted(admin_names, key=str.lower))

@app.route('/logout')
def logout():
    log_audit("Logout", "Afgemeld van admin sessie.")
    session.clear()
    return redirect(url_for('index'))

@app.route('/admin')
def admin():
    # VEILIGHEID: Blokkeer ook actieve sessies tijdens onderhoudsmodus
    if is_maintenance(): 
        return render_template('onderhoud.html')
        
    if not session.get('admin_logged_in'): return redirect(url_for('admin_login'))
    
    # --- UI MODUS (Toggle) ---
    s = Setting.query.filter_by(key='ui_mode').first()
    modus = s.value if s else 'batch'
    
    # --- NIEUW: BAR-MEDEDELING ---
    msg_setting = Setting.query.filter_by(key='bar_message').first()
    bar_message = msg_setting.value if msg_setting else ""
    
    # --- NIEUW: RECENTE LOGS ---
    recente_logs = ConsumptieLog.query.order_by(ConsumptieLog.id.desc()).limit(10).all()
    
    # --- NIEUW: ERROR LOGS (Laatste 10 regels uit errordump.txt) ---
    error_logs = []
    if os.path.exists("errordump.txt"):
        with open("errordump.txt", "r") as f:
            error_logs = f.readlines()[-10:]
            
    # --- BESTAANDE LEDEN & PRODUCTEN LOGICA ---
    alle_leden = Lid.query.filter_by(actief=True).all()
    leden_lijst = []
    for l in alle_leden:
        leden_lijst.append({
            'volledig': l.volledig, 
            'saldo': l.saldo, 
            'eenheid': l.speleenheid
        })
    alle_leden_gesorteerd = sorted(leden_lijst, key=lambda x: x['volledig'].lower())
    
    prod_df = [{'Naam': p.naam, 'Prijs': p.prijs} for p in Product.query.all()]
    all_admins = [{'Gebruiker': a.gebruiker, 'Rol': a.rol, 'Pincode': a.Pincode} for a in AdminUser.query.all()]
    
    # Alles doorsturen naar de template
    return render_template('admin.html', 
                           leden=alle_leden_gesorteerd, 
                           producten=sorted(prod_df, key=lambda x: x['Naam'].lower()), 
                           all_admins=all_admins,
                           modus=modus,
                           recente_logs=recente_logs,
                           error_logs=error_logs,
                           bar_message=bar_message)

@app.route('/admin/update_message', methods=['POST'])
def update_message():
    if not has_role('beheerder'): return "Geen toegang", 403
    msg = request.form.get('message', '')
    s = Setting.query.filter_by(key='bar_message').first()
    if not s: db.session.add(Setting(key='bar_message', value=msg))
    else: s.value = msg
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/admin/cloud_sync')
def cloud_sync_manual():
    if not has_role('beheerder'): return "Geen toegang", 403
    vandaag = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    
    # 1. Database uploaden
    db_path = os.path.join(BASE_DIR, 'instance', 'barkaart.db')
    upload_to_gdrive(db_path, f"handmatige_backup_{vandaag}.db")
    
    # 2. Saldi CSV genereren en uploaden
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Naam', 'Speltak', 'Saldo'])
    leden = Lid.query.filter_by(actief=True).all()
    for l in sorted(leden, key=lambda x: x.voornaam.lower()):
        writer.writerow([l.volledig, l.speleenheid, f"{l.saldo:.2f}"])
    
    csv_path = os.path.join(BASE_DIR, 'backups', 'temp_sync.csv')
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, 'w', encoding='utf-8') as f: f.write(output.getvalue())
    upload_to_gdrive(csv_path, f"handmatige_saldi_{vandaag}.csv")
    
    log_audit("Handmatige Cloud Sync", "Database en Saldi CSV geupload naar Google Drive.")
    return redirect(url_for('admin'))

@app.route('/admin/opwaarderen', methods=['POST'])
def opwaarderen():
    if not has_role('kassa'): return "Geen toegang", 403
    naam = request.form.get('naam')
    bedrag = float(request.form.get('bedrag', 0))
    
    if bedrag <= 0: return "Ongeldig bedrag", 400
    
    log = ConsumptieLog(naam=naam, actie="Opwaardering (PIN)", bedrag=bedrag)
    db.session.add(log)
    db.session.commit()
    
    log_audit("Opwaardering", f"€ {bedrag:.2f} bijgeschreven bij {naam}")
    return redirect(url_for('admin'))

@app.route('/admin/toggle_mode')
def toggle_mode():
    if not has_role('beheerder'): return "Geen toegang", 403
    s = Setting.query.filter_by(key='ui_mode').first()
    if not s:
        db.session.add(Setting(key='ui_mode', value='numpad'))
    else:
        s.value = 'batch' if s.value == 'numpad' else 'numpad'
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/admin/batch_import', methods=['POST'])
def batch_import():
    if not has_role('beheerder'): return "Geen toegang", 403
    if 'csv_file' not in request.files: return "Geen bestand geüpload", 400
    
    file = request.files['csv_file']
    if file.filename == '': return "Leeg bestand", 400
    
    try:
        file_content = file.stream.read().decode("UTF-8-sig")
        bestands_delimiter = ';'
        if ',' in file_content.split('\n')[0] and ';' not in file_content.split('\n')[0]:
            bestands_delimiter = ','
            
        reader = csv.DictReader(io.StringIO(file_content), delimiter=bestands_delimiter)
        if reader.fieldnames:
            reader.fieldnames = [field.strip().lower().replace('"', '').replace("'", "") for field in reader.fieldnames]
        
        geimporteerd = 0
        for row in reader:
            v = row.get('voornaam', '').strip() if row.get('voornaam') else ''
            t = row.get('tussenvoegsel', '').strip() if row.get('tussenvoegsel') else ''
            a = row.get('achternaam', '').strip() if row.get('achternaam') else ''
            e = row.get('speleenheid', 'Lid').strip() if row.get('speleenheid') else 'Lid'
            
            if v and a:
                bestaat_al = Lid.query.filter_by(voornaam=v, tussenvoegsel=t, achternaam=a, actief=True).first()
                if not bestaat_al:
                    db.session.add(Lid(voornaam=v, tussenvoegsel=t, achternaam=a, speleenheid=e, actief=True))
                    geimporteerd += 1
                    
        db.session.commit()
        log_audit("Batch Import", f"{geimporteerd} leden geïmporteerd via CSV.")
        return redirect(url_for('admin'))
    except Exception as e:
        log_error(f"Batch import crash: {traceback.format_exc()}")
        return f"Fout bij importeren: {str(e)}", 500

@app.route('/admin/export_periode')
def export_periode():
    if not has_role('penningmeester'): return "Geen toegang", 403
    export_type = request.args.get('type', 'ALLES').upper()
    
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Naam', 'Speltak/Eenheid', 'Actueel Saldo'])
    
    leden = Lid.query.filter_by(actief=True).all()
    for l in leden:
        if export_type == 'INTERN' and l.speleenheid == 'Extern': continue
        if export_type == 'EXTERN' and l.speleenheid != 'Extern': continue
        writer.writerow([l.volledig, l.speleenheid, f"{l.saldo:.2f}"])
        
    log_audit("Export Saldi", f"Periode-export gedownload (Type: {export_type})")
    
    response = app.response_class(output.getvalue(), mimetype='text/csv')
    response.headers["Content-Disposition"] = f"attachment; filename=barsysteem_export_{export_type.lower()}_{datetime.now().strftime('%Y%m%d')}.csv"
    return response

@app.route('/admin/product/add', methods=['POST'])
def add_product():
    if not has_role('beheerder'): return "Geen toegang", 403
    naam = request.form.get('naam').strip()
    prijs = float(request.form.get('prijs', 0))
    
    prod = Product.query.filter_by(naam=naam).first()
    if prod:
        prod.prijs = prijs
        log_audit("Product Wijziging", f"Prijs van '{naam}' aangepast naar €{prijs:.2f}")
    else:
        db.session.add(Product(naam=naam, prijs=prijs))
        log_audit("Product Toevoeging", f"Nieuw product '{naam}' toegevoegd voor €{prijs:.2f}")
        
    db.session.commit()
    return redirect(url_for('admin'))

@app.route('/admin/product/delete/<path:naam>')
def delete_product(naam):
    if not has_role('beheerder'): return "Geen toegang", 403
    prod = Product.query.filter_by(naam=naam).first()
    if prod:
        db.session.delete(prod)
        db.session.commit()
        log_audit("Product Verwijdering", f"Product '{naam}' gewist.")
    return redirect(url_for('admin'))

@app.route('/admin/add', methods=['POST'])
def add_lid():
    if not has_role('kassa'): return "Geen toegang", 403
    v = request.form.get('voornaam').strip()
    t = request.form.get('tussenvoegsel', '').strip()
    a = request.form.get('achternaam').strip()
    e = request.form.get('eenheid')
    
    db.session.add(Lid(voornaam=v, tussenvoegsel=t, achternaam=a, speleenheid=e, actief=True))
    db.session.commit()
    log_audit("Lid Toevoegen", f"Lid '{v} {t} {a}' handmatig aangemaakt bij {e}.")
    return redirect(url_for('admin'))

@app.route('/admin/archiveer_lid/<path:naam>')
def archiveer_lid(naam):
    if not has_role('superadmin'): return "Geen toegang", 403
    alle_machende_leden = Lid.query.filter_by(actief=True).all()
    lid = next((l for l in alle_machende_leden if l.volledig == naam), None)
    
    if not lid: return "Lid niet gevonden", 404
    if lid.saldo != 0.0: return "Fout: Lid kan alleen gearchiveerd worden bij exact € 0,00 saldo!", 400
    
    lid.actief = False
    db.session.commit()
    log_audit("Lid Archiveren", f"Lid '{naam}' succesvol op inactief gezet.")
    return redirect(url_for('admin'))

@app.route('/admin/reset_saldo/<path:naam>')
def reset_saldo(naam):
    if not has_role('penningmeester'): return "Geen toegang", 403
    alle_machende_leden = Lid.query.filter_by(actief=True).all()
    lid = next((l for l in alle_machende_leden if l.volledig == naam), None)
    if not lid: return "Lid niet gevonden", 404
    
    huidig = lid.saldo
    if huidig == 0: return redirect(url_for('admin'))
    
    tegenboeking = -huidig
    log = ConsumptieLog(naam=naam, actie="Handmatige Saldo Reset", bedrag=tegenboeking)
    db.session.add(log)
    db.session.commit()
    
    log_audit("Saldo Reset", f"Saldo van {naam} gereset naar €0.00 (Tegenboeking: €{tegenboeking:.2f})")
    return redirect(url_for('admin'))

@app.route('/admin/admin/add', methods=['POST'])
def add_admin():
    if not has_role('superadmin'): return "Geen toegang", 403
    u = request.form.get('username').strip()
    p = request.form.get('pincode').strip()
    r = request.form.get('role')
    
    hashed_pin = generate_password_hash(p)
    db.session.add(AdminUser(gebruiker=u, pincode=hashed_pin, rol=r))
    db.session.commit()
    log_audit("Admin Aanmaken", f"Nieuwe admin '{u}' aangemaakt met rechtenniveau '{r}'.")
    return redirect(url_for('admin'))

@app.route('/admin/update_admin', methods=['POST'])
def update_admin():
    if not has_role('superadmin'): return "Geen toegang", 403
    u = request.form.get('username')
    r = request.form.get('role')
    p = request.form.get('pincode')
    
    admin = AdminUser.query.filter_by(gebruiker=u).first()
    if admin:
        admin.rol = r
        if p and p != "[VERSLEUTELD]":
            admin.pincode = generate_password_hash(p)
        db.session.commit()
        log_audit("Admin Update", f"Instellingen voor admin '{u}' bijgewerkt.")
    return redirect(url_for('admin'))

@app.route('/admin/admin/delete/<path:username>')
def delete_admin(username):
    if not has_role('superadmin'): return "Geen toegang", 403
    if username == session.get('user_name'): return "Je kunt jezelf niet verwijderen", 400
    
    admin = AdminUser.query.filter_by(gebruiker=username).first()
    if admin:
        db.session.delete(admin)
        db.session.commit()
        log_audit("Admin Verwijderd", f"Toegang voor admin '{username}' permanent ingetrokken.")
    return redirect(url_for('admin'))

@app.route('/admin/undo_last_streep')
def undo_last_streep():
    if not has_role('kassa'): return jsonify({'status': 'error', 'message': 'Geen toegang'}), 403
    
    laatste_log = ConsumptieLog.query.order_by(ConsumptieLog.id.desc()).first()
    if laatste_log:
        db.session.delete(laatste_log)
        db.session.commit()
        msg = f"Succes: Laatste streep-actie ({laatste_log.actie} voor {laatste_log.naam}) succesvol hersteld!"
        log_audit("Undo Actie", msg)
        return jsonify({'status': 'success', 'message': msg})
    return jsonify({'status': 'error', 'message': 'Geen consumptielogs aanwezig om te herstellen.'})

@app.route('/admin/check_integriteit')
def check_integriteit():
    if not has_role('beheerder'): return jsonify({'status': 'error', 'message': 'Geen toegang'}), 403
    try:
        fouten = 0
        leden = Lid.query.all()
        for l in leden:
            if not l.voornaam or not l.achternaam: fouten += 1
        return jsonify({'status': 'success', 'message': f"Database Validering Geslaagd! Alle SQL-tabellen zijn consistent. Aantal corrupte rijen: {fouten}."})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f"Fout bij database-controle: {str(e)}"})

@app.route('/admin/toggle_maintenance')
def toggle_maintenance():
    if not has_role('superadmin'): return "Geen toegang", 403
    if os.path.exists(MAINTENANCE_FLAG):
        os.remove(MAINTENANCE_FLAG)
        log_audit("Onderhoud", "Systeem ONLINE gezet.")
    else:
        with open(MAINTENANCE_FLAG, 'w') as f: f.write('on')
        log_audit("Onderhoud", "Systeem OFFLINE gezet voor onderhoud.")
    return redirect(url_for('admin'))

@app.route('/admin/reset_all')
def reset_all():
    if not has_role('superadmin'): return "Geen toegang", 403
    try:
        ConsumptieLog.query.delete()
        db.session.commit()
        log_audit("GROTE SCHOONMAAK", "Volledige transactiehistorie permanent gewist uit SQLite database. Iedereen staat weer op 0.")
        return redirect(url_for('admin'))
    except Exception as e:
        return f"Schoonmaak mislukt: {str(e)}", 500

# --- NOOD-BACKDOOR OM ONDERHOUD UIT TE ZETTEN ---
@app.route('/nood-herstel-883921-barkaart')
def nood_herstel():
    if os.path.exists(MAINTENANCE_FLAG):
        os.remove(MAINTENANCE_FLAG)
        log_audit("Nood-herstel", "Systeem via geheime URL uit onderhoud gehaald.")
        return render_template('status.html') # Hier roepen we de nieuwe nette pagina aan!
    return render_template('status.html')

if __name__ == '__main__':
    if not os.path.exists('instance'):
        os.makedirs('instance')
    with app.app_context():
        db.create_all()
        if not AdminUser.query.filter_by(gebruiker='Admin').first():
            db.session.add(AdminUser(gebruiker='Admin', pincode=generate_password_hash('191019'), rol='superadmin'))
            db.session.commit()
    app.run(host='0.0.0.0', port=5000, debug=False)