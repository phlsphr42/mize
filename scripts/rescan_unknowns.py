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
from collections import defaultdict
import urllib.request, urllib.parse, urllib.error

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL        = os.environ.get('SUPABASE_URL', 'https://vkgtqunhsalquihlqkxp.supabase.co')
SUPABASE_KEY        = os.environ.get('SUPABASE_KEY', '')
SIMILARITY_THRESHOLD = 0.60
MIN_REFERENCE_DECKS  = 5
BATCH_SIZE           = 500

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

# ── Similarity matching ───────────────────────────────────────────────────────
def load_identifiers(fmt):
    """Load exact 3-card archetype identifiers for the given format."""
    rows = sb_get('archetype_identifiers',
        f'?format=eq.{urllib.parse.quote(fmt)}&select=archetype_name,card1,card2,card3'
    )
    print(f'  {len(rows)} archetype identifiers loaded for {fmt}')
    return rows


def detect_by_identifier(deck_mb, identifiers):
    """Return archetype_name if all 3 identifier cards are present in mainboard, else None."""
    mb_set = set(deck_mb.keys())
    for ident in identifiers:
        if ident['card1'] in mb_set and ident['card2'] in mb_set and ident['card3'] in mb_set:
            return ident['archetype_name']
    return None


def load_fingerprints(fmt):
    """Load reference decklists and build per-archetype fingerprints."""
    print(f'  Loading reference decklists for {fmt}...')
    rows = sb_get('reference_decklists',
        f'?format=eq.{fmt}&main_side=eq.Main&select=archetype_name,card_name,quantity'
    )
    if not rows:
        print(f'  No reference decklists found for {fmt}.')
        return {}, {}

    by_arch = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_arch[r['archetype_name']][r['card_name']].append(r['quantity'])

    fingerprints = {}
    deck_counts  = {}
    for arch, cards in by_arch.items():
        avg = {card: sum(qtys)/len(qtys) for card, qtys in cards.items()}
        fingerprints[arch] = avg
        deck_counts[arch]  = max(len(v) for v in cards.values())

    print(f'  {len(fingerprints)} archetype fingerprints loaded')
    return fingerprints, deck_counts

def weighted_similarity(deck, fingerprint):
    score     = sum(min(deck.get(c, 0), q) for c, q in fingerprint.items() if c in deck)
    deck_total = sum(deck.values())
    ref_total  = sum(fingerprint.values())
    total      = max(deck_total, ref_total)
    return score / total if total > 0 else 0.0

def best_match(deck, fingerprints):
    best_arch, best_score = None, 0.0
    for arch, fp in fingerprints.items():
        score = weighted_similarity(deck, fp)
        if score > best_score:
            best_score = score
            best_arch  = arch
    if best_score >= SIMILARITY_THRESHOLD:
        return best_arch, best_score
    return None, best_score

# ── Main ──────────────────────────────────────────────────────────────────────
def rescan_format(fmt, fingerprints, deck_counts, identifiers):
    print(f'\nRescanning {fmt} unknown decks...')

    # Fetch all unreviewed unknown decks for this format
    decks = sb_get('mtgo_unknown_decks',
        f'?format=eq.{fmt}&reviewed=eq.false'
        f'&select=id,event_id,player_name,format,finish,mainboard,sideboard'
    )
    print(f'  {len(decks)} unreviewed decks to scan')
    if not decks:
        return 0, 0

    matched   = 0
    new_refs  = []
    now       = datetime.now(timezone.utc).isoformat()

    for i, deck in enumerate(decks):
        mb = deck['mainboard']
        if isinstance(mb, str):
            mb = json.loads(mb)

        arch, score = None, 0.0
        # 1. Try exact identifier match (fast, precise)
        id_match = detect_by_identifier(mb, identifiers)
        if id_match:
            arch, score = id_match, 1.0
        else:
            # 2. Fall back to weighted similarity
            arch, score = best_match(mb, fingerprints)

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

            # Add to reference decklists if needed
            current = deck_counts.get(arch, 0)
            if current < MIN_REFERENCE_DECKS:
                idx = current + 1
                sb = deck.get('sideboard') or {}
                if isinstance(sb, str):
                    sb = json.loads(sb)
                for card, qty in mb.items():
                    if card:
                        new_refs.append({
                            'archetype_name': arch, 'format': fmt,
                            'card_name': card, 'quantity': qty,
                            'main_side': 'Main', 'source': 'rescan_script',
                            'deck_index': idx
                        })
                for card, qty in sb.items():
                    if card:
                        new_refs.append({
                            'archetype_name': arch, 'format': fmt,
                            'card_name': card, 'quantity': qty,
                            'main_side': 'Side', 'source': 'rescan_script',
                            'deck_index': idx
                        })
                deck_counts[arch] = idx

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

    for fmt in formats:
        fingerprints, deck_counts = load_fingerprints(fmt)
        identifiers = load_identifiers(fmt)
        if not fingerprints and not identifiers:
            print(f'  Skipping {fmt} — no fingerprints or identifiers')
            continue
        matched, unmatched = rescan_format(fmt, fingerprints, deck_counts, identifiers)
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
