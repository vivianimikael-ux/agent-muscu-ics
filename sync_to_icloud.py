"""
Synchronise les séances de la base Notion du coach ("Suivi de séance Mika")
directement vers un calendrier iCloud "Musculation" via CalDAV — visible
nativement sur tous les appareils Apple (iPhone compris), sans passer par
un abonnement .ics en lecture seule (V3) ni dépendre du Mac de
l'utilisateur (contrairement à la V2, qui pilotait Calendar.app en local
via AppleScript/launchd).

Toute la logique de lecture Notion est identique à sync_to_ics.py (V3) —
seule la destination change : upsert CalDAV au lieu d'un fichier .ics.

Contournement temporaire de l'API interne (non-officielle) de Notion, tant
que le compte a le rôle Invité — voir le CLAUDE.md du projet agent-Muscu
pour le contexte complet.
"""
from __future__ import annotations

import html
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import caldav
import requests
from icalendar import Calendar, Event

NOTION_HOST = "https://www.notion.so"
PAGE_ID = "3039d3f5-b6e7-8192-af50-f6b74caba87f"
SPACE_ID = "ca211929-422b-4134-9f9b-e53492e87eae"

ICLOUD_CALDAV_URL = "https://caldav.icloud.com/"
ICLOUD_CALENDAR_NAME = "Musculation"

EXCLUDED_TYPES = {"day off", "padel"}  # comparaison insensible à la casse

TIMEZONE = ZoneInfo("Europe/Paris")
DEFAULT_START_HOUR, DEFAULT_START_MINUTE = 8, 0
DEFAULT_END_HOUR, DEFAULT_END_MINUTE = 9, 0

REQUEST_DELAY_SECONDS = 0.4
MAX_RETRIES = 6

# Nom de fichier volontairement peu devinable ("URL obscure") plutôt qu'un
# nom prévisible comme "exercices.html" — le repo GitHub étant public, ce
# n'est pas une vraie protection d'accès, juste un frein à la découverte
# fortuite. Fixe (ne pas régénérer) pour que l'URL reste stable d'un run à
# l'autre.
HISTORY_PAGE_SLUG = "suivi-3a0b024c509595f74397.html"
HISTORY_OUTPUT_PATH = Path(__file__).parent / "docs" / HISTORY_PAGE_SLUG


def load_cookie() -> str:
    token = os.environ.get("NOTION_TOKEN_V2", "").strip()
    if not token:
        raise SystemExit("Variable d'environnement NOTION_TOKEN_V2 manquante ou vide.")
    return token


def load_icloud_credentials() -> tuple[str, str]:
    apple_id = os.environ.get("ICLOUD_APPLE_ID", "").strip()
    app_password = os.environ.get("ICLOUD_APP_PASSWORD", "").strip()
    if not apple_id or not app_password:
        raise SystemExit("Variables d'environnement ICLOUD_APPLE_ID / ICLOUD_APP_PASSWORD manquantes ou vides.")
    return apple_id, app_password


def notion_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Cookie": f"token_v2={token}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }


def notion_post(token: str, path: str, payload: dict, timeout: int = 15) -> requests.Response:
    """POST vers l'API interne Notion avec retry/backoff sur 429 (rate limit).

    L'API non-documentée de Notion rate-limite assez vite sur ~156 séances
    (observé en test : ~90 échecs 429 sur un run sans retry). Respecte
    Retry-After si fourni, sinon backoff exponentiel plafonné."""
    delay = 1.0
    resp = None
    for attempt in range(MAX_RETRIES):
        resp = requests.post(f"{NOTION_HOST}{path}", json=payload, headers=notion_headers(token), timeout=timeout)
        if resp.status_code != 429:
            return resp
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after else delay
        print(f"429 sur {path}, attente {wait:.1f}s (tentative {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
        time.sleep(wait)
        delay = min(delay * 2, 30)
    return resp


def _unwrap(record: dict) -> dict:
    """Les records Notion sont enveloppés {"value": {"value": {...}}} (parfois
    juste {"value": {...}}) — normalise vers le dict de données réel."""
    v = record.get("value", record)
    return v.get("value", v)


def load_page_chunk(token: str, page_id: str) -> dict:
    resp = notion_post(
        token,
        "/api/v3/loadPageChunk",
        {
            "pageId": page_id,
            "limit": 100,
            "cursor": {"stack": []},
            "chunkNumber": 0,
            "verticalColumns": False,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["recordMap"]


def find_table_view_for_collection(record_map: dict, collection_id: str) -> str | None:
    for cvid, cv in record_map.get("collection_view", {}).items():
        val = _unwrap(cv)
        pointer = val.get("format", {}).get("collection_pointer", {})
        if pointer.get("id") == collection_id and val.get("type") == "table":
            return cvid
    return None


def fetch_collection_info(token: str) -> tuple[str, str, dict]:
    """Retourne (collection_id, table_view_id, schema) de la base principale."""
    record_map = load_page_chunk(token, PAGE_ID)
    collections = record_map.get("collection", {})
    if not collections:
        raise SystemExit("Aucune collection trouvée sur cette page Notion.")
    collection_id, collection_record = next(iter(collections.items()))
    schema = _unwrap(collection_record)["schema"]

    table_view_id = find_table_view_for_collection(record_map, collection_id)
    if table_view_id is None:
        raise SystemExit(
            "Aucune vue de type 'table' trouvée pour interroger la collection. "
            "Vérifiez qu'une vue table existe sur la base Notion."
        )
    return collection_id, table_view_id, schema


def _parse_rich_property(prop_val: list) -> dict:
    """Extrait le texte/mentions d'une propriété Notion.

    Retourne {"text": str, "date": str|None, "mentions": [page_id, ...]} —
    "date" pour les mentions de date, "mentions" pour les mentions de page
    (utilisé par la propriété "Mouvements", une relation).
    """
    texts = []
    date_start = None
    mentions = []
    for span in prop_val:
        text = span[0]
        formats = span[1] if len(span) > 1 else []
        is_mention = False
        for fmt in formats:
            if fmt[0] == "d" and len(fmt) > 1:
                date_start = fmt[1].get("start_date")
                is_mention = True
            elif fmt[0] == "p" and len(fmt) > 1:
                mentions.append(fmt[1])
                is_mention = True
        if not is_mention and text != "‣":
            texts.append(text)
    return {"text": "".join(texts), "date": date_start, "mentions": mentions}


def fetch_rows(token: str, collection_id: str, table_view_id: str, rich: bool = False) -> list[dict]:
    """Récupère toutes les lignes d'une collection Notion.

    rich=False (défaut) : chaque propriété -> valeur texte simple (comportement
    utilisé pour la base principale des séances).
    rich=True : chaque propriété -> dict complet {"text","date","mentions"}
    (nécessaire pour la sous-base d'exercices, où "Mouvements" est une
    relation à résoudre).
    """
    resp = notion_post(
        token,
        "/api/v3/queryCollection",
        {
            "collectionId": collection_id,
            "collectionViewId": table_view_id,
            "query": {},
            "loader": {
                "type": "reducer",
                "reducers": {"results": {"type": "results", "limit": 1000}},
                "searchQuery": "",
                "userTimeZone": "Europe/Paris",
            },
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    record_map = data["recordMap"]
    blocks = record_map.get("block", {})

    schema = _unwrap(record_map["collection"][collection_id])["schema"]
    prop_names = {pid: f.get("name") for pid, f in schema.items()}

    rows = []
    for block_id, block in blocks.items():
        val = _unwrap(block)
        if val.get("parent_id") != collection_id:
            continue
        props = val.get("properties", {})
        row = {"_id": block_id, "_last_edited_time": val.get("last_edited_time")}
        for prop_id, prop_val in props.items():
            name = prop_names.get(prop_id, prop_id)
            parsed = _parse_rich_property(prop_val)
            row[name] = parsed if rich else (parsed["date"] or parsed["text"])
        rows.append(row)
    return rows


def fetch_session_details(token: str, row_id: str) -> tuple[list[dict], list[str]]:
    """Retourne (exercices, textes_libres) pour une séance (page Notion).

    Explore le contenu de la page : les blocs de type "collection_view"
    sont la sous-base d'exercices ; tout autre bloc avec du texte est
    remonté comme note libre, pour ne rien perdre du contenu de la page.
    """
    record_map = load_page_chunk(token, row_id)
    block_record = record_map.get("block", {}).get(row_id)
    if not block_record:
        return [], []
    session_val = _unwrap(block_record)

    exercises: list[dict] = []
    freeform_texts: list[str] = []

    for child_id in session_val.get("content", []) or []:
        child_record = record_map.get("block", {}).get(child_id)
        if not child_record:
            continue
        child_val = _unwrap(child_record)
        btype = child_val.get("type")

        if btype == "collection_view":
            # Le bloc "collection_view" porte directement collection_id/view_ids
            # (différents de l'id du bloc lui-même).
            sub_collection_id = child_val.get("collection_id")
            if not sub_collection_id:
                continue
            view_ids = child_val.get("view_ids") or []
            sub_table_view_id = None
            for vid in view_ids:
                vrecord = record_map.get("collection_view", {}).get(vid)
                if vrecord and _unwrap(vrecord).get("type") == "table":
                    sub_table_view_id = vid
                    break
            if sub_table_view_id is None:
                sub_table_view_id = find_table_view_for_collection(record_map, sub_collection_id) or (view_ids[0] if view_ids else None)
            if not sub_table_view_id:
                continue
            exercises.extend(fetch_rows(token, sub_collection_id, sub_table_view_id, rich=True))
        else:
            title_prop = child_val.get("properties", {}).get("title")
            if title_prop:
                text = _parse_rich_property(title_prop)["text"].strip()
                if text:
                    freeform_texts.append(text)

    return exercises, freeform_texts


def resolve_movement_name(token: str, page_id: str, cache: dict) -> None:
    """Résout le titre d'une page référencée par une mention (ex: nom d'un
    exercice). Peuple `cache[page_id]`. No-op si déjà en cache."""
    if page_id in cache:
        return
    time.sleep(REQUEST_DELAY_SECONDS)
    resp = notion_post(
        token,
        "/api/v3/syncRecordValues",
        {"requests": [{"pointer": {"table": "block", "id": page_id, "spaceId": SPACE_ID}, "version": -1}]},
        timeout=15,
    )
    name = None
    if resp.ok:
        block_record = resp.json().get("recordMap", {}).get("block", {}).get(page_id)
        if block_record:
            val = _unwrap(block_record)
            title_prop = val.get("properties", {}).get("title")
            if title_prop:
                name = _parse_rich_property(title_prop)["text"].strip() or None
    cache[page_id] = name or "(exercice inconnu)"


def exercise_display_name(ex: dict, movement_cache: dict, fallback_index: int) -> str:
    mentions = ex.get("Mouvements", {}).get("mentions", [])
    resolved = [movement_cache.get(mid) for mid in mentions]
    resolved = [r for r in resolved if r]
    if resolved:
        return ", ".join(resolved)
    return ex.get("Mouvements", {}).get("text") or ex.get("Nom", {}).get("text") or f"Exercice {fallback_index}"


def format_event_notes(type_: str, seance_ok: bool, exercises: list[dict], freeform_texts: list[str], movement_cache: dict) -> str:
    lines = [f"Statut: {'Faite' if seance_ok else 'À venir'} | Type: {type_ or 'N/A'}", ""]

    for i, ex in enumerate(exercises, 1):
        name = exercise_display_name(ex, movement_cache, i)

        series = ex.get("Séries", {}).get("text", "")
        reps = ex.get("Répétitions", {}).get("text", "")
        charge_cible = ex.get("Charge cible", {}).get("text", "")
        charge_reelle = ex.get("Charge réelle", {}).get("text", "")
        recuperation = ex.get("Récupération", {}).get("text", "")
        details = ex.get("Détails", {}).get("text", "")

        lines.append(f"{i}. {name}")

        set_rep_parts = [p for p in (f"Séries: {series}" if series else "", f"Répétitions: {reps}" if reps else "") if p]
        if set_rep_parts:
            lines.append("   " + " | ".join(set_rep_parts))

        charge_parts = [p for p in (f"Charge cible: {charge_cible}" if charge_cible else "", f"Charge réelle: {charge_reelle}" if charge_reelle else "") if p]
        if charge_parts:
            lines.append("   " + " | ".join(charge_parts))
        if recuperation:
            lines.append(f"   Récupération: {recuperation}")
        if details:
            lines.append(f"   {details}")
        lines.append("")

    if freeform_texts:
        lines.append("--- Notes libres ---")
        lines.extend(freeform_texts)
        lines.append("")

    lines.append("(Synchronisé depuis Notion)")
    return "\n".join(lines)


def record_exercise_occurrence(history: dict, movement_cache: dict, row: dict, exercises: list[dict]) -> None:
    """Ajoute à l'historique de chaque exercice tout enregistrement où une
    "Charge réelle" a été renseignée (une occurrence par séance où
    l'exercice a réellement été loggé — pas juste programmé). Garde tout
    l'historique, pas seulement le plus récent, pour la vue "progression"."""
    iso_date = row.get("Date", "")
    titre = row.get("Titre", "")

    for i, ex in enumerate(exercises, 1):
        charge_reelle = ex.get("Charge réelle", {}).get("text", "").strip()
        if not charge_reelle:
            continue
        name = exercise_display_name(ex, movement_cache, i)
        history.setdefault(name, []).append({
            "date": iso_date,
            "titre_seance": titre,
            "charge_reelle": charge_reelle,
            "charge_cible": ex.get("Charge cible", {}).get("text", "").strip(),
            "series": ex.get("Séries", {}).get("text", "").strip(),
            "reps": ex.get("Répétitions", {}).get("text", "").strip(),
        })


MONTHS_FR = ["jan.", "fév.", "mars", "avr.", "mai", "juin", "juil.", "aoû.", "sept.", "oct.", "nov.", "déc."]


def format_date_fr(iso_date: str) -> str:
    y, m, d = iso_date.split("-")
    return f"{int(d)} {MONTHS_FR[int(m) - 1]}"


def format_weight_reps(entry: dict) -> str:
    """Formatage "au mieux" façon 4×8 @ 82.5kg. Les données Notion sont du
    texte libre (pas toujours des nombres propres : "Barre + 40kg", "10
    répétitions par exercice D/G"...) — on ne force le format compact que
    quand séries/reps sont des entiers simples et que la charge est un
    nombre + kg ; sinon on retombe sur le texte brut tel quel."""
    series, reps, charge = entry["series"], entry["reps"], entry["charge_reelle"]

    if re.fullmatch(r"\d+", series or "") and re.fullmatch(r"\d+", reps or ""):
        sxr = f"{series}×{reps}"
    else:
        sxr = " / ".join(p for p in (series, reps) if p)

    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*kg", charge.strip(), re.I)
    weight = f"@ {m.group(1)}kg" if m else charge

    return f"{sxr} {weight}".strip() if sxr else weight


def build_history_html(history: dict) -> str:
    all_entries = []
    exercise_names = sorted(history.keys(), key=str.casefold)
    for name in exercise_names:
        for r in history[name]:
            all_entries.append((name, r))
    all_entries.sort(key=lambda item: item[1]["date"], reverse=True)

    session_count = len({(r["date"], r["titre_seance"]) for _, r in all_entries})

    options_html = "".join(f'<option value="{html.escape(n)}">{html.escape(n)}</option>' for n in exercise_names)

    rows_html = []
    for name, r in all_entries:
        rows_html.append(f"""
      <tr data-exercise="{html.escape(name)}">
        <td class="ex">{html.escape(name)}</td>
        <td class="wr">{html.escape(format_weight_reps(r))}</td>
        <td class="date">{html.escape(format_date_fr(r['date']))}</td>
      </tr>""")

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Mika Training — Historique</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    margin: 0; padding: 1.25rem 1rem 3rem;
    background: #0a0a0d; color: #f0f0f2;
  }}
  .mono {{ font-family: "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; }}
  header {{ display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: .5rem; margin-bottom: 1.25rem; }}
  h1 {{ font-size: 1.4rem; font-weight: 800; margin: 0; letter-spacing: -.01em; }}
  .subtitle {{ font-size: .8rem; color: #7a7a85; margin-left: .6rem; }}
  .subtitle.mono {{ letter-spacing: .02em; }}
  nav.tabs {{ display: flex; gap: 1.5rem; border-bottom: 1px solid #1c1c22; margin-bottom: 1.25rem; }}
  nav.tabs span {{ font-size: .95rem; color: #55555f; padding-bottom: .6rem; }}
  nav.tabs span.active {{ color: #f0f0f2; font-weight: 600; border-bottom: 2px solid #2dd4a0; }}
  .filter-row {{ display: flex; align-items: center; gap: .6rem; margin-bottom: 1rem; }}
  .filter-row label {{ font-size: .8rem; color: #7a7a85; }}
  select#filter {{
    background: #14141a; color: #f0f0f2; border: 1px solid #262630; border-radius: .5rem;
    padding: .5rem .7rem; font-size: .9rem; font-family: inherit; min-width: 220px;
  }}
  .table-wrap {{ width: 100%; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  thead th {{
    text-align: left; font-size: .7rem; letter-spacing: .04em; text-transform: uppercase;
    color: #6a6a75; font-weight: 500; padding: .6rem .5rem; border-bottom: 1px solid #1c1c22;
  }}
  thead th.wr, thead th.date {{ text-align: right; }}
  tbody td {{ padding: .8rem .5rem; border-bottom: 1px solid #16161b; vertical-align: middle; }}
  tbody tr.hidden {{ display: none; }}
  td.ex {{ font-weight: 600; }}
  td.wr {{ text-align: right; white-space: nowrap; }}
  td.date {{ text-align: right; color: #7a7a85; white-space: nowrap; }}
  p.count {{ color: #55555f; font-size: .8rem; margin-top: 1rem; }}

  /* En dessous de 640px : chaque ligne devient une carte empilée au lieu
     de colonnes côte à côte qui débordent hors de l'écran. */
  @media (max-width: 640px) {{
    select#filter {{ min-width: 0; width: 100%; }}
    table, thead, tbody, tr, td {{ display: block; width: 100%; }}
    thead {{ display: none; }}
    tbody tr {{ padding: .7rem 0; border-bottom: 1px solid #16161b; }}
    tbody td {{ border: none; padding: .1rem 0; text-align: left; white-space: normal; }}
    td.ex {{ font-size: 1rem; }}
    td.wr::before {{ content: "Répétitions × Poids : "; color: #6a6a75; font-size: .72rem; }}
    td.date::before {{ content: "Date : "; color: #6a6a75; font-size: .72rem; }}
  }}
</style>
</head>
<body>
<header>
  <div><h1 style="display:inline">Mika Training</h1><span class="subtitle mono">{session_count} séances trackées</span></div>
</header>
<nav class="tabs"><span class="active">Historique</span></nav>
<div class="filter-row">
  <label for="filter">Exercice</label>
  <select id="filter" class="mono">
    <option value="">Tous les exercices</option>
    {options_html}
  </select>
</div>
<div class="table-wrap">
<table>
  <thead>
    <tr><th>Exercice</th><th class="wr">Répétitions × Poids</th><th class="date">Date</th></tr>
  </thead>
  <tbody class="mono">{"".join(rows_html)}
  </tbody>
</table>
</div>
<p class="count"><span id="visible-count">{len(all_entries)}</span> / {len(all_entries)} entrées — généré automatiquement depuis Notion</p>
<script>
  const select = document.getElementById('filter');
  const rows = Array.from(document.querySelectorAll('tbody tr'));
  const countEl = document.getElementById('visible-count');
  select.addEventListener('change', () => {{
    const q = select.value;
    let visible = 0;
    for (const row of rows) {{
      const match = !q || row.dataset.exercise === q;
      row.classList.toggle('hidden', !match);
      if (match) visible++;
    }}
    countEl.textContent = visible;
  }});
</script>
</body>
</html>
"""


def build_ical_bytes(row: dict, notes: str) -> bytes:
    """Construit un VCALENDAR à un seul VEVENT, sérialisé pour un upload
    CalDAV. UID = ID du bloc Notion : stable et unique, sert de clé
    d'upsert (recherche via calendar.event_by_uid)."""
    y, m, d = (int(part) for part in row["Date"].split("-"))
    start = datetime(y, m, d, DEFAULT_START_HOUR, DEFAULT_START_MINUTE, tzinfo=TIMEZONE)
    end = datetime(y, m, d, DEFAULT_END_HOUR, DEFAULT_END_MINUTE, tzinfo=TIMEZONE)

    event = Event()
    event.add("uid", f"{row['_id']}@agent-muscu")
    event.add("summary", row["Titre"])
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("description", notes)
    event.add("dtstamp", datetime.now(tz=ZoneInfo("UTC")))

    cal = Calendar()
    cal.add("prodid", "-//Agent Muscu//sync_to_icloud//FR")
    cal.add("version", "2.0")
    cal.add_component(event)
    return cal.to_ical()


def get_or_create_calendar(principal: caldav.Principal) -> caldav.Calendar:
    for cal in principal.calendars():
        if cal.name == ICLOUD_CALENDAR_NAME:
            return cal
    return principal.make_calendar(name=ICLOUD_CALENDAR_NAME)


def build_uid_index(calendar: caldav.Calendar) -> dict:
    """Construit un index uid -> objet événement à partir d'un listing complet.

    calendar.event_by_uid() (recherche REPORT filtrée par UID) renvoie
    systématiquement 412 Precondition Failed contre le serveur CalDAV
    d'iCloud avec cette version de la lib caldav — y compris pour un UID
    qui n'existe pas. calendar.events() (listing simple, sans filtre)
    fonctionne bien et renvoie déjà le contenu de chaque événement (aucun
    appel réseau supplémentaire), donc on construit l'index nous-mêmes une
    seule fois par run plutôt que de chercher un par un."""
    index = {}
    for ev in calendar.events():
        try:
            parsed = Calendar.from_ical(ev.data)
        except ValueError:
            continue
        for component in parsed.walk("VEVENT"):
            index[str(component.get("uid"))] = ev
            break
    return index


def upsert_event(calendar: caldav.Calendar, uid_index: dict, row: dict, notes: str) -> str:
    """Crée ou met à jour l'événement iCloud correspondant à cette séance.
    Retourne "created" ou "updated"."""
    uid = f"{row['_id']}@agent-muscu"
    ical_bytes = build_ical_bytes(row, notes)
    existing = uid_index.get(uid)
    if existing is not None:
        existing.data = ical_bytes
        existing.save()
        return "updated"
    new_event = calendar.save_event(ical_bytes)
    uid_index[uid] = new_event
    return "created"


def main() -> None:
    notion_token = load_cookie()
    apple_id, app_password = load_icloud_credentials()

    client = caldav.DAVClient(url=ICLOUD_CALDAV_URL, username=apple_id, password=app_password)
    principal = client.principal()
    calendar = get_or_create_calendar(principal)
    uid_index = build_uid_index(calendar)
    print(f"Calendrier iCloud '{ICLOUD_CALENDAR_NAME}' prêt ({len(uid_index)} événements existants).")

    collection_id, table_view_id, schema = fetch_collection_info(notion_token)
    rows = fetch_rows(notion_token, collection_id, table_view_id)
    print(f"{len(rows)} lignes récupérées depuis Notion.")

    movement_cache: dict = {}
    exercise_history: dict = {}
    created = updated = skipped_no_date = skipped_excluded = deleted_excluded = failed = 0

    for row in rows:
        titre = row.get("Titre")
        iso_date = row.get("Date")
        if not titre or not iso_date:
            skipped_no_date += 1
            continue

        seance_ok = row.get("Séance ok") == "Yes"
        type_ = row.get("Type", "")

        if type_.strip().lower() in EXCLUDED_TYPES:
            skipped_excluded += 1
            uid = f"{row['_id']}@agent-muscu"
            existing = uid_index.get(uid)
            if existing is not None:
                existing.delete()
                del uid_index[uid]
                deleted_excluded += 1
            continue

        try:
            exercises, freeform_texts = fetch_session_details(notion_token, row["_id"])
            for ex in exercises:
                for mention_id in ex.get("Mouvements", {}).get("mentions", []):
                    resolve_movement_name(notion_token, mention_id, movement_cache)
            notes = format_event_notes(type_, seance_ok, exercises, freeform_texts, movement_cache)
            record_exercise_occurrence(exercise_history, movement_cache, row, exercises)
            result = upsert_event(calendar, uid_index, row, notes)
        except Exception as e:
            print(f"ERREUR sur '{titre}' ({iso_date}): {e}", file=sys.stderr)
            failed += 1
            continue

        if result == "created":
            created += 1
        else:
            updated += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    HISTORY_OUTPUT_PATH.parent.mkdir(exist_ok=True)
    HISTORY_OUTPUT_PATH.write_text(build_history_html(exercise_history), encoding="utf-8")
    robots_path = HISTORY_OUTPUT_PATH.parent / "robots.txt"
    if not robots_path.exists():
        robots_path.write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    print(f"Page de suivi ({len(exercise_history)} exercices) écrite dans {HISTORY_OUTPUT_PATH}")

    print(
        f"Terminé. Créés: {created} | Mis à jour: {updated} | "
        f"Exclus (Padel/Day off): {skipped_excluded} (dont {deleted_excluded} supprimés d'iCloud) | "
        f"Sans date/titre: {skipped_no_date} | Échecs: {failed}"
    )


if __name__ == "__main__":
    main()
