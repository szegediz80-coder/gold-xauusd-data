# XAU/USD Gold – 3 év H1 + D1, automatikus óránkénti frissítéssel

**Adatforrás:** Dukascopy Bank, XAU/USD **ASK**, időzóna: **UTC**. 3 éves **gördülő** időablak. Nincs szükség fizetős árfolyam-API-ra, sem API-kulcsra.

Az első automatikus lefutás tölti le a múlt 3 év H1 adatait (kb. 37 havi állomány). Ezután minden órában csak az aktuális és az előző hónap adatait kéri le újra, majd újraírja a két CSV-t.

## A fájlok

- `data/XAUUSD_H1_ASK_UTC.csv` – minden lezárt kereskedési órára: UTC kezdőidő, Open, High, Low, Close, Volume.
- `data/XAUUSD_D1_ASK_UTC.csv` – UTC napokra aggregált Open, High, Low, Close, napon belüli változás %, H1 darabszám és státusz.
- `data/STATUS.json` – utolsó sikeres ellenőrzés, legutolsó lezárt H1, sorok száma, forrás.

A `data/` könyvtár az első lefutás előtt üres lehet. A `D1` fájl a H1 sorok összesítéséből keletkezik (nem külön Dukascopy napi adatletöltésből). A UTC szerinti vasárnapi kereskedés külön napi sort is eredményezhet. Az aktuális UTC nap `IN_PROGRESS` státuszú. Az automata a még le nem zárt órát nem adja a H1 fájlhoz.

## 1. GitHub – egyszeri beállítás

1. Nyisd meg: https://github.com/ és jelentkezz be.
2. Hozz létre egy **Public** (nyilvános) adattárat `gold-xauusd-data` néven, `main` ággal és egy README-vel. A nyilvánosság csak az általános piaci adatokra vonatkozzon – személyes vásárlási adatokat ne helyezz ide.
3. Töltsd le, csomagold ki ezt a projektcsomagot, és másold a repóba a `.github/workflows/update-xauusd.yml`, `scripts/update_xauusd.py` fájlokat. A könyvtárszerkezetet pontosan tartsd meg. GitHub webes felületen: **Add file > Upload files**, vagy Git klienssel. A `.github` mappa rejtett lehet a saját számítógépeden, figyelj rá, hogy az is bekerüljön.
4. Az adattárban kattints az **Actions** fülre, majd **XAUUSD hourly data refresh > Run workflow**. Ha szükséges, engedélyezd az Actions futtatását.
5. Ha a futás zöld jelölésű, megjelennek a `data/` mappában a CSV-k és `STATUS.json`.
6. A GitHub automatikus időzítése UTC szerint **minden óra 17. percében** próbál lefutni. Az ütemezés nem garantált percre pontosan; a piaci adatok publikálása is késhet.

Ha a `git push` visszautasítás miatt a workflow nem tudja menteni az adatokat: **Settings > Actions > General > Workflow permissions > Read and write permissions**, majd ismételd meg a futtatást. A workflow maga is deklarál `contents: write` engedélyt.

**Ellenőrzés:** `data/STATUS.json` fájlban a `checked_at_utc` és `last_h1_open_utc` mezőknek frissülniük kell. Piacszünetben a legutolsó H1 változatlan maradhat.

## 2. Google Táblázatok – opcionális, automatikus megjelenítés

1. Nyisd meg: https://sheets.google.com/ > új táblázat, cím: `Gold – XAUUSD 3 év`.
2. **Fájl > Beállítások > Időzóna = UTC.**
3. **Bővítmények > Apps Script.** Másold be a `scripts/google_sheets_import.gs` fájl tartalmát.
4. A fájl elején írd át a `GITHUB_USER` értékét a saját GitHub-felhasználónevedre (a repó neve: `gold-xauusd-data`).
5. Válaszd a függvénylistából `createHourlyTrigger` és nyomd meg a **Run / Futtatás** gombot. A Google első alkalommal engedélyt kérhet (külső adat lekérése + táblázat módosítása + trigger).
6. Létrejönnek a `XAUUSD_H1`, `XAUUSD_D1`, `ALLAPOT` lapok. Ezután óránként frissülnek akkor is, ha a táblázat nincs megnyitva.

Megjegyzés: a Google Apps Script idővezérelt trigger nem percre pontos. Emiatt a Google Táblázatokban látható adatok a GitHub fájljaihoz képest további késéssel követhetik az órás gyertyákat.

## 3. Használat / értelmezés

- `Open`: az óra/nap elején érvényes ASK nyitóár.
- `High`: az időszak legmagasabb ASK ára.
- `Low`: az időszak legalacsonyabb ASK ára.
- `Close`: az időszak utolsó ASK záróára.
- `Change_Pct`: **(Close/Open - 1) × 100**, nem az előző napi záróhoz mért változás.
- `Volume`: Dukascopy-árfolyamforrás szerinti adatszám, **nem a teljes fizikai aranypiac forgalma**.
- `H1_Count`: az aznapi elérhető órás gyertyák száma. Nem mindig 24 (piaczárás, ünnep, rövid vasárnapi kereskedés).

A Dukascopy ASK jegyzése **nem azonos** a Goldtresor lakossági vételárával. Ha később a Goldtresor vételi időzítését vizsgáljuk, külön kezeljük a devizaárfolyamot (USD/HUF), spreadet, felárat és a vétel időpontját.

## Hibaelhárítás

- **Nincsenek fájlok:** Actions > legutolsó futás > piros lépés > napló, nézd a konkrét HTTP / dekódolási hibát.
- **Nincs új óra:** aranykereskedési szünetben normális; nézd a `checked_at_utc` mezőt.
- **Google Táblázatok # / HTTP 404:** először fussanak le a GitHub Actions feladatok és legyen Public a repó; ellenőrizd a `GITHUB_USER` beállítást.
- **Más napos gyertyák a grafikonon:** minden összevonás UTC dátummal történik. New York-i záráshoz igazított D1 gyertya ettől eltér.
- **Nincs 3 évnyi adat:** az első letöltés nem futott végig; hibajegyzék a GitHub Actions naplóban.

## Műszaki megjegyzés

Adatfájl-minta: `https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/10/ASK_candles_hour_1.bi5` (2026. november, a hónap **nullától** indul). A bináris havi H1 file LZMA-tömörítésű, 24 bájtos `>IIIIIf` rekordokkal; XAUUSD ár skálája 1000. A program Python standard könyvtárakat használ, nincs külső csomagigény.

Hivatalos forrás: https://www.dukascopy.com/swiss/english/marketwatch/historical/
GitHub Actions időzítés: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule
