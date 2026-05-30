"""
rescan_unknowns.py — Mize Unknown Decks Rescan
Runs similarity matching on unreviewed mtgo_unknown_decks entries,
auto-assigning those that match a reference fingerprint above threshold.

Usage:
  SUPABASE_URL=... SUPABASE_KEY=... python3 rescan_unknowns.py
  SUPABASE_URL=... SUPABASE_KEY=... python3 rescan_unknowns.py --format Modern
  SUPABASE_URL=... SUPABASE_KEY=... python3 rescan_unknowns.py --format Legacy
"""

import os, sys, json, time, math
from datetime import datetime, timezone
import urllib.request, urllib.parse, urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL        = os.environ.get('SUPABASE_URL', 'https://vkgtqunhsalquihlqkxp.supabase.co')
SUPABASE_KEY        = os.environ.get('SUPABASE_KEY', '')
SIMILARITY_THRESHOLD = 0.60
MIN_REFERENCE_DECKS  = 5
BATCH_SIZE = 500

# ── Supabase helpers ──────────────────────────────────────────────────────────
def sb_headers():
    return {
        'apikey':        SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type':  'application/json',
        'Prefer':        'return=minimal'
    }

SB_TIMEOUT = 30  # seconds

def sb_get(table, params=''):
    """Paginated GET — fetches all rows with timeout and retries."""
    all_rows = []
    offset   = 0
    while True:
        sep = '&' if '?' in params else '?'
        url = f'{SUPABASE_URL}/rest/v1/{table}{params}{sep}limit=1000&offset={offset}'
        req = urllib.request.Request(url, headers={**sb_headers(), 'Prefer': 'count=none'})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=SB_TIMEOUT) as r:
                    batch = json.loads(r.read().decode())
                    if not batch:
                        return all_rows
                    all_rows.extend(batch)
                    if len(batch) < 1000:
                        return all_rows
                    offset += 1000
                    break
            except urllib.error.HTTPError as e:
                print(f'  GET error {table}: {e.code} {e.read().decode()[:200]}')
                return all_rows
            except Exception as e:
                print(f'  GET timeout/error {table} offset={offset} attempt {attempt+1}: {e}')
                if attempt < 2:
                    time.sleep(5)
                else:
                    print(f'  Giving up on {table}')
                    return all_rows
        time.sleep(0.05)
    return all_rows

def sb_patch(table, params, body):
    """PATCH rows matching params."""
    url  = f'{SUPABASE_URL}/rest/v1/{table}{params}'
    data = json.dumps(body).encode()
    req  = urllib.request.Request(url, data=data, headers=sb_headers(), method='PATCH')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=SB_TIMEOUT) as r:
                return True
        except urllib.error.HTTPError as e:
            print(f'  PATCH error: {e.code} {e.read().decode()[:200]}')
            return False
        except Exception as e:
            print(f'  PATCH timeout attempt {attempt+1}: {e}')
            if attempt < 2:
                time.sleep(5)
    return False

def sb_insert(table, rows, batch_size=BATCH_SIZE):
    if not rows:
        return
    url     = f'{SUPABASE_URL}/rest/v1/{table}'
    headers = sb_headers()
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i+batch_size]
        data  = json.dumps(batch).encode()
        req   = urllib.request.Request(url, data=data, headers=headers, method='POST')
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=SB_TIMEOUT) as r:
                    pass
                break
            except urllib.error.HTTPError as e:
                print(f'  INSERT error: {e.code} {e.read().decode()[:200]}')
                break
            except Exception as e:
                print(f'  INSERT timeout attempt {attempt+1}: {e}')
                if attempt < 2:
                    time.sleep(5)
        time.sleep(0.1)

# ── Key card detection ────────────────────────────────────────────────────────
MIZE_REPO              = 'phlsphr42/mize'
CUSTOM_ARCHETYPES_PATH = 'scripts/custom_archetypes.json'
GITHUB_API             = 'https://api.github.com'
GITHUB_TOKEN           = os.environ.get('GITHUB_TOKEN', '')

def load_key_cards():
    """Load key_cards from custom_archetypes.json."""
    url = f'{GITHUB_API}/repos/{MIZE_REPO}/contents/{CUSTOM_ARCHETYPES_PATH}'
    hdrs = {'Accept': 'application/json', 'User-Agent': 'Mize-Rescan'}
    if GITHUB_TOKEN:
        hdrs['Authorization'] = f'token {GITHUB_TOKEN}'
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            file_info = json.loads(r.read().decode())
        req2 = urllib.request.Request(file_info['download_url'], headers={'User-Agent': 'Mize-Rescan'})
        with urllib.request.urlopen(req2, timeout=30) as r2:
            data = json.loads(r2.read().decode())
        key_cards = data.get('key_cards', {})
        total = sum(len(v) for v in key_cards.values())
        print(f'  Loaded key_cards: {total} archetypes across formats: {list(key_cards.keys())}')
        return key_cards
    except Exception as e:
        print(f'  Error loading key_cards: {e}')
        return {}


def detect_archetype(deck_cards_set, key_cards_for_format):
    """Return archetype if all 3 key cards present in deck, else None."""
    for arch, keys in key_cards_for_format.items():
        if all(k in deck_cards_set for k in keys):
            return arch
    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def rescan_format(fmt, key_cards_for_format):
    print(f'\nRescanning {fmt} unknown decks...')

    decks = sb_get('mtgo_unknown_decks',
        f'?format=eq.{fmt}&reviewed=eq.false'
        f'&select=id,event_id,player_name,format,finish,mainboard,sideboard'
    )
    print(f'  {len(decks)} unreviewed decks to scan')
    if not decks:
        return 0, 0

    matched = 0
    now     = datetime.now(timezone.utc).isoformat()

    for i, deck in enumerate(decks):
        mb = deck['mainboard']
        if isinstance(mb, str):
            mb = json.loads(mb)
        deck_set = set(mb.keys())
        arch = detect_archetype(deck_set, key_cards_for_format)

        if arch:
            # Mark as reviewed
            sb_patch('mtgo_unknown_decks',
                f'?id=eq.{deck["id"]}',
                {
                    'assigned_name':      arch,
                    'assigned_supertype': None,
                    'reviewed':           True,
                    'reviewed_at':        now,
                    'reviewed_by':        'rescan_script'
                }
            )

            # Update mtgo_results
            sb_patch('mtgo_results',
                f'?event_id=eq.{urllib.parse.quote(deck["event_id"])}'
                f'&player_name=eq.{urllib.parse.quote(deck["player_name"])}',
                {'archetype_canonical': arch, 'archetype_raw': arch}
            )

            # Update mtgo_matches (player1 and player2)
            sb_patch('mtgo_matches',
                f'?event_id=eq.{urllib.parse.quote(deck["event_id"])}'
                f'&player1=eq.{urllib.parse.quote(deck["player_name"])}',
                {'player1_arch': arch}
            )
            sb_patch('mtgo_matches',
                f'?event_id=eq.{urllib.parse.quote(deck["event_id"])}'
                f'&player2=eq.{urllib.parse.quote(deck["player_name"])}',
                {'player2_arch': arch}
            )

            matched += 1

        if (i + 1) % 500 == 0:
            print(f'  Progress: {i+1}/{len(decks)} scanned, {matched} matched')
            # Insert reference rows in batches to avoid memory buildup
            if new_refs:
                sb_insert('reference_decklists', new_refs)
                new_refs = []

    # Insert remaining reference rows
    if new_refs:
        sb_insert('reference_decklists', new_refs)

    print(f'  Done: {matched}/{len(decks)} matched, {len(decks)-matched} remain unknown')
    return matched, len(decks) - matched


def main():
    if not SUPABASE_KEY:
        print('ERROR: SUPABASE_KEY not set')
        sys.exit(1)

    # ── Connectivity test ─────────────────────────────────────────────────────
    print('Testing Supabase connectivity...', flush=True)
    try:
        test_url = f'{SUPABASE_URL}/rest/v1/pilots?select=pilot_name&limit=1'
        req = urllib.request.Request(test_url, headers=sb_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
            print(f'  Supabase OK — got {len(data)} row(s)', flush=True)
    except Exception as e:
        print(f'  Supabase connectivity FAILED: {e}', flush=True)
        print('  Check SUPABASE_URL and SUPABASE_KEY secrets', flush=True)
        sys.exit(1)

    # Parse --format argument
    fmt_arg = None
    for i, arg in enumerate(sys.argv):
        if arg == '--format' and i + 1 < len(sys.argv):
            fmt_arg = sys.argv[i + 1]

    formats = [fmt_arg] if fmt_arg else ['Modern', 'Legacy', 'Pioneer', 'Pauper', 'Vintage']

    print(f'Mize Unknown Decks Rescan — {datetime.now(timezone.utc).isoformat()}')
    print(f'Formats: {formats}')
    print(f'Threshold: {SIMILARITY_THRESHOLD}')

    total_matched  = 0
    total_unmatched = 0

    all_key_cards = load_key_cards()
    if not all_key_cards:
        print('ERROR: Could not load key_cards — aborting')
        sys.exit(1)

    for fmt in formats:
        key_cards_fmt = all_key_cards.get(fmt, {})
        if not key_cards_fmt:
            print(f'  Skipping {fmt} — no key_cards defined')
            continue
        matched, unmatched = rescan_format(fmt, key_cards_fmt)
        total_matched   += matched
        total_unmatched += unmatched

    print(f'\n{"="*60}')
    print(f'RESCAN COMPLETE')
    print(f'{"="*60}')
    print(f'Total matched:   {total_matched}')
    print(f'Total remaining: {total_unmatched}')
    print(f'Completed: {datetime.now(timezone.utc).isoformat()}')


if __name__ == '__main__':
    main()
