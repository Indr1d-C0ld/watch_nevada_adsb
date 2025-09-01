### Descrizione ###

Il programma è un sistema di monitoraggio aereo avanzato, progettato per osservare in tempo reale tramite sito adsb.fi il traffico ADS-B che transita nello spazio aereo del Nevada, con particolare attenzione alle aree interdette e militari classificate (NTTR – Nellis Test and Training Range, poligoni R-4806/7/8/9/4810). La zone di interesse può essere facilmente modificata e personalizzata tramite modifica del file JSON contenente il poligono delle coordinate, utilizzando ad esempio il sito https://geojson.io/ .

### Funzioni principali ###

- Delimitazione geografica precisa: utilizza algoritmi leggeri di point-in-polygon (ray casting) per verificare se un velivolo entra nelle zone militari riservate.

- Rilevamento eventi: segnala l’ingresso di nuovi aerei, variazioni anomale di velocità, altitudine o traiettoria.

- Filtri configurabili: supporta un file esterno per includere/escludere velivoli in base a HEX codes (anche parziali, con wildcard).

- Registro contatti: ogni nuovo velivolo intercettato viene registrato in un log CSV con timestamp e dati identificativi.

- Notifiche opzionali: possibilità di inviare avvisi immediati via Telegram per garantire un monitoraggio a distanza.

- Funzionamento continuo: pensato per Raspberry Pi, gira come servizio di sistema systemd, con avvio automatico al boot e riavvio in caso di crash.

### Utilizzo ###

usare --polygons-file <file> per caricare poligoni di coordinate tramite JSON.

Nota: se non passi --polygons-file lo script userà poligoni di esempio (approssimativi, sulla zona di default).

usare --interval <secondi> per regolare la frequenza di controllo.

usare --notify-telegram per abilitare le notifiche Telegram, dopo aver modificato il file di servizio sistema inserendo ID del bot e della chat.

---

### Description ###

The program is an advanced aircraft monitoring system, designed to monitor ADS-B traffic in Nevada airspace in real time via the adsb.fi website, with particular attention to restricted and classified military areas (NTTR – Nellis Test and Training Range, polygons R-4806/7/8/9/4810). The area of ​​interest can be easily modified and customized by editing the JSON file containing the coordinate polygon, for example, using the website https://geojson.io/.

### Main Functions ###

- Precise geographic delimitation: uses lightweight point-in-polygon (ray casting) algorithms to verify whether an aircraft is entering restricted military zones.

- Event detection: reports the entry of new aircraft and anomalous changes in speed, altitude, or trajectory.

- Configurable filters: Supports an external file to include/exclude aircraft based on HEX codes (including partial ones, with wildcards).

- Contact log: Each new intercepted aircraft is recorded in a CSV log with timestamp and identification data.

- Optional notifications: Ability to send immediate alerts via Telegram to ensure remote monitoring.

- Continuous operation: Designed for Raspberry Pi, it runs as a systemd system service, starting automatically at boot and restarting in the event of a crash.

### Usage ###

Use --polygons-file <file> to load coordinate polygons via JSON.

Note: If you don't specify --polygons-file, the script will use sample polygons (approximate, based on the default area).

Use --interval <seconds> to adjust the check frequency.

Use --notify-telegram to enable Telegram notifications, after editing the system service file by entering bot and chat IDs.
