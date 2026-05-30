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

# ── Reference decklist similarity matching ────────────────────────────────────
SIMILARITY_THRESHOLD     = 0.60  # minimum Jaccard similarity for full deck match
SIMILARITY_THRESHOLD_HAND = 0.35  # lower threshold for 7-card opening hand
MIN_REFERENCE_DECKS      = 5     # auto-add matched decks until this many refs exist
MAX_DECK_CARDS           = 80    # ignore lands-only noise above this count

def load_reference_fingerprints(fmt):
    """Load reference decklists from Supabase and build per-archetype fingerprints.

    Returns:
        fingerprints: dict of archetype_name -> {
            'cards': {card_name: avg_quantity},
            'card_set': set of card names in mainboard,
            'deck_count': int
        }
        deck_counts: dict of archetype_name -> number of reference decks stored
    """
    # Fetch all rows with pagination to handle large tables
    rows = []
    offset = 0
    while True:
        batch = sb_get('reference_decklists',
            f'?format=eq.{fmt}&main_side=eq.Main&select=archetype_name,card_name,quantity'
            f'&limit=1000&offset={offset}'
        )
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    if not rows:
        return {}, {}

    # Group by archetype — each archetype may have multiple reference decks.
    # deck_index distinguishes them; for fingerprinting we average across all decks.
    from collections import defaultdict
    arch_cards = defaultdict(lambda: defaultdict(list))  # arch -> card -> [qty per deck]
    arch_deck_sets = defaultdict(set)  # arch -> set of deck_index values (proxy for deck count)

    for row in rows:
        arch  = row['archetype_name']
        card  = row['card_name']
        qty   = row.get('quantity', 1)
        arch_cards[arch][card].append(qty)

    fingerprints = {}
    for arch, cards in arch_cards.items():
        # Average quantity per card across all reference decks
        avg = {card: sum(qtys) / len(qtys) for card, qtys in cards.items()}
        fingerprints[arch] = {
            'cards':      avg,
            'card_set':   set(avg.keys()),
            'deck_count': max(len(v) for v in cards.values()),  # max occurrences ≈ deck count
        }

    deck_counts = {arch: fp['deck_count'] for arch, fp in fingerprints.items()}
    return fingerprints, deck_counts


def jaccard_similarity(deck_cards: set, ref_cards: set) -> float:
    """Jaccard similarity between two sets of card names."""
    if not deck_cards or not ref_cards:
        return 0.0
    intersection = len(deck_cards & ref_cards)
    union        = len(deck_cards | ref_cards)
    return intersection / union if union > 0 else 0.0


def weighted_similarity(deck: dict, fingerprint: dict) -> float:
    """Weighted similarity: cards with higher average quantity in reference count more.

    Score = sum of min(deck_qty, ref_avg_qty) for shared cards
            / max(sum of deck quantities, sum of ref avg quantities)
    This rewards matching high-copy cards (4-ofs) more than 1-ofs.
    """
    ref   = fingerprint['cards']
    score = sum(min(deck.get(c, 0), ref[c]) for c in ref if c in deck)
    total = max(sum(deck.values()), sum(ref.values()))
    return score / total if total > 0 else 0.0


def detect_archetype_from_deck(deck_data, fingerprints, threshold=SIMILARITY_THRESHOLD):
    """Compare a full decklist against reference fingerprints.

    Returns (archetype_name, score) or (None, 0.0) if no match above threshold.
    """
    mainboard = {c['CardName']: c.get('Count', 1) for c in deck_data.get('Mainboard', [])}
    if not mainboard:
        return None, 0.0

    deck_set = set(mainboard.keys())
    best_arch, best_score = None, 0.0

    for arch, fp in fingerprints.items():
        # Use weighted similarity as primary score
        score = weighted_similarity(mainboard, fp)
        if score > best_score:
            best_score = score
            best_arch  = arch

    if best_score >= threshold:
        return best_arch, best_score
    return None, best_score


def detect_archetype_from_cards(cards_list, fingerprints, threshold=SIMILARITY_THRESHOLD_HAND):
    """Compare a 7-card opening hand against reference fingerprints.

    Uses Jaccard similarity (no quantities) since hand is a small sample.
    Returns archetype_name or None.
    """
    hand_set = {c for c in cards_list if c}
    if not hand_set:
        return None

    best_arch, best_score = None, 0.0
    for arch, fp in fingerprints.items():
        score = jaccard_similarity(hand_set, fp['card_set'])
        if score > best_score:
            best_score = score
            best_arch  = arch

    return best_arch if best_score >= threshold else None


def add_to_reference_decklists(deck_data, archetype, fmt, deck_counts):
    """Add a matched deck to reference_decklists if archetype has < MIN_REFERENCE_DECKS."""
    current = deck_counts.get(archetype, 0)
    if current >= MIN_REFERENCE_DECKS:
        return False

    next_index = current + 1
    rows = []
    for c in deck_data.get('Mainboard', []):
        rows.append({
            'archetype_name': archetype,
            'format':         fmt,
            'card_name':      c['CardName'],
            'quantity':       c.get('Count', 1),
            'main_side':      'Main',
            'source':         'mtgo_import',
            'deck_index':     next_index,
        })
    for c in deck_data.get('Sideboard', []):
        rows.append({
            'archetype_name': archetype,
            'format':         fmt,
            'card_name':      c['CardName'],
            'quantity':       c.get('Count', 1),
            'main_side':      'Side',
            'source':         'mtgo_import',
            'deck_index':     next_index,
        })
    if rows:
        sb_insert('reference_decklists', rows, batch_size=100)
        deck_counts[archetype] = next_index  # update in-memory count
        return True
    return False

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
def compute_archetype_summary(results, date_from, date_to, fmt='Modern'):
    by_arch = defaultdict(lambda: {
        'appearances': [], 'top8': 0, 'events': set(),
        'points': [], 'mwp': [], 'gwp': [], 'omwp': [],
        'format': fmt
    })
    total = len(results)
    for r in results:
        arch = r.get('archetype_canonical') or 'Unknown'
        pos  = r.get('finish_position')
        by_arch[arch]['appearances'].append(pos)
        by_arch[arch]['events'].add(r['event_id'])
        by_arch[arch]['format'] = fmt  # always use the explicit format, never infer
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
        raw_perf = (
            (((avg_mwp * top32) + (0.5 * 20)) / (top32 + 20))
            * math.log(max(top32, 1) + 1)
            * (1 + top8_rate)
        ) if avg_mwp is not None else 0
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
            'raw_performance_score': round(raw_perf, 6),
            'last_updated':          datetime.now(timezone.utc).isoformat()
        })
    return sorted(rows, key=lambda x: x['meta_adjusted_score'], reverse=True)

def compute_pilot_summary(results, date_from, date_to, fmt='Modern'):
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

    # Load reference fingerprints for all formats
    print('Loading reference decklist fingerprints...')
    fingerprints_by_format = {}
    deck_counts_by_format  = {}
    for fmt in VALIDATABLE_FORMATS:
        fp, dc = load_reference_fingerprints(fmt)
        fingerprints_by_format[fmt] = fp
        deck_counts_by_format[fmt]  = dc
        print(f'  {fmt}: {len(fp)} archetype fingerprints loaded')

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

    print(f'Discovering events from fbettega (scanning years: {years})...')
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

    print(f'Total events found: {len(all_format_events)}')
    for fmt, count in sorted(Counter(e['format'] for e in all_format_events).items()):
        print(f'  {fmt}: {count}')

    # Filter to only new events
    existing_events = sb_get('mtgo_events', '?select=event_id')
    existing_ids    = set(r['event_id'] for r in existing_events)
    new_events      = [e for e in all_format_events if e['name'] not in existing_ids]
    print(f'New events to process: {len(new_events)}')
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
            fmt         = event['format']
            fingerprints = fingerprints_by_format.get(fmt, {})
            deck_counts  = deck_counts_by_format.get(fmt, {})
            if deck_data:
                detected, score = detect_archetype_from_deck(deck_data, fingerprints)
                # Auto-add to reference if match found and archetype needs more examples
                if detected:
                    add_to_reference_decklists(deck_data, detected, fmt, deck_counts)
            else:
                detected, score = None, 0.0
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
            print(f'  Processed {idx+1}/{len(new_events)}... ({len(result_rows)} results, {len(match_rows)} matches)')
        time.sleep(0.3)

    print(f'Events processed: {len(event_rows)} | Skipped: {skipped} | Errors: {len(errors)}')
    print(f'Result rows: {len(result_rows)} | Match rows: {len(match_rows)}')

    # Insert new events, results, and matches
    if event_rows:
        print(f'Inserting {len(event_rows)} events...')
        sb_insert('mtgo_events', event_rows)
    if result_rows:
        print(f'Inserting {len(result_rows)} results...')
        sb_insert('mtgo_results', result_rows, batch_size=300)
    if match_rows:
        print(f'Inserting {len(match_rows)} match rows...')
        sb_insert('mtgo_matches', match_rows, batch_size=300)
    if unknown_deck_rows:
        print(f'Inserting {len(unknown_deck_rows)} unknown decks for review...')
        sb_insert('mtgo_unknown_decks', unknown_deck_rows, batch_size=100)

    # ── Validate mymtgo game log imports ──────────────────────────────────────
    print('\nValidating mymtgo game log archetypes...')
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
        fingerprints = fingerprints_by_format.get(fmt, {})
        if not fingerprints:
            continue
        # For mymtgo validation, check if declared archetype exists in fingerprints
        if declared_arch not in fingerprints:
            continue
        hand_cards = [game.get(f'card{i}') for i in range(1, 8) if game.get(f'card{i}')]
        if not hand_cards:
            continue
        corrected = detect_archetype_from_cards(hand_cards, fingerprints)
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
    print('Clearing existing summaries...')
    sb_delete('mtgo_archetype_summary', '?id=neq.00000000-0000-0000-0000-000000000000')
    sb_delete('mtgo_pilot_summary',     '?id=neq.00000000-0000-0000-0000-000000000000')
    time.sleep(1)

    print(f'Writing {len(all_summaries_arch)} archetype summary rows...')
    sb_insert('mtgo_archetype_summary', all_summaries_arch, batch_size=300)
    print(f'Writing {len(all_summaries_pilot)} pilot summary rows...')
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
