"""
Mize MTGO Data Import Script
Discovers and imports MTGO Challenge events from fbettega/MTG_decklistcache
and computes archetype/pilot summaries into Supabase.
Includes archetype validation and mismatch correction for all supported formats.
"""

import requests
import json
import time
import re
import os
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

# ── Credentials from environment variables ───────────────────────────────────
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
GITHUB_TOKEN = os.environ['GITHUB_TOKEN']

GITHUB_API    = 'https://api.github.com'
FBETTEGA_REPO = 'fbettega/MTG_decklistcache'
FORMAT_REPO   = 'Badaro/MTGOFormatData'
MIZE_REPO     = 'phlsphr42/mize'
CUSTOM_ARCHETYPES_PATH = 'scripts/custom_archetypes.json'

headers = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'resolution=ignore-duplicates,return=minimal'
}

VALIDATABLE_FORMATS = {'Modern', 'Legacy', 'Pauper', 'Pioneer', 'Standard', 'Vintage'}

# ── Supabase helpers ──────────────────────────────────────────────────────────
SB_TIMEOUT = 30  # seconds — all Supabase requests timeout after this

def sb_get(table, params=''):
    all_rows = []
    limit    = 1000
    offset   = 0
    retries  = 3
    while True:
        sep = '&' if '?' in params else '?'
        url = f'{SUPABASE_URL}/rest/v1/{table}{params}{sep}limit={limit}&offset={offset}'
        for attempt in range(retries):
            try:
                r = requests.get(url, headers=headers, timeout=SB_TIMEOUT)
                if r.status_code != 200:
                    print(f'  Error fetching {table}: {r.status_code} {r.text[:200]}')
                    return all_rows
                batch = r.json()
                break
            except requests.exceptions.Timeout:
                print(f'  Timeout fetching {table} offset={offset} (attempt {attempt+1}/{retries})')
                if attempt < retries - 1:
                    time.sleep(5)
                else:
                    print(f'  Giving up on {table} after {retries} timeouts')
                    return all_rows
            except Exception as e:
                print(f'  Error fetching {table}: {e}')
                return all_rows
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.05)
    return all_rows

def sb_insert(table, rows, batch_size=300):
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        for attempt in range(3):
            try:
                r = requests.post(
                    f'{SUPABASE_URL}/rest/v1/{table}',
                    headers=headers,
                    data=json.dumps(batch),
                    timeout=SB_TIMEOUT
                )
                if r.status_code not in [200, 201]:
                    print(f'  Insert error on {table} batch {i}: {r.status_code} {r.text[:300]}')
                else:
                    total += len(batch)
                break
            except requests.exceptions.Timeout:
                print(f'  Timeout inserting {table} batch {i} (attempt {attempt+1})')
                if attempt < 2:
                    time.sleep(5)
        time.sleep(0.1)
    return total

def sb_delete(table, params):
    try:
        r = requests.delete(
            f'{SUPABASE_URL}/rest/v1/{table}{params}',
            headers=headers,
            timeout=SB_TIMEOUT
        )
        return r.status_code
    except requests.exceptions.Timeout:
        print(f'  Timeout deleting from {table}')
        return 0

# ── GitHub helpers ────────────────────────────────────────────────────────────
def gh_get_json(url, retries=3):
    headers = {
        'Accept': 'application/json',
        'User-Agent': 'Mize-Scraper',
        'Authorization': f'token {GITHUB_TOKEN}'
    }
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                # Rate limited — check reset time
                reset = int(r.headers.get('X-RateLimit-Reset', 0))
                wait  = max(reset - int(time.time()), 1)
                wait  = min(wait, 60)  # cap at 60s
                print(f'  Rate limited. Waiting {wait}s...')
                time.sleep(wait)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get('Retry-After', 10))
                print(f'  429 Too Many Requests. Waiting {wait}s...')
                time.sleep(wait)
                continue
            print(f'  GitHub error {r.status_code}: {url}')
            return None
        except requests.exceptions.Timeout:
            print(f'  Timeout on attempt {attempt+1}: {url}')
            if attempt < retries - 1:
                time.sleep(5)
        except Exception as e:
            print(f'  Request error: {e}')
            return None
    return None

def gh_get_raw(url, retries=3):
    headers = {
        'User-Agent': 'Mize-Scraper',
        'Authorization': f'token {GITHUB_TOKEN}'
    }
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                return r.text
            if r.status_code in (403, 429):
                wait = int(r.headers.get('Retry-After', 10))
                time.sleep(min(wait, 30))
                continue
            return None
        except requests.exceptions.Timeout:
            print(f'  Timeout on raw fetch attempt {attempt+1}')
            if attempt < retries - 1:
                time.sleep(5)
        except Exception as e:
            print(f'  Raw fetch error: {e}')
            return None
    return None

# ── Key card detection ───────────────────────────────────────────────────────
# Each archetype is identified by 3 cards that must ALL be present in mainboard

def load_key_cards():
    """Load key_cards from custom_archetypes.json in the repo."""
    url = f'{GITHUB_API}/repos/{MIZE_REPO}/contents/{CUSTOM_ARCHETYPES_PATH}'
    file_info = gh_get_json(url)
    if not file_info:
        print('  Could not fetch custom_archetypes.json')
        return {}
    raw = gh_get_raw(file_info['download_url'])
    if not raw:
        print('  Could not download custom_archetypes.json')
        return {}
    try:
        data = json.loads(raw)
        key_cards = data.get('key_cards', {})
        total = sum(len(v) for v in key_cards.values())
        print(f'  Loaded key_cards: {total} archetypes across {list(key_cards.keys())}')
        return key_cards
    except Exception as e:
        print(f'  Error parsing key_cards: {e}')
        return {}


def detect_archetype_from_deck(deck_data, key_cards_for_format):
    """Identify archetype by requiring all 3 key cards present in mainboard."""
    mainboard = {c['CardName'] for c in deck_data.get('Mainboard', [])}
    if not mainboard:
        return None
    for archetype, keys in key_cards_for_format.items():
        if all(k in mainboard for k in keys):
            return archetype
    return None


def detect_archetype_from_cards(cards_list, key_cards_for_format):
    """Identify archetype from opening hand cards (subset of deck)."""
    hand = set(c for c in cards_list if c)
    for archetype, keys in key_cards_for_format.items():
        if all(k in hand for k in keys):
            return archetype
    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f'Mize MTGO Import — {datetime.now(timezone.utc).isoformat()}')

    # ── Parse arguments ───────────────────────────────────────────────────────
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--lookback-days', type=int, default=365,
                        help='Only import events within this many days (default: 365)')
    parser.add_argument('--full', action='store_true',
                        help='Import all available history, ignoring lookback window')
    args = parser.parse_args()

    lookback_days = None if args.full else args.lookback_days
    cutoff_date   = None
    if lookback_days:
        cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=lookback_days))
        print(f'Lookback window: {lookback_days} days (events on or after {cutoff_date})')
    else:
        print('Full import mode — no date cutoff')

    import_start = time.time()
    def ts(label):
        elapsed = int(time.time() - import_start)
        m, s = divmod(elapsed, 60)
        print(f'[{m:02d}:{s:02d}] {label}', flush=True)

    # Load key cards for archetype detection
    ts('Loading key card definitions...')
    all_key_cards = load_key_cards()
    key_cards_by_format = {fmt: all_key_cards.get(fmt, {}) for fmt in VALIDATABLE_FORMATS}
    for fmt in VALIDATABLE_FORMATS:
        print(f'  {fmt}: {len(key_cards_by_format[fmt])} archetypes')

    FORMAT_PATTERNS = {
        'Modern':    ['modern-challenge', 'modern-showcase-challenge'],
        'Legacy':    ['legacy-challenge', 'legacy-showcase-challenge'],
        'Pauper':    ['pauper-challenge'],
        'Pioneer':   ['pioneer-challenge', 'pioneer-showcase-challenge'],
        'Vintage':   ['vintage-challenge'],
        'Premodern': ['premodern-challenge'],
    }

    # Discover events — only scan years within the lookback window
    current_year = datetime.now(timezone.utc).year
    if cutoff_date:
        start_year = cutoff_date.year
    else:
        start_year = 2023
    years = [str(y) for y in range(start_year, current_year + 1)]
    all_format_events = []

    ts(f'Discovering events from fbettega (scanning years: {years})...')
    for year in years:
        print(f'  Scanning {year}...')
        months = gh_get_json(f'{GITHUB_API}/repos/{FBETTEGA_REPO}/contents/Tournaments/MTGO/{year}')
        if not months:
            continue
        for month in months:
            days = gh_get_json(f'{GITHUB_API}/repos/{FBETTEGA_REPO}/contents/Tournaments/MTGO/{year}/{month["name"]}')
            if not days:
                continue
            time.sleep(0.1)
            for day in days:
                files = gh_get_json(f'{GITHUB_API}/repos/{FBETTEGA_REPO}/contents/Tournaments/MTGO/{year}/{month["name"]}/{day["name"]}')
                if not files:
                    continue
                time.sleep(0.1)
                for f in files:
                    name = f['name'].lower()
                    if not name.endswith('.json'):
                        continue
                    matched_format = None
                    for fmt, patterns in FORMAT_PATTERNS.items():
                        if any(p in name for p in patterns):
                            matched_format = fmt
                            break
                    if matched_format:
                        all_format_events.append({
                            'name':         f['name'].replace('.json', ''),
                            'download_url': f['download_url'],
                            'format':       matched_format,
                            'year':         year,
                            'month':        month['name'],
                            'day':          day['name']
                        })

    all_format_events.sort(key=lambda x: x['name'])

    # Filter by cutoff date — event name contains the date e.g. "modern-challenge-32-2024-06-15..."
    if cutoff_date:
        before = len(all_format_events)
        def event_date_str(e):
            # Extract date from event name: last 10 chars of the date portion
            # e.g. "modern-challenge-32-2024-06-1512345678" -> "2024-06-15"
            import re
            m = re.search(r'(\d{4}-\d{2}-\d{2})', e['name'])
            return m.group(1) if m else '0000-00-00'
        cutoff_str = cutoff_date.isoformat()
        all_format_events = [e for e in all_format_events if event_date_str(e) >= cutoff_str]
        print(f'Date filter: {before} → {len(all_format_events)} events (cutoff: {cutoff_str})')

    ts(f'Total events found: {len(all_format_events)}')
    for fmt, count in sorted(Counter(e['format'] for e in all_format_events).items()):
        print(f'  {fmt}: {count}')

    # Filter to only new events
    existing_events = sb_get('mtgo_events', '?select=event_id')
    existing_ids    = set(r['event_id'] for r in existing_events)
    new_events      = [e for e in all_format_events if e['name'] not in existing_ids]
    ts(f'New events to process: {len(new_events)}')
    for fmt, count in sorted(Counter(e['format'] for e in new_events).items()):
        print(f'  {fmt}: {count} new')

    # Process new events
    event_rows        = []
    result_rows       = []
    match_rows        = []
    unknown_deck_rows = []
    errors            = []
    skipped           = 0

    for idx, event in enumerate(new_events):
        raw = gh_get_raw(event['download_url'])
        if not raw:
            errors.append(event['name'])
            continue
        try:
            data = json.loads(raw)
        except:
            errors.append(event['name'])
            continue

        tournament = data.get('Tournament', {})
        standings  = data.get('Standings', [])
        decks      = data.get('Decks', [])

        event_date = tournament.get('Date', '')[:10]
        if not event_date:
            event_date = f'{event["year"]}-{event["month"]}-{event["day"]}'

        if not standings:
            skipped += 1
            continue

        decks_by_player = {d['Player']: d for d in decks}

        # Build player -> archetype map for this event
        player_arch_map = {}
        for player in standings:
            player_name = player.get('Player')
            deck_data   = decks_by_player.get(player_name, {})
            fmt              = event['format']
            key_cards_fmt    = key_cards_by_format.get(fmt, {})
            detected         = detect_archetype_from_deck(deck_data, key_cards_fmt) if deck_data else None
            player_arch_map[player_name] = detected  # None = unknown

            # Store unknown decks for admin review
            if detected is None and deck_data:
                mb = {c['CardName']: c.get('Count', 1) for c in deck_data.get('Mainboard', [])}
                sb = {c['CardName']: c.get('Count', 1) for c in deck_data.get('Sideboard', [])}
                unknown_deck_rows.append({
                    'event_id':    event['name'],
                    'player_name': player_name,
                    'format':      fmt,
                    'finish':      player.get('Rank'),
                    'mainboard':   json.dumps(mb),
                    'sideboard':   json.dumps(sb),
                    'reviewed':    False,
                    'created_at':  datetime.now(timezone.utc).isoformat()
                })

        event_rows.append({
            'event_id':   event['name'],
            'event_name': tournament.get('Name', event['name']),
            'event_date': event_date,
            'event_type': determine_event_type(event['name']),
            'format':     event['format'],
            'scraped_at': datetime.now(timezone.utc).isoformat()
        })

        for player in standings:
            player_name = player.get('Player')
            archetype   = player_arch_map.get(player_name) or 'Unknown'
            result_rows.append({
                'event_id':            event['name'],
                'player_name':         player_name,
                'archetype_raw':       archetype,
                'archetype_canonical': archetype,
                'finish_position':     player.get('Rank'),
                'points':              player.get('Points'),
                'match_win_pct':       norm_pct(player.get('OMWP')),
                'game_win_pct':        norm_pct(player.get('GWP')),
                'opp_match_win_pct':   norm_pct(player.get('OGWP')),
                'created_at':          datetime.now(timezone.utc).isoformat()
            })

        # Process rounds data for matchup tracking
        rounds = data.get('Rounds', [])
        for rnd in rounds:
            round_name = rnd.get('RoundName', '')
            for match in rnd.get('Matches', []):
                p1     = match.get('Player1')
                p2     = match.get('Player2')
                result = match.get('Result', '')
                parts  = result.split('-')
                if len(parts) != 3:
                    continue
                try:
                    p1_games = int(parts[0])
                    p2_games = int(parts[1])
                    draws    = int(parts[2])
                except ValueError:
                    continue
                match_rows.append({
                    'event_id':      event['name'],
                    'round_name':    round_name,
                    'player1':       p1,
                    'player2':       p2,
                    'player1_arch':  player_arch_map.get(p1),
                    'player2_arch':  player_arch_map.get(p2),
                    'player1_games': p1_games,
                    'player2_games': p2_games,
                    'draws':         draws
                })

        if (idx + 1) % 25 == 0:
            ts(f'  Processed {idx+1}/{len(new_events)}... ({len(result_rows)} results, {len(match_rows)} matches)')
        time.sleep(0.3)

    ts(f'Events processed: {len(event_rows)} | Skipped: {skipped} | Errors: {len(errors)}')
    print(f'Result rows: {len(result_rows)} | Match rows: {len(match_rows)}')

    # Insert new events, results, and matches
    if event_rows:
        ts(f'Inserting {len(event_rows)} events...')
        sb_insert('mtgo_events', event_rows)
    if result_rows:
        ts(f'Inserting {len(result_rows)} results...')
        sb_insert('mtgo_results', result_rows, batch_size=300)
    if match_rows:
        ts(f'Inserting {len(match_rows)} match rows...')
        sb_insert('mtgo_matches', match_rows, batch_size=300)
    if unknown_deck_rows:
        ts(f'Inserting {len(unknown_deck_rows)} unknown decks for review...')
        sb_insert('mtgo_unknown_decks', unknown_deck_rows, batch_size=100)

    # ── Validate mymtgo game log imports ──────────────────────────────────────
    ts('Validating mymtgo game log archetypes...')
    mymtgo_games = sb_get('raw_game_log',
        '?select=id,pilot_name,deck_archetype,format,card1,card2,card3,card4,card5,card6,card7,external_id'
        '&external_id=not.is.null'
    )
    print(f'Validating {len(mymtgo_games)} mymtgo games...')

    corrections = []
    for game in mymtgo_games:
        fmt           = game.get('format', 'Modern')
        declared_arch = game.get('deck_archetype', '')
        if fmt not in VALIDATABLE_FORMATS:
            continue
        key_cards_fmt = key_cards_by_format.get(fmt, {})
        if not key_cards_fmt:
            continue
        hand_cards = [game.get(f'card{i}') for i in range(1, 8) if game.get(f'card{i}')]
        if not hand_cards:
            continue
        # Check if hand matches a different archetype than declared
        corrected = detect_archetype_from_cards(hand_cards, key_cards_fmt)
        if corrected and corrected != declared_arch:
            corrections.append({
                'id':          game['id'],
                'external_id': game['external_id'],
                'pilot_name':  game['pilot_name'],
                'format':      fmt,
                'declared':    declared_arch,
                'corrected':   corrected,
                'hand':        hand_cards
            })

    print(f'Corrections needed: {len(corrections)}')
    if corrections:
        sb_hdrs = {
            'apikey':        SUPABASE_KEY,
            'Authorization': f'Bearer {SUPABASE_KEY}',
            'Content-Type':  'application/json',
            'Prefer':        'return=minimal'
        }
        for fix in corrections:
            r = requests.patch(
                f'{SUPABASE_URL}/rest/v1/raw_game_log?id=eq.{fix["id"]}',
                headers=sb_hdrs,
                data=json.dumps({'deck_archetype': fix['corrected']}),
                timeout=10
            )
            if r.status_code not in [200, 204]:
                print(f'  Error updating {fix["external_id"]}: {r.status_code}')
            time.sleep(0.05)
        print(f'Applied {len(corrections)} corrections.')

    # ── Compute summaries ─────────────────────────────────────────────────────
    ts('Computing summaries...')
    all_results      = sb_get('mtgo_results',
        '?select=event_id,player_name,archetype_canonical,finish_position,points,match_win_pct,game_win_pct,opp_match_win_pct'
    )
    all_events_db    = sb_get('mtgo_events', '?select=event_id,event_date,format')
    event_date_map   = {e['event_id']: e['event_date'] for e in all_events_db}
    event_format_map = {e['event_id']: e.get('format') for e in all_events_db}  # None if missing
    print(f'Total results: {len(all_results)} | Total events: {len(all_events_db)}')

    now     = datetime.now(timezone.utc).date()
    windows = [
        {'days': 30}, {'days': 90}, {'days': 180}, {'days': 365}, {'days': None}
    ]

    all_summaries_arch  = []
    all_summaries_pilot = []

    for w in windows:
        cutoff    = (now - timedelta(days=w['days'])) if w['days'] else None
        date_from = cutoff.isoformat() if cutoff else '2000-01-01'
        date_to   = now.isoformat()
        filtered  = [r for r in all_results if not cutoff or
                     event_date_map.get(r['event_id'], '0000-00-00') >= date_from]
        for r in filtered:
            r['_format'] = event_format_map.get(r['event_id'])  # None if event unknown
        # Drop results whose event format is unknown — prevents cross-format contamination
        filtered = [r for r in filtered if r['_format'] is not None]
        if not filtered:
            continue

        # Group by format to avoid cross-format contamination
        formats_in_window = set(r['_format'] for r in filtered)
        for fmt in sorted(formats_in_window):
            fmt_results = [r for r in filtered if r['_format'] == fmt]
            label = ('all time' if not w['days'] else f'{w["days"]}d') + f' / {fmt}'
            print(f'Window {label}: {len(fmt_results)} results')
            arch_rows  = compute_archetype_summary(fmt_results, date_from, date_to, fmt)
            pilot_rows = compute_pilot_summary(fmt_results, date_from, date_to, fmt)
            for r in arch_rows:
                r['window_days'] = w['days'] if w['days'] else 0
            for r in pilot_rows:
                r['window_days'] = w['days'] if w['days'] else 0
            all_summaries_arch.extend(arch_rows)
            all_summaries_pilot.extend(pilot_rows)

    print(f'Total archetype summary rows: {len(all_summaries_arch)}')
    print(f'Total pilot summary rows: {len(all_summaries_pilot)}')

    # Write summaries
    ts('Clearing existing summaries...')
    sb_delete('mtgo_archetype_summary', '?id=neq.00000000-0000-0000-0000-000000000000')
    sb_delete('mtgo_pilot_summary',     '?id=neq.00000000-0000-0000-0000-000000000000')
    time.sleep(1)

    ts(f'Writing {len(all_summaries_arch)} archetype summary rows...')
    sb_insert('mtgo_archetype_summary', all_summaries_arch, batch_size=300)
    ts(f'Writing {len(all_summaries_pilot)} pilot summary rows...')
    sb_insert('mtgo_pilot_summary', all_summaries_pilot, batch_size=300)

    # ── Final summary ─────────────────────────────────────────────────────────
    print('\n' + '='*60)
    print('IMPORT COMPLETE')
    print('='*60)
    print(f'New events imported:    {len(event_rows)}')
    print(f'New results imported:   {len(result_rows)}')
    print(f'New matches imported:   {len(match_rows)}')
    print(f'Archetype corrections:  {len(corrections)}')
    print(f'Unknown decks queued:   {len(unknown_deck_rows)}')
    print(f'Errors:                 {len(errors)}')

    if corrections:
        print('\nARCHETYPE CORRECTIONS APPLIED:')
        print('-'*60)
        for fix in corrections:
            print(f'  Pilot:     {fix["pilot_name"]}')
            print(f'  Game:      {fix["external_id"]}')
            print(f'  Format:    {fix["format"]}')
            print(f'  Was:       {fix["declared"]}')
            print(f'  Corrected: {fix["corrected"]}')
            print(f'  Hand:      {", ".join(c for c in fix["hand"] if c)}')
            print()

    if errors:
        print('\nEVENTS WITH ERRORS:')
        for e in errors:
            print(f'  {e}')

    print(f'\nCompleted: {datetime.now(timezone.utc).isoformat()}')

    if corrections:
        print(f'\n⚠ {len(corrections)} archetype correction(s) were applied.')
        print('Check the Actions log above for details.')

if __name__ == '__main__':
    main()
