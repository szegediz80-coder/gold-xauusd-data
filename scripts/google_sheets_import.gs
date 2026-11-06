/**
 * Google Táblázatok > Bővítmények > Apps Script.
 * Előbb cseréld ki a GITHUB_USER értékét a saját felhasználónevedre.
 * A GitHub tárolónak NYILVÁNOSNAK kell lennie a tokenmentes működéshez.
 * A táblázat időzónája UTC legyen (Fájl > Beállítások).
 */
const GITHUB_USER = 'IDE_IRD_A_GITHUB_FELHASZNALONEVED';
const REPO_NAME = 'gold-xauusd-data';
const BRANCH = 'main';

function fetchCsvFile(filename) {
  if (GITHUB_USER === 'IDE_IRD_A_GITHUB_FELHASZNALONEVED') {
    throw new Error('Állítsd be a GITHUB_USER változót!');
  }
  const base = `https://raw.githubusercontent.com/${GITHUB_USER}/${REPO_NAME}/${BRANCH}/data/`;
  const url = base + filename + '?t=' + Date.now();
  const response = UrlFetchApp.fetch(url, {muteHttpExceptions: true});
  if (response.getResponseCode() !== 200) {
    throw new Error('GitHub adatletöltési hiba (' + response.getResponseCode() + '): ' + url);
  }
  return Utilities.parseCsv(response.getContentText('UTF-8'));
}

function syncSheet(name, filename, dateColumnType) {
  const raw = fetchCsvFile(filename);
  if (raw.length <= 1) throw new Error('Üres adatfájl: ' + filename);
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(name) || ss.insertSheet(name);
  sheet.clearContents();
  const header = raw[0];
  const values = raw.slice(1).map(r => r.map((cell, i) => {
    if (i === 0) {
      return dateColumnType === 'date' ? new Date(cell + 'T00:00:00Z') : new Date(cell);
    }
    if (cell === 'IN_PROGRESS' || cell === 'CLOSED') return cell;
    return cell === '' ? '' : Number(cell);
  }));
  sheet.getRange(1, 1, 1, header.length).setValues([header]);
  for (let i = 0; i < values.length; i += 2000) {
    const chunk = values.slice(i, i + 2000);
    sheet.getRange(i + 2, 1, chunk.length, header.length).setValues(chunk);
  }
  sheet.getRange('A:A').setNumberFormat(dateColumnType === 'date' ? 'yyyy-mm-dd' : 'yyyy-mm-dd hh:mm');
  sheet.getRange(2, 2, values.length, 4).setNumberFormat('0.000');
  sheet.getRange(1, 1, 1, header.length).setFontWeight('bold').setBackground('#e8f0e9');
  sheet.setFrozenRows(1);
  return values.length;
}

function refreshGold() {
  const h1 = syncSheet('XAUUSD_H1', 'XAUUSD_H1_ASK_UTC.csv', 'datetime');
  const d1 = syncSheet('XAUUSD_D1', 'XAUUSD_D1_ASK_UTC.csv', 'date');
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const status = ss.getSheetByName('ALLAPOT') || ss.insertSheet('ALLAPOT');
  status.getRange('A1:B5').setValues([
    ['Forrás', 'Dukascopy XAU/USD ASK'],
    ['Időzóna', 'UTC'],
    ['Google Sheets ellenőrzés', new Date()],
    ['H1 gyertyák száma', h1],
    ['Napi gyertyák száma', d1]
  ]);
  status.getRange('B3').setNumberFormat('yyyy-mm-dd hh:mm:ss');
}

function createHourlyTrigger() {
  const existing = ScriptApp.getProjectTriggers();
  existing.filter(t => t.getHandlerFunction() === 'refreshGold').forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('refreshGold').timeBased().everyHours(1).create();
  refreshGold();
}
