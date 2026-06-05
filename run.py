# run.py - Shawano Bar Command Center v1.6.1
import subprocess
import time
import sys
import threading
import os
import shutil
import sqlite3
import colorama
from colorama import Fore, Style
from datetime import datetime
from werkzeug.security import generate_password_hash
import csv

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

# Initialiseer kleuren
colorama.init(autoreset=True)

VERSION = "1.6.1"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(BASE_DIR, 'instance', 'barkaart.db')
MAINTENANCE_FLAG = os.path.join(BASE_DIR, 'maintenance.flag')
CREDENTIALS_FILE = os.path.join(BASE_DIR, 'credentials.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'token.json')
GDRIVE_FOLDER_ID = '1zD97nlummGl4LybhJsQSlE6BL9qNWVuS' 
LAST_GDRIVE_DATE = ""

flask_process = None

def color_print(text, color=Fore.WHITE, end="\n"):
    """Hulpmethode voor veilige, gekleen-gekleurde output."""
    sys.stdout.write(f"{color}{text}{Style.RESET_ALL}{end}")
    sys.stdout.flush()

def log_wrapper_error(msg):
    try:
        with open("errordump.txt", "a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [WRAPPER] {msg}\n")
    except:
        pass

# --- AUTOMATISCH SQL BACK-UP SYSTEEM ---
def upload_to_gdrive(filepath, custom_name=None):
    """Uploadt een bestand naar Google Drive met User OAuth credentials."""
    if not GDRIVE_AVAILABLE or not os.path.exists(TOKEN_FILE): return False
    
    creds = None
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, ['https://www.googleapis.com/auth/drive.file'])
    except Exception:
        return False
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                color_print("⚠️ Google Token verlopen. Typ 'google-auth' om te herstellen.", Fore.RED)
                return False
        else:
            return False

    if not creds: return False

    try:
        # Gebruik expliciet de user-creds om Service Account fallback te voorkomen
        service = build('drive', 'v3', credentials=creds, static_discovery=False)
        
        file_metadata = {
            'name': custom_name or os.path.basename(filepath),
            'parents': [GDRIVE_FOLDER_ID]
        }
        media = MediaFileUpload(filepath, resumable=True)
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return True
    except Exception as e:
        log_wrapper_error(f"Google Drive Upload Fout: {e}")
        return False

def google_authenticate_manual():
    """Handmatige inlog-flow die wél een browser kan openen."""
    if not GDRIVE_AVAILABLE:
        color_print("❌ Google API libraries niet gevonden.", Fore.RED); return
    if not os.path.exists(CREDENTIALS_FILE):
        color_print(f"❌ {CREDENTIALS_FILE} niet gevonden!", Fore.RED); return

    # Verwijder oude token als die bestaat om schone start te garanderen
    if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)

    try:
        color_print("🔐 Bezig met openen browser voor Google inlog...", Fore.YELLOW)
        color_print("⚠️  LET OP: Zorg dat je email op de 'Test Users' lijst staat in Google Cloud!", Fore.YELLOW)
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, ['https://www.googleapis.com/auth/drive.file'])
        # Prompt=consent dwingt Google om opnieuw toestemming te vragen
        creds = flow.run_local_server(port=0, prompt='consent')
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
        color_print("✅ Succesvol ingelogd! token.json is aangemaakt.", Fore.GREEN)
    except Exception as e:
        color_print(f"❌ Inloggen mislukt: {e}", Fore.RED)

def maak_saldo_csv_backup():
    """Genereert een tijdelijke CSV met alleen namen en saldi."""
    csv_pad = 'backups/dagelijkse_saldi_temp.csv'
    try:
        with sqlite3.connect(DB_FILE) as conn:
            leden = conn.execute("SELECT voornaam, tussenvoegsel, achternaam, speleenheid FROM lid WHERE actief=1").fetchall()
            with open(csv_pad, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f, delimiter=';')
                writer.writerow(['Naam', 'Speltak', 'Saldo'])
                for l in sorted(leden, key=lambda x: x[0].lower()):
                    volledige_naam = f"{l[0]} {l[1] + ' ' if l[1] else ''}{l[2]}".strip()
                    saldo = conn.execute("SELECT SUM(bedrag) FROM consumptie_log WHERE naam=?", (volledige_naam,)).fetchone()[0] or 0.0
                    writer.writerow([volledige_naam, l[3], f"{float(saldo):.2f}"])
        return csv_pad
    except Exception as e:
        log_wrapper_error(f"CSV backup mislukt: {e}")
        return None

def schoon_oude_backups():
    """Verwijdert backups ouder dan 14 dagen om schijfruimte te besparen."""
    backup_dir = 'backups'
    if not os.path.exists(backup_dir):
        return

    nu = time.time()
    retentie_seconden = 14 * 24 * 60 * 60  # 14 dagen in seconden
    grens = nu - retentie_seconden

    verwijderd = 0
    try:
        for bestandsnaam in os.listdir(backup_dir):
            pad = os.path.join(backup_dir, bestandsnaam)
            if os.path.isfile(pad):
                if os.path.getmtime(pad) < grens:
                    os.remove(pad)
                    verwijderd += 1
        if verwijderd > 0:
            color_print(f"[BACKUP] Schoonmaak voltooid: {verwijderd} oude backup(s) verwijderd.", Fore.YELLOW)
    except Exception as e:
        log_wrapper_error(f"Fout bij opschonen backups: {e}")

def voer_backup_uit(force_cloud=False):
    global LAST_GDRIVE_DATE
    if os.path.exists(DB_FILE):
        os.makedirs('backups', exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_naam = f"backups/barkaart_backup_{timestamp}.db"
        try:
            shutil.copy(DB_FILE, backup_naam)
            color_print(f"\n[BACKUP] Succesvol opgeslagen als: {backup_naam}", Fore.GREEN)
            schoon_oude_backups()
            
            # Cloud Sync (Eén keer per dag)
            vandaag = datetime.now().strftime("%Y-%m-%d")
            if vandaag != LAST_GDRIVE_DATE or force_cloud:
                color_print(f"[GDRIVE] Starten van cloud-sync (Force={force_cloud})...", Fore.CYAN)
                
                # 1. Database naar cloud
                if upload_to_gdrive(DB_FILE, f"barkaart_dagelijks_{vandaag}.db"):
                    color_print(f"[GDRIVE] ✅ Database geüpload naar Google Drive.", Fore.GREEN)
                
                # 2. Saldi CSV naar cloud
                csv_pad = maak_saldo_csv_backup()
                if csv_pad and upload_to_gdrive(csv_pad, f"saldi_export_{vandaag}.csv"):
                    color_print(f"[GDRIVE] ✅ Saldi CSV geüpload naar Google Drive.", Fore.GREEN)
                
                LAST_GDRIVE_DATE = vandaag
        except Exception as e:
            msg = f"Backup mislukt: {e}"
            log_wrapper_error(msg)
            color_print(f"\n[BACKUP] {msg}", Fore.RED)
    else:
        color_print("\n[BACKUP] Geen database gevonden om te back-uppen.", Fore.YELLOW)

def backup_scheduler():
    while True:
        voer_backup_uit()
        # Wacht 4 uur voor de volgende automatische backup
        time.sleep(4 * 60 * 60)

# --- INTERACTIEVE ADMIN CONSOLE ---
def admin_console():
    global flask_process
    color_print("\n" + "="*60, Fore.YELLOW)
    color_print(f"       SHAWANO BAR COMMAND CENTER v{VERSION}", Fore.YELLOW)
    color_print("       Database: SQLite 3", Fore.YELLOW)
    color_print("       Typ 'help' voor alle beschikbare commando's.", Fore.YELLOW)
    color_print("="*60 + "\n", Fore.YELLOW)
    
    while True:
        try:
            input_line = input(Fore.CYAN + "ShawanoBar> " + Style.RESET_ALL).strip()
            if not input_line: continue
            
            parts = input_line.split()
            cmd = parts[0].lower()
            
            # 1. HELP COMMANDO
            if cmd == 'help':
                color_print("\n--- Beschikbare commando's ---", Fore.YELLOW)
                print("  status          - Bekijk de live status van de SQL-database")
                print("  health          - Systeem status & omzet samenvatting")
                print("  watch           - Live transactie-monitor (Ctrl+C om te stoppen)")
                print("  account [naam]  - Saldo en historie van een specifiek lid")
                print("  restart         - Herstart de Flask-server direct (hard reset)")
                print("  broadcast [msg] - Stuur een push-melding naar alle kassa's")
                print("  stats           - Dagcijfers: Omzet en Top-producten van vandaag")
                print("  set-price       - Formaat: set-price [product] [nieuwe_prijs]")
                print("  clear-message   - Verwijder de huidige bar-mededeling")
                print("  logs            - Toon de laatste 15 audit-acties uit SQL")
                print("  leden           - Toon een snelle stand van alle leden en saldi")
                print("  producten       - Bekijk de huidige productlijst en prijzen")
                print("  set-pin         - Formaat: set-pin [naam] [nieuwe_pin]")
                print("  add-admin       - Formaat: add-admin [naam] [pin] [rol]")
                print("  clear-errors    - Verwijder alle opgeslagen foutmeldingen")
                print("  errorlog        - Toon de laatste foutmeldingen uit errordump.txt")
                print("  google-auth     - Open browser om in te loggen bij Google")
                print("  backup          - Maak NU handmatig een back-up van barkaart.db")
                print("  cloud-now       - Forceer NU een upload naar Google Drive")
                print("  maintenance     - Formaat: maintenance [on/off]")
                print("  shutdown        - Sluit de Flask-server en console veilig af\n")
                
            # 2. STATUS & HEALTH
            elif cmd == 'status':
                print(f"\nServerversie: {VERSION}")
                print(f"Database aanwezig: {'Ja' if os.path.exists(DB_FILE) else 'Nee'}")
                print(f"Onderhoudsmodus: {'AAN' if os.path.exists(MAINTENANCE_FLAG) else 'UIT'}\n")
                
            elif cmd == 'health':
                if not os.path.exists(DB_FILE): 
                    color_print("⚠️ Geen database gevonden.", Fore.RED); continue
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        cur = conn.cursor()
                        leden = cur.execute("SELECT count(*) FROM lid WHERE actief=1").fetchone()[0]
                        omzet = cur.execute("SELECT sum(bedrag) FROM consumptie_log WHERE bedrag > 0").fetchone()[0] or 0
                    color_print(f"\n--- SYSTEEM STATUS ---", Fore.GREEN)
                    color_print(f"Actieve Leden: {leden}")
                    color_print(f"Totale Opwaardering: € {omzet:.2f}\n", Fore.YELLOW)
                except Exception as e: color_print(f"❌ Fout bij laden health: {e}", Fore.RED)

            # 3. LIVE MONITORING
            elif cmd == 'watch':
                if not os.path.exists(DB_FILE): 
                    color_print("⚠️ Geen database gevonden.", Fore.RED); continue
                color_print("Monitoring live... Omzet sessie wordt bijgehouden. (Druk Ctrl+C om te stoppen)", Fore.CYAN)
                last_id = 0
                sessie_omzet = 0.0
                try:
                    while True:
                        with sqlite3.connect(DB_FILE) as conn:
                            log = conn.execute("SELECT id, naam, actie, bedrag FROM consumptie_log ORDER BY id DESC LIMIT 1").fetchone()
                            if log and log[0] > last_id:
                                if last_id != 0: # Voorkom dat de allerlaatste actie bij start direct geteld wordt
                                    last_id = log[0]
                                    bedrag = float(log[3])
                                    if bedrag < 0: sessie_omzet += abs(bedrag)
                                    color_print(f"[{datetime.now().strftime('%H:%M:%S')}] {log[1]:<20} | {log[2]:<25} | €{bedrag:>6.2f} | Sessie: €{sessie_omzet:>7.2f}", Fore.CYAN)
                                else:
                                    last_id = log[0]
                        time.sleep(2)
                except KeyboardInterrupt:
                    color_print("\nGestopt met monitoren.", Fore.YELLOW)

            elif cmd == 'stats':
                vandaag = datetime.now().strftime("%Y-%m-%d")
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        omzet = conn.execute("SELECT SUM(ABS(bedrag)) FROM consumptie_log WHERE bedrag < 0 AND datum LIKE ?", (f"{vandaag}%",)).fetchone()[0] or 0.0
                        top_prods = conn.execute("SELECT actie, COUNT(*) as aantal FROM consumptie_log WHERE bedrag < 0 AND datum LIKE ? GROUP BY actie ORDER BY aantal DESC LIMIT 3", (f"{vandaag}%",)).fetchall()
                    
                    color_print(f"\n--- STATISTIEKEN VAN VANDAAG ({vandaag}) ---", Fore.MAGENTA)
                    color_print(f"Totale Dagomzet:  € {float(omzet):.2f}", Fore.GREEN)
                    print("-" * 45)
                    print("TOP 3 PRODUCTEN:")
                    for i, p in enumerate(top_prods, 1):
                        print(f" {i}. {p[0]:<30} ({p[1]}x verkocht)")
                    print("-" * 45 + "\n")
                except Exception as e: color_print(f"❌ Fout bij stats: {e}", Fore.RED)

            # 4. SERVER BEHEER
            elif cmd == 'restart':
                if flask_process:
                    color_print("\n[CONSOLE] Flask-server wordt geforceerd afgesloten...", Fore.YELLOW)
                    flask_process.terminate()
                else:
                    color_print("[CONSOLE] Er draait momenteel geen actieve Flask-server.", Fore.RED)
                    
            elif cmd == 'shutdown':
                color_print("\n[CONSOLE] Systeem wordt veilig afgesloten...", Fore.RED)
                if os.path.exists(MAINTENANCE_FLAG): os.remove(MAINTENANCE_FLAG)
                os._exit(0)

            elif cmd == 'maintenance':
                if len(parts) > 1 and parts[1] == 'on':
                    with open(MAINTENANCE_FLAG, 'w') as f: f.write('on')
                    color_print("[CONSOLE] Onderhoudsmodus AAN gezet.", Fore.RED)
                elif len(parts) > 1 and parts[1] == 'off':
                    if os.path.exists(MAINTENANCE_FLAG): os.remove(MAINTENANCE_FLAG)
                    color_print("[CONSOLE] Onderhoudsmodus UIT gezet.", Fore.GREEN)
                else:
                    color_print("⚠️ Gebruik: maintenance [on/off]", Fore.YELLOW)

            elif cmd == 'broadcast':
                msg = " ".join(parts[1:])
                if not msg:
                    color_print("⚠️ Gebruik: broadcast [bericht]", Fore.YELLOW)
                else:
                    with sqlite3.connect(DB_FILE) as conn:
                        conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('bar_message', ?)", (msg,))
                        conn.commit()
                    color_print(f"📢 Bericht uitgezonden naar kassa's: {msg}", Fore.GREEN)

            elif cmd == 'clear-message':
                with sqlite3.connect(DB_FILE) as conn:
                    conn.execute("UPDATE setting SET value='' WHERE key='bar_message'")
                    conn.commit()
                color_print("✅ Bar-mededeling verwijderd.", Fore.GREEN)

            elif cmd == 'cls':
                os.system('cls' if os.name == 'nt' else 'clear')

            # 5. DATA INZIEN (Logs, Leden, Producten, Accounts)
            elif cmd == 'logs':
                if not os.path.exists(DB_FILE): color_print("⚠️ Geen database.", Fore.RED); continue
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        rows = conn.execute("SELECT datum, admin, actie, details FROM audit_log ORDER BY id DESC LIMIT 15").fetchall()
                    if not rows: print("Geen acties gevonden.")
                    else:
                        print("-" * 75)
                        print(f"{'DATUM':<17} | {'ADMIN':<15} | {'ACTIE':<18} | {'DETAILS'}")
                        print("-" * 75)
                        for r in reversed(rows): print(f"{str(r[0]):<17} | {str(r[1]):<15} | {str(r[2]):<18} | {str(r[3])}")
                        print("-" * 75 + "\n")
                except Exception as e: color_print(f"❌ SQL fout: {e}", Fore.RED)
                
            elif cmd == 'leden':
                if not os.path.exists(DB_FILE): color_print("⚠️ Geen database.", Fore.RED); continue
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        leden = conn.execute("SELECT voornaam, tussenvoegsel, achternaam, speleenheid FROM lid WHERE actief=1").fetchall()
                        print("-" * 75)
                        print(f"{'NAAM':<35} | {'SPELTAK':<20} | {'SALDO'}")
                        print("-" * 75)
                        for l in sorted(leden, key=lambda x: x[0].lower()):
                            volledige_naam = f"{l[0]} {l[1] + ' ' if l[1] else ''}{l[2]}".strip()
                            saldo = conn.execute("SELECT SUM(bedrag) FROM consumptie_log WHERE naam=?", (volledige_naam,)).fetchone()[0] or 0.0
                            print(f"{volledige_naam:<35} | {l[3]:<20} | € {float(saldo):.2f}")
                        print("-" * 75 + "\n")
                except Exception as e: color_print(f"❌ Fout bij laden leden: {e}", Fore.RED)

            elif cmd == 'producten':
                if not os.path.exists(DB_FILE): color_print("⚠️ Geen database.", Fore.RED); continue
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        rows = conn.execute("SELECT naam, prijs FROM product").fetchall()
                    print("-" * 50)
                    print(f"{'PRODUCT':<35} | {'PRIJS'}")
                    print("-" * 50)
                    for r in rows: print(f"{r[0]:<35} | € {float(r[1]):.2f}")
                    print("-" * 50 + "\n")
                except Exception as e: color_print(f"❌ Fout: {e}", Fore.RED)

            elif cmd == 'set-price':
                if len(parts) < 3:
                    color_print("⚠️ Gebruik: set-price [productnaam] [prijs]", Fore.YELLOW); continue
                p_naam, p_prijs = " ".join(parts[1:-1]), parts[-1]
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        exists = conn.execute("SELECT * FROM product WHERE naam=?", (p_naam,)).fetchone()
                        if not exists:
                            color_print(f"⚠️ Product '{p_naam}' niet gevonden.", Fore.YELLOW); continue
                        conn.execute("UPDATE product SET prijs=? WHERE naam=?", (float(p_prijs), p_naam))
                        conn.commit()
                    color_print(f"✅ Prijs van '{p_naam}' aangepast naar €{float(p_prijs):.2f}", Fore.GREEN)
                except Exception as e: color_print(f"❌ Fout: {e}", Fore.RED)

            elif cmd == 'account':
                if len(parts) < 2: 
                    color_print("⚠️ Gebruik: account [volledige naam]", Fore.YELLOW); continue
                naam = " ".join(parts[1:])
                try:
                    with sqlite3.connect(DB_FILE) as conn:
                        res = conn.execute("SELECT sum(bedrag) FROM consumptie_log WHERE naam=?", (naam,)).fetchone()[0]
                    color_print(f"\n--- ACCOUNT: {naam.upper()} ---", Fore.MAGENTA)
                    color_print(f"Actueel Saldo: € {res or 0.00:.2f}\n", Fore.WHITE)
                except Exception as e:
                    color_print(f"❌ Fout bij ophalen account: {e}", Fore.RED)

            # 6. ADMIN & VEILIGHEID
            elif cmd == 'set-pin':
                if len(parts) < 3:
                    color_print("⚠️ Gebruik: set-pin [naam] [nieuwe_pincode]", Fore.YELLOW); continue
                naam, nieuwe_pin = parts[1], parts[2]
                try:
                    hashed_pin = generate_password_hash(nieuwe_pin)
                    with sqlite3.connect(DB_FILE) as conn:
                        if not conn.execute("SELECT * FROM admin_user WHERE gebruiker=?", (naam,)).fetchone():
                            color_print(f"⚠️ Admin '{naam}' bestaat niet.", Fore.YELLOW)
                            continue
                        conn.execute("UPDATE admin_user SET pincode=? WHERE gebruiker=?", (hashed_pin, naam))
                        conn.commit()
                    color_print(f"🔒 Pincode voor admin '{naam}' succesvol gewijzigd!", Fore.GREEN)
                except Exception as e: color_print(f"❌ Fout: {e}", Fore.RED)

            elif cmd == 'add-admin':
                if len(parts) < 4:
                    color_print("⚠️ Gebruik: add-admin [naam] [pin] [rol]", Fore.YELLOW); continue
                naam, pin, rol = parts[1], parts[2], parts[3].lower()
                if rol not in ['kassa', 'penningmeester', 'beheerder', 'superadmin']:
                    color_print("⚠️ Ongeldige rol. Kies uit: kassa, penningmeester, beheerder, superadmin", Fore.YELLOW)
                    continue
                try:
                    hashed_pin = generate_password_hash(pin)
                    with sqlite3.connect(DB_FILE) as conn:
                        conn.execute("INSERT INTO admin_user (gebruiker, pincode, rol) VALUES (?, ?, ?)", (naam, hashed_pin, rol))
                        conn.commit()
                    color_print(f"✅ Admin '{naam}' ({rol}) veilig toegevoegd!", Fore.GREEN)
                except Exception as e: color_print(f"❌ Fout: {e}", Fore.RED)

            elif cmd == 'errorlog':
                if os.path.exists('errordump.txt'):
                    color_print("\n[ERRORDUMP - LAATSTE 15 REGELS]", Fore.RED)
                    print("-" * 60)
                    with open('errordump.txt', 'r') as f:
                        for l in f.readlines()[-15:]: print(l.strip())
                    print("-" * 60 + "\n")
                else: color_print("[CONSOLE] Schoon! Geen fouten gevonden.", Fore.GREEN)

            elif cmd == 'clear-errors':
                if os.path.exists('errordump.txt'):
                    os.remove('errordump.txt')
                    color_print("🧹 Errordump.txt is succesvol geleegd.", Fore.GREEN)
                else: color_print("⚠️ Geen errorlog bestand gevonden.", Fore.YELLOW)

            elif cmd == 'backup':
                voer_backup_uit()

            elif cmd == 'google-auth':
                google_authenticate_manual()

            elif cmd == 'cloud-now':
                voer_backup_uit(force_cloud=True)

            else:
                color_print(f"Onbekend commando: '{cmd}'. Typ 'help' voor een overzicht.", Fore.RED)
                
        except (KeyboardInterrupt, EOFError):
            color_print("\n[CONSOLE] Gebruik het commando 'shutdown' om veilig af te sluiten.", Fore.YELLOW)
            continue
        except Exception as e:
            color_print(f"⚠️ Onverwachte Console Fout: {e}", Fore.RED)

# --- FLASK WRAPPER ---
def start_flask():
    global flask_process
    while True:
        color_print("[SERVER] Flask-server wordt opgestart...", Fore.YELLOW)
        flask_process = subprocess.Popen(
            [sys.executable, "app.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        # Vang de output op en kleur errors rood
        for process_line in flask_process.stdout:
            if "Traceback" in process_line or "Error" in process_line:
                log_wrapper_error(process_line.strip())
                color_print(process_line.strip(), Fore.RED)
            else:
                # Gecorrigeerde geel/oranje kleur voor de server output
                color_print(process_line.strip(), Fore.LIGHTYELLOW_EX)
                
        flask_process.wait()
        log_wrapper_error("Flask server gestopt.")
        color_print("\n🔄 [SERVER UPDATE] Flask-server is afgesloten of wordt herstart...", Fore.MAGENTA)
        time.sleep(1.5)

if __name__ == '__main__':
    threading.Thread(target=backup_scheduler, daemon=True).start()
    threading.Thread(target=start_flask, daemon=True).start()
    time.sleep(1.5)
    admin_console()