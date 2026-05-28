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

headers = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
    'Prefer': 'resolution=ignore-duplicates,return=minimal'
}

# Formats that have archetype definitions available
VALIDATABLE_FORMATS = {'Modern', 'Legacy', 'Pauper', 'Pioneer', 'Standard', 'Vintage'}

# ── Supabase helpers ─────────────────────────────────────────────────────────
def sb_get(table, params=''):
    all_rows = []
    limit = 1000
    offset = 0
    while True:
        sep = '&' if '?' in params else '?'
        url = f'{SUPABASE_URL}/rest/v1/{table}{params}{sep}limit={limit}&offset={offset}'
        r = requests.get(url, headers=headers)
        if r.status_code != 200:
            print(f'Error fetching {table}: {r.status_code} {r.text[:200]}')
            break
        batch = r.json()
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
        r = requests.post(
            f'{SUPABASE_URL}/rest/v1/{table}',
            headers=headers,
            data=json.dumps(batch)
        )
        if r.status_code not in [200, 201]:
            print(f'Insert error on {table} batch {i}: {r.status_code} {r.text[:300]}')
        else:
            total += len(batch)
        time.sleep(0.1)
    return total

def sb_delete(table, params):
    r = requests.delete(
        f'{SUPABASE_URL}/rest/v1/{table}{params}',
        headers=headers
    )
    return r.status_code

# ── GitHub helpers ────────────────────────────────────────────────────────────
def gh_get_json(url):
    r = requests.get(url, headers={
        'Accept': 'application/json',
        'User-Agent': 'Mize-Scraper',
        'Authorization': f'token {GITHUB_TOKEN}'
    })
    if r.status_code == 200:
        return r.json()
    print(f'GitHub error {r.status_code}: {url}')
    return None

def gh_get_raw(url):
    r = requests.get(url, headers={
        'User-Agent': 'Mize-Scraper',
        'Authorization': f'token {GITHUB_TOKEN}'
    })
    if r.status_code == 200:
        return r.text
    return None

# ── Archetype definitions ─────────────────────────────────────────────────────
def load_archetype_defs_for_format(fmt):
    """Load archetype definitions for a specific format from Badaro/MTGOFormatData."""
    # Map our format names to folder names in the repo
    folder_map = {
        'Modern':    'Modern',
        'Legacy':    'Legacy',
        'Pauper':    'Pauper',
        'Pioneer':   'Pioneer',
        'Standard':  'Standard',
        'Vintage':   'Vintage',
    }
    folder = folder_map.get(fmt)
    if not folder:
        return []

    arch_url   = f'{GITHUB_API}/repos/{FORMAT_REPO}/contents/Formats/{folder}/Archetypes'
    arch_files = gh_get_json(arch_url)
    if not arch_files:
        print(f'  Could not fetch {fmt} archetype definitions.')
        return []

    arch_defs = []
    for f in arch_files:
        if not f['name'].endswith('.json'):
            continue
        name    = f['name'].replace('.json', '')
        raw_def = gh_get_raw(f['download_url'])
        if raw_def:
            try:
                d        = json.loads(raw_def)
                d['_name'] = name
                arch_defs.append(d)
            except:
                pass
        time.sleep(0.05)
    return arch_defs

def load_all_arch_defs():
    """Load archetype definitions for all validatable formats."""
    all_defs = {}
    for fmt in VALIDATABLE_FORMATS:
        print(f'  Loading {fmt} archetype definitions...')
        defs = load_archetype_defs_for_format(fmt)
        all_defs[fmt] = defs
        print(f'    {len(defs)} definitions loaded')
    return all_defs

# ── Archetype detection ───────────────────────────────────────────────────────
def test_conditions(conditions, mainboard, sideboard):
    if not conditions:
        return False
    mb   = {c.lower(): q for c, q in mainboard.items()}
    sb   = {c.lower(): q for c, q in sideboard.items()}
    both = dict(mb)
    for c, q in sb.items():
        both[c] = both.get(c, 0) + q
    for cond in conditions:
        t     = cond.get('Type', '')
        cards = [c.lower() for c in cond.get('Cards', [])]
        if t == 'InMainboard':
            if not all(mb.get(c, 0) > 0 for c in cards): return False
        elif t == 'InSideboard':
            if not all(sb.get(c, 0) > 0 for c in cards): return False
        elif t == 'InMainOrSideboard':
            if not all(both.get(c, 0) > 0 for c in cards): return False
        elif t == 'OneOrMoreInMainboard':
            if not any(mb.get(c, 0) > 0 for c in cards): return False
        elif t == 'OneOrMoreInSideboard':
            if not any(sb.get(c, 0) > 0 for c in cards): return False
        elif t == 'OneOrMoreInMainOrSideboard':
            if not any(both.get(c, 0) > 0 for c in cards): return False
        elif t == 'TwoOrMoreInMainboard':
            if sum(1 for c in cards if mb.get(c, 0) > 0) < 2: return False
        elif t == 'TwoOrMoreInSideboard':
            if sum(1 for c in cards if sb.get(c, 0) > 0) < 2: return False
        elif t == 'TwoOrMoreInMainOrSideboard':
            if sum(1 for c in cards if both.get(c, 0) > 0) < 2: return False
        elif t == 'DoesNotContain':
            if any(both.get(c, 0) > 0 for c in cards): return False
        elif t == 'DoesNotContainMainboard':
            if any(mb.get(c, 0) > 0 for c in cards): return False
        elif t == 'DoesNotContainSideboard':
            if any(sb.get(c, 0) > 0 for c in cards): return False
    return True

def detect_archetype_from_cards(cards_list, arch_defs):
    """Detect archetype from a list of card names (opening hand)."""
    # Treat opening hand cards as mainboard for matching purposes
    mainboard = {}
    for card in cards_list:
        if card:
            mainboard[card] = mainboard.get(card, 0) + 1
    sideboard = {}
    matches = []
    for d in arch_defs:
        if test_conditions(d.get('Conditions', []), mainboard, sideboard):
            matches.append(d['_name'])
    return matches[0] if matches else None

def detect_archetype_from_deck(deck_data, arch_defs):
    """Detect archetype from full deck data (mainboard + sideboard)."""
    mainboard = {}
    sideboard = {}
    for c in deck_data.get('Mainboard', []):
        mainboard[c['CardName']] = c.get('Count', 1)
    for c in deck_data.get('Sideboard', []):
        sideboard[c['CardName']] = c.get('Count', 1)
    matches = []
    for d in arch_defs:
        if test_conditions(d.get('Conditions', []), mainboard, sideboard):
            matches.append(d['_name'])
    return matches[0] if matches else 'Unknown'

def validate_archetype(game, arch_defs_by_format):
    """
    Validate that a game's declared archetype matches its opening hand cards.
    Returns (is_valid, corrected_archetype) where corrected_archetype is None
    if no better match is found.
    """
    fmt           = game.get('format', 'Modern')
    declared_arch = game.get('deck_archetype', '')

    # Skip validation for formats without definitions
    if fmt not in VALIDATABLE_FORMATS:
        return True, None

    arch_defs = arch_defs_by_format.get(fmt, [])
    if not arch_defs:
        return True, None

    # Get the declared archetype's definition
    declared_def = next((d for d in arch_defs if d['_name'] == declared_arch), None)

    # If we don't have a definition for the declared archetype, skip validation
    if not declared_def:
        return True, None

    # Get opening hand cards
    hand_cards = [game.get(f'card{i}') for i in range(1, 8) if game.get(f'card{i}')]
    if not hand_cards:
        return True, None

    # Check if the declared archetype's conditions are met by the hand
    mainboard = {card: hand_cards.count(card) for card in set(hand_cards)}
    is_valid  = test_conditions(declared_def.get('Conditions', []), mainboard, {})

    if is_valid:
        return True, None

    # Try to find a better match from the opening hand
    corrected = detect_archetype_from_cards(hand_cards, arch_defs)
    return False, corrected

# ── Utility functions ─────────────────────────────────────────────────────────
def determine_event_type(event_name):
    name_lower = event_name.lower()
    if 'showcase' in name_lower: return 'Showcase Challenge'
    elif '64' in name_lower:     return 'Challenge 64'
    elif '32' in name_lower:     return 'Challenge 32'
    else:                        return 'Challenge'

def norm_pct(val):
    if val is None: return None
    try:
        f = float(val)
        return round(f / 100 if f > 1 else f, 6)
    except:
        return None

def avg(arr):
    arr = [x for x in arr if x is not None]
    return sum(arr) / len(arr) if arr else None

# ── Summary computation ───────────────────────────────────────────────────────
def compute_archetype_summary(results, date_from, date_to):
    by_arch = defaultdict(lambda: {
        'appearances': [], 'top8': 0, 'events': set(),
        'points': [], 'mwp': [], 'gwp': [], 'omwp': [],
        'format': 'Modern'
    })
    total = len(results)
    for r in results:
        arch = r.get('archetype_canonical') or 'Unknown'
        pos  = r.get('finish_position')
        by_arch[arch]['appearances'].append(pos)
        by_arch[arch]['events'].add(r['event_id'])
        by_arch[arch]['format'] = r.get('_format', 'Modern')
        if pos and pos <= 8:
            by_arch[arch]['top8'] += 1
        for key, field in [('points','points'),('mwp','match_win_pct'),
                            ('gwp','game_win_pct'),('omwp','opp_match_win_pct')]:
            if r.get(field) is not None:
                by_arch[arch][key].append(r[field])
    rows = []
    for arch, d in by_arch.items():
        top32       = len(d['appearances'])
        top32_share = top32 / total if total > 0 else 0
        top8_rate   = d['top8'] / top32 if top32 > 0 else 0
        avg_finish  = avg(d['appearances'])
        avg_pts     = avg(d['points'])
        avg_mwp     = avg(d['mwp'])
        avg_gwp     = avg(d['gwp'])
        avg_omwp    = avg(d['omwp'])
        perf = (
            ((avg_pts / 18) * 0.40 if avg_pts is not None else 0) +
            ((avg_mwp or 0) * 0.25) +
            ((avg_gwp or 0) * 0.15) +
            ((avg_omwp or 0) * 0.20)
        )
        meta_adj = perf / top32_share if top32_share > 0 else 0
rows.append({
            'archetype_name':        arch,
            'format':                d['format'],
            'date_from':             date_from,
            'date_to':               date_to,
            'event_count':           len(d['events']),
            'top32_appearances':     top32,
            'top8_appearances':      d['top8'],
            'top32_share':           round(top32_share, 6),
            'top8_rate':             round(top8_rate, 6),
            'avg_finish':            round(avg_finish, 2) if avg_finish else None,
            'avg_points':            round(avg_pts, 2) if avg_pts else None,
            'avg_mwp':               round(avg_mwp, 6) if avg_mwp else None,
            'avg_gwp':               round(avg_gwp, 6) if avg_gwp else None,
            'avg_omwp':              round(avg_omwp, 6) if avg_omwp else None,
            'performance_score':     round(perf, 6),
            'meta_adjusted_score':   round(meta_adj, 6),
            'raw_performance_score': round(
                (((avg_mwp * top32) + (0.5 * 20)) / (top32 + 20))
                * math.log(max(top32, 1) + 1)
                * (1 + top8_rate), 6
            ) if avg_mwp is not None else 0,
            'last_updated':          datetime.now(timezone.utc).isoformat()
        })
    return sorted(rows, key=lambda x: x['meta_adjusted_score'], reverse=True)

def compute_pilot_summary(results, date_from, date_to):
    # Key by (pilot, format) instead of just pilot
    by_pilot = defaultdict(lambda: {
        'appearances': [], 'top8': 0, 'events': set(),
        'points': [], 'mwp': [], 'gwp': [], 'omwp': [],
        'archetypes': defaultdict(int),
        'format': 'Modern'
    })
    for r in results:
        p = r.get('player_name')
        if not p: continue
        fmt = r.get('_format', 'Modern')
        key = (p, fmt)
        pos = r.get('finish_position')
        by_pilot[key]['appearances'].append(pos)
        by_pilot[key]['events'].add(r['event_id'])
        by_pilot[key]['format'] = fmt
        if pos and pos <= 8:
            by_pilot[key]['top8'] += 1
        arch = r.get('archetype_canonical') or 'Unknown'
        by_pilot[key]['archetypes'][arch] += 1
        for k, field in [('points','points'),('mwp','match_win_pct'),
                          ('gwp','game_win_pct'),('omwp','opp_match_win_pct')]:
            if r.get(field) is not None:
                by_pilot[key][k].append(r[field])
    rows = []
    for (pilot, fmt), d in by_pilot.items():
        avg_pts  = avg(d['points'])
        avg_mwp  = avg(d['mwp'])
        avg_gwp  = avg(d['gwp'])
        avg_omwp = avg(d['omwp'])
        primary  = sorted(d['archetypes'].items(), key=lambda x: x[1], reverse=True)
        primary_arch = primary[0][0] if primary else 'Unknown'
        total_pts    = sum(d['points']) if d['points'] else 0
        appearances  = [x for x in d['appearances'] if x is not None]
        perf = (
            ((avg_pts / 18) * 0.40 if avg_pts is not None else 0) +
            ((avg_mwp or 0) * 0.25) +
            ((avg_gwp or 0) * 0.15) +
            ((avg_omwp or 0) * 0.20)
        )
        rows.append({
            'player_name':        pilot,
            'format':             fmt,
            'date_from':          date_from,
            'date_to':            date_to,
            'top32_appearances':  len(d['appearances']),
            'top8_appearances':   d['top8'],
            'best_finish':        min(appearances) if appearances else None,
            'avg_finish':         round(avg(d['appearances']), 2) if avg(d['appearances']) else None,
            'avg_points':         round(avg_pts, 2) if avg_pts else None,
            'avg_mwp':            round(avg_mwp, 6) if avg_mwp else None,
            'avg_gwp':            round(avg_gwp, 6) if avg_gwp else None,
            'avg_omwp':           round(avg_omwp, 6) if avg_omwp else None,
            'total_points':       total_pts,
            'events_played':      len(d['events']),
            'primary_archetype':  primary_arch,
            'performance_score':  round(perf, 6),
            'last_updated':       datetime.now(timezone.utc).isoformat()
        })
    return sorted(rows, key=lambda x: x['performance_score'], reverse=True)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f'Mize MTGO Import — {datetime.now(timezone.utc).isoformat()}')

    # Load archetype definitions for all formats
    print('Loading archetype definitions...')
    arch_defs_by_format = load_all_arch_defs()

    # Format patterns for event discovery
    FORMAT_PATTERNS = {
        'Modern':    ['modern-challenge', 'modern-showcase-challenge'],
        'Legacy':    ['legacy-challenge', 'legacy-showcase-challenge'],
        'Pauper':    ['pauper-challenge'],
        'Pioneer':   ['pioneer-challenge', 'pioneer-showcase-challenge'],
        'Vintage':   ['vintage-challenge'],
        'Premodern': ['premodern-challenge'],
    }

    # Discover events
    years = ['2023', '2024', '2025', '2026']
    all_format_events = []

    print('Discovering events from fbettega...')
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
    print(f'Total events found: {len(all_format_events)}')
    for fmt, count in sorted(Counter(e['format'] for e in all_format_events).items()):
        print(f'  {fmt}: {count}')

    # Filter to only new events
    existing_events = sb_get('mtgo_events', '?select=event_id')
    existing_ids    = set(r['event_id'] for r in existing_events)
    new_events      = [e for e in all_format_events if e['name'] not in existing_ids]
    print(f'New events to process: {len(new_events)}')

    # Process new events
    event_rows   = []
    result_rows  = []
    errors       = []
    skipped      = 0
    corrections  = []  # Track archetype corrections for notification

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

        event_rows.append({
            'event_id':   event['name'],
            'event_name': tournament.get('Name', event['name']),
            'event_date': event_date,
            'event_type': determine_event_type(event['name']),
            'format':     event['format'],
            'scraped_at': datetime.now(timezone.utc).isoformat()
        })

        for player in standings:
            player_name   = player.get('Player')
            deck_data     = decks_by_player.get(player_name, {})
            arch_defs     = arch_defs_by_format.get(event['format'], [])
            declared_arch = detect_archetype_from_deck(deck_data, arch_defs) if deck_data else 'Unknown'
            finish        = player.get('Rank')

            result_rows.append({
                'event_id':            event['name'],
                'player_name':         player_name,
                'archetype_raw':       declared_arch,
                'archetype_canonical': declared_arch,
                'finish_position':     finish,
                'points':              player.get('Points'),
                'match_win_pct':       norm_pct(player.get('OMWP')),
                'game_win_pct':        norm_pct(player.get('GWP')),
                'opp_match_win_pct':   norm_pct(player.get('OGWP')),
                'created_at':          datetime.now(timezone.utc).isoformat()
            })

        if (idx + 1) % 25 == 0:
            print(f'  Processed {idx+1}/{len(new_events)}... ({len(result_rows)} rows)')
        time.sleep(0.3)

    print(f'Events processed: {len(event_rows)} | Skipped: {skipped} | Errors: {len(errors)}')

    # Insert new events and results
    if event_rows:
        print(f'Inserting {len(event_rows)} events...')
        sb_insert('mtgo_events', event_rows)
    if result_rows:
        print(f'Inserting {len(result_rows)} results...')
        sb_insert('mtgo_results', result_rows, batch_size=300)

    # ── Validate mymtgo game log imports ──────────────────────────────────────
    print('\nValidating mymtgo game log archetypes...')

    # Fetch all mymtgo-imported games that haven't been validated yet
    mymtgo_games = sb_get('raw_game_log',
        '?select=id,pilot_name,deck_archetype,format,card1,card2,card3,card4,card5,card6,card7,external_id'
        '&not.external_id=is.null'
        '&archetype_validated=is.false'
    )

    # Fall back to fetching all mymtgo games if the column doesn't exist yet
    if not mymtgo_games:
        mymtgo_games = sb_get('raw_game_log',
            '?select=id,pilot_name,deck_archetype,format,card1,card2,card3,card4,card5,card6,card7,external_id'
            '&not.external_id=is.null'
        )

    print(f'Validating {len(mymtgo_games)} mymtgo games...')

    corrections = []
    for game in mymtgo_games:
        fmt           = game.get('format', 'Modern')
        declared_arch = game.get('deck_archetype', '')

        if fmt not in VALIDATABLE_FORMATS:
            continue

        arch_defs = arch_defs_by_format.get(fmt, [])
        if not arch_defs:
            continue

        # Get declared archetype definition
        declared_def = next((d for d in arch_defs if d['_name'] == declared_arch), None)
        if not declared_def:
            continue

        # Build hand card counts
        hand_cards = [game.get(f'card{i}') for i in range(1, 8) if game.get(f'card{i}')]
        if not hand_cards:
            continue

        mainboard = {card: hand_cards.count(card) for card in set(hand_cards)}

        # Check if declared archetype matches hand
        is_valid = test_conditions(declared_def.get('Conditions', []), mainboard, {})
        if is_valid:
            continue

        # Try to find better match
        corrected = detect_archetype_from_cards(hand_cards, arch_defs)
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

    # Apply corrections to raw_game_log
    if corrections:
        for fix in corrections:
            r = requests.patch(
                f'{SUPABASE_URL}/rest/v1/raw_game_log?id=eq.{fix["id"]}',
                headers={**headers, 'Prefer': 'return=minimal'},
                data=json.dumps({'deck_archetype': fix['corrected']})
            )
            if r.status_code not in [200, 204]:
                print(f'  Error updating {fix["external_id"]}: {r.status_code}')
            time.sleep(0.05)
        print(f'Applied {len(corrections)} corrections.')

    # ── Compute summaries ─────────────────────────────────────────────────────
    print('\nComputing summaries...')
    all_results      = sb_get('mtgo_results',
        '?select=event_id,player_name,archetype_canonical,finish_position,points,match_win_pct,game_win_pct,opp_match_win_pct'
    )
    all_events_db    = sb_get('mtgo_events', '?select=event_id,event_date,format')
    event_date_map   = {e['event_id']: e['event_date'] for e in all_events_db}
    event_format_map = {e['event_id']: e.get('format', 'Modern') for e in all_events_db}
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
            r['_format'] = event_format_map.get(r['event_id'], 'Modern')
    if not filtered:
            continue

        # Group by format and compute summaries separately to avoid
        # cross-format contamination of archetype names (e.g. Affinity
        # exists in both Modern and Vintage)
    formats_in_window = set(r['_format'] for r in filtered)
    for fmt in sorted(formats_in_window):
        fmt_results = [r for r in filtered if r['_format'] == fmt]
        label = ('all time' if not w['days'] else f'{w["days"]}d') + f' / {fmt}'
        print(f'Window {label}: {len(fmt_results)} results')
        arch_rows  = compute_archetype_summary(fmt_results, date_from, date_to)
        pilot_rows = compute_pilot_summary(fmt_results, date_from, date_to)
        for r in arch_rows:
            r['window_days'] = w['days'] if w['days'] else 0
        for r in pilot_rows:
            r['window_days'] = w['days'] if w['days'] else 0
        all_summaries_arch.extend(arch_rows)
        all_summaries_pilot.extend(pilot_rows)

    print(f'Total archetype summary rows: {len(all_summaries_arch)}')
    print(f'Total pilot summary rows: {len(all_summaries_pilot)}')

    print(f'Archetype summary rows: {len(all_summaries_arch)}')
    print(f'Pilot summary rows: {len(all_summaries_pilot)}')

    # Write summaries
    print('Clearing existing summaries...')
    sb_delete('mtgo_archetype_summary', '?id=neq.00000000-0000-0000-0000-000000000000')
    sb_delete('mtgo_pilot_summary',     '?id=neq.00000000-0000-0000-0000-000000000000')
    time.sleep(1)

    print(f'Writing {len(all_summaries_arch)} archetype summary rows...')
    sb_insert('mtgo_archetype_summary', all_summaries_arch, batch_size=300)
    print(f'Writing {len(all_summaries_pilot)} pilot summary rows...')
    sb_insert('mtgo_pilot_summary', all_summaries_pilot, batch_size=300)

    # ── Print correction summary for GitHub Actions log ───────────────────────
    print('\n' + '='*60)
    print('IMPORT COMPLETE')
    print('='*60)
    print(f'New events imported:    {len(event_rows)}')
    print(f'New results imported:   {len(result_rows)}')
    print(f'Archetype corrections:  {len(corrections)}')
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

    # Exit with error code if there were corrections so GitHub Actions flags the run
    if corrections:
        print(f'\n⚠ {len(corrections)} archetype correction(s) were applied.')
        print('Check the Actions log above for details.')

if __name__ == '__main__':
    main()
