"""
Génère un flux .ics des séances de la base Notion du coach
("Suivi de séance Mika"), publié via GitHub Pages et auquel Calendar.app
s'abonne — indépendant de l'état du Mac de l'utilisateur (contrairement à
la V2 qui pilotait Calendar.app en local via AppleScript/launchd).

Toute la logique de lecture Notion est reprise telle quelle du projet
agent-Muscu (notion_scraper.py), qui ne dépend pas de macOS. Seuls
changent : la source du cookie (variable d'env au lieu d'un fichier local)
et la sortie (fichier .ics au lieu d'appels osascript).

Contournement temporaire de l'API interne (non-officielle) de Notion, tant
que le compte a le rôle Invité — voir le CLAUDE.md du projet agent-Muscu
pour le contexte complet.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar, Event

NOTION_HOST = "https://www.notion.so"
PAGE_ID = "3039d3f5-b6e7-8192-af50-f6b74caba87f"
SPACE_ID = "ca211929-422b-4134-9f9b-e53492e87eae"

TIMEZONE = ZoneInfo("Europe/Paris")
DEFAULT_START_HOUR, DEFAULT_START_MINUTE = 8, 0
DEFAULT_END_HOUR, DEFAULT_END_MINUTE = 9, 0

REQUEST_DELAY_SECONDS = 0.4
MAX_RETRIES = 6
OUTPUT_PATH = Path(__file__).parent / "docs" / "musculation.ics"


def load_cookie() -> str:
    token = os.environ.get("NOTION_TOKEN_V2", "").strip()
    if not token:
        raise SystemExit("Variable d'environnement NOTION_TOKEN_V2 manquante ou vide.")
    return token


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


def format_event_notes(type_: str, seance_ok: bool, exercises: list[dict], freeform_texts: list[str], movement_cache: dict) -> str:
    lines = [f"Statut: {'Faite' if seance_ok else 'À venir'} | Type: {type_ or 'N/A'}", ""]

    for i, ex in enumerate(exercises, 1):
        mentions = ex.get("Mouvements", {}).get("mentions", [])
        resolved = [movement_cache.get(mid) for mid in mentions]
        resolved = [r for r in resolved if r]
        if resolved:
            name = ", ".join(resolved)
        else:
            name = ex.get("Mouvements", {}).get("text") or ex.get("Nom", {}).get("text") or f"Exercice {i}"

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


def build_ics_event(row: dict, notes: str) -> Event:
    """Construit un VEVENT. UID = ID du bloc Notion : stable et unique, donc
    Calendar.app fait lui-même la correspondance/mise à jour d'un refresh à
    l'autre — pas besoin de mécanisme d'idempotence séparé (contrairement à
    la V2, où AppleScript n'a pas d'équivalent natif à ce comportement)."""
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
    return event


def main() -> None:
    token = load_cookie()
    collection_id, table_view_id, schema = fetch_collection_info(token)
    rows = fetch_rows(token, collection_id, table_view_id)
    print(f"{len(rows)} lignes récupérées depuis Notion.")

    movement_cache: dict = {}
    cal = Calendar()
    cal.add("prodid", "-//Agent Muscu//sync_to_ics//FR")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Musculation")

    included = skipped_no_date = failed = 0

    for row in rows:
        titre = row.get("Titre")
        iso_date = row.get("Date")
        if not titre or not iso_date:
            skipped_no_date += 1
            continue

        seance_ok = row.get("Séance ok") == "Yes"
        type_ = row.get("Type", "")

        try:
            exercises, freeform_texts = fetch_session_details(token, row["_id"])
            for ex in exercises:
                for mention_id in ex.get("Mouvements", {}).get("mentions", []):
                    resolve_movement_name(token, mention_id, movement_cache)
            notes = format_event_notes(type_, seance_ok, exercises, freeform_texts, movement_cache)
            cal.add_component(build_ics_event(row, notes))
        except Exception as e:
            print(f"ERREUR sur '{titre}' ({iso_date}): {e}", file=sys.stderr)
            failed += 1
            continue

        included += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_bytes(cal.to_ical())

    print(f"Terminé. Événements inclus: {included} | Sans date/titre: {skipped_no_date} | Échecs: {failed}")
    print(f"Écrit dans {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
