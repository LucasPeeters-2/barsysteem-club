# barsysteem-club

Dit systeem is een webgebaseerde barkaart voor oorspronkelijk Scoutinggroepen
Het vervangt de papieren lijsten en synchroniseert alle data in een SQL database. Wat ook weer gesynchroniseerd wordt met de cloud. (Google Drive)

INSTALLATIE:
1. Zorg dat Python geïnstalleerd is.
2. Installeer de benodigde pakketten:
   pip install flask flask-sqlalchemy colorama werkzeug google-api-python-client google-auth-httplib2 google-auth-oauthlib

3. Plaats 'app.py' en de mappen 'templates' en 'static' in één map.
4. Zorg dat run.py ook in één map staan. 

OPSTARTEN:
1. Open een terminal/opdrachtprompt in de projectmap.
2. Start het systeem met: python RUN.py
3. Ga in de browser naar: http://localhost:5000 of zie de terminal welk IP-adres hij host. 

ADMINISTRATIE:
- Toegang tot admin: Klik op de rode knop (of ga naar /admin).
- Pincode: 191019
- Functies: Leden toevoegen/verwijderen, saldo's opwaarderen, 
            producten beheren en de log opschonen en meer!

BESTANDEN:
- run.py         : Wrapper die app.py start en beheert, met een terminal
- app.py         : De motor van het systeem (Python/Flask).
- templates/     : De schermen (HTML).
- static/        : Afbeeldingen en opmaak (CSS/JS).

Versie: 1.6.1
Ontwikkeld door: Lucas Peeters (met behulp van wat AI :P)
