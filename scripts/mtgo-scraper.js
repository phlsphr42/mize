const fetch = (...args) => import('node-fetch').then(({default: f}) => f(...args));

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;
const FORMAT = 'modern';

const headers = {
  'apikey': SUPABASE_KEY,
  'Authorization': `Bearer ${SUPABASE_KEY}`,
  'Content-Type': 'application/json',
  'Prefer': 'resolution=merge-duplicates,return=minimal'
};

// ── CANONICAL ARCHETYPE MATCHING ──────────────────────────────────

const ARCHETYPE_MAP = {
  'domain zoo': 'Domain Zoo',
  'boros energy': 'Boros Energy',
  'amulet titan': 'Amulet Titan',
  'jeskai control': 'Jeskai Control',
  'lantern control': 'Lantern',
  'lantern': 'Lantern',
  'eldrazi ramp': 'Eldrazi Ramp',
  'eldrazi tron': 'Eldrazi Tron',
  'eldrazi aggro': 'Eldrazi Aggro',
  'ruby storm': 'Ruby Storm',
  'esper blink': 'Esper Blink',
  'orzhov blink': 'Orzhov Blink',
  'yawgmoth': 'Yawgmoth',
  'grixis reanimator': 'Grixis Reanimator',
  'dimir midrange': 'Dimir Midrange',
  'living end': 'Living End',
  'belcher': 'Azorius Belcher',
  'goryo\'s vengeance': 'Goryo\'s Vengeance',
  'burn': 'Burn',
  'affinity': 'Affinity',
  'hammer time': 'Hammer Time',
  'hammer': 'Hammer Time',
  'infect': 'Infect',
  'merfolk': 'Merfolk',
  'mill': 'Mill',
  'tron': 'Tron',
  'eldrazi tron': 'Eldrazi Tron',
  'blue tron': 'Blue Tron',
  'green tron': 'Green Tron',
  'hardened scales': 'Hardened Scales',
  'jund': 'Jund Midrange',
  'jund midrange': 'Jund Midrange',
  'grixis shadow': 'Grixis Shadow',
  'grixis control': 'Grixis Control',
  'dimir control': 'Dimir Control',
  'azorius control': 'Azorius Control',
  'bogles': 'Bogles',
  'rack': 'Rack',
  'scapeshift': 'Scapeshift',
  'crashing footfalls': 'Crashing Footfalls',
  'rhinos': 'Crashing Footfalls',
  'temur rhinos': 'Crashing Footfalls',
  'birthing ritual': 'Birthing Ritual',
  'broodscale': 'Broodscale Combo',
  'broodscale combo': 'Broodscale Combo',
  'indomitable creativity': 'Indomitable Creativity',
  'creativity': 'Indomitable Creativity',
  'goblins': 'Goblins',
  'samwise combo': 'Samwise Combo',
  'grinding station': 'Grinding Station',
  'izzet cutter': 'Izzet Cutter',
  'dredge': 'Dredge',
  'coffers': 'Coffers',
  'death\'s shadow': 'Death\'s Shadow',
  'prowess': 'Prowess',
  'through the breach': 'Through the Breach',
  'omni woodland': 'Omni Woodland',
  'red belcher': 'Red Belcher',
  'domain': 'Domain Zoo',
};

function canonicalise(raw) {
  if (!raw) return 'Unknown';
  const key = raw.toLowerCase().trim()
    .replace(/[^a-z0-9\s']/g, '')
    .replace(/\s+/g, ' ');
  if (ARCHETYPE_MAP[key]) return ARCHETYPE_MAP[key];
  // Try partial matches
  for (const [k, v] of Object.entries(ARCHETYPE_MAP)) {
    if (key.includes(k) || k.includes(key)) return v;
  }
  // Title case the raw name as fallback
  return raw.trim().replace(/\w\S*/g, w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
}

// ── SUPABASE HELPERS ───────────────────────────────────────────────

async function sbGet(table, params = '') {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${table}${params}`, { headers });
  return res.json();
}

async function sbUpsert(table, rows) {
  if (!rows.length) return 0;
  const batchSize = 200;
  let total = 0;
  for (let i = 0; i < rows.length; i += batchSize) {
    const batch = rows.slice(i, i + batchSize);
    const res = await fetch(`${SUPABASE_URL}/rest/v1/${table}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(batch)
    });
    if (res.status >= 200 && res.status < 300) total += batch.length;
    else console.error(`Upsert error on ${table} batch ${i}:`, res.status, await res.text());
    await new Promise(r => setTimeout(r, 100));
  }
  return total;
}

async function sbDelete(table, params) {
  await fetch(`${SUPABASE_URL}/rest/v1/${table}${params}`, {
    method: 'DELETE',
    headers
  });
}

// ── MTGO DATA FETCHING ─────────────────────────────────────────────

async function fetchEventList() {
  console.log('Fetching MTGO event list...');
  try {
    const res = await fetch(
      'https://www.mtgo.com/api/decklists/events?format=modern&limit=50',
      { headers: { 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0' } }
    );
    if (!res.ok) {
      console.log('Primary endpoint failed, trying alternate...');
      // Try alternate endpoint
      const res2 = await fetch(
        'https://www.mtgo.com/en/mtgo/api/decklists?format=modern',
        { headers: { 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0' } }
      );
      if (!res2.ok) throw new Error(`HTTP ${res2.status}`);
      return res2.json();
    }
    return res.json();
  } catch(e) {
    console.error('Failed to fetch event list:', e.message);
    return null;
  }
}

async function fetchEventData(eventSlug) {
  try {
    const url = `https://www.mtgo.com/api/decklists/${eventSlug}`;
    const res = await fetch(url, {
      headers: { 'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0' }
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  } catch(e) {
    console.error(`Failed to fetch event ${eventSlug}:`, e.message);
    return null;
  }
}

// ── COMPUTATION ────────────────────────────────────────────────────

function computeArchetypeSummary(results, dateFrom, dateTo) {
  const byArch = {};
  const totalTop32 = results.length;

  for (const r of results) {
    const arch = r.archetype_canonical;
    if (!byArch[arch]) {
      byArch[arch] = {
        appearances: [], top8: 0, events: new Set(),
        points: [], mwp: [], gwp: [], omwp: []
      };
    }
    byArch[arch].appearances.push(r.finish_position);
    byArch[arch].events.add(r.event_id);
    if (r.finish_position <= 8) byArch[arch].top8++;
    if (r.points != null) byArch[arch].points.push(r.points);
    if (r.match_win_pct != null) byArch[arch].mwp.push(r.match_win_pct);
    if (r.game_win_pct != null) byArch[arch].gwp.push(r.game_win_pct);
    if (r.opp_match_win_pct != null) byArch[arch].omwp.push(r.opp_match_win_pct);
  }

  const avg = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;

  const summaries = [];
  for (const [arch, data] of Object.entries(byArch)) {
    const top32 = data.appearances.length;
    const top32Share = totalTop32 > 0 ? top32 / totalTop32 : 0;
    const top8Rate = top32 > 0 ? data.top8 / top32 : 0;
    const avgFinish = avg(data.appearances);
    const avgPoints = avg(data.points);
    const avgMwp = avg(data.mwp);
    const avgGwp = avg(data.gwp);
    const avgOmwp = avg(data.omwp);

    // Normalise finish position (1st = 1.0, 32nd = 0.0)
    const normFinish = avgFinish != null ? Math.max(0, (32 - avgFinish) / 31) : 0;

    // Performance score (fixed weights)
    const perfScore =
      (avgPoints != null ? (avgPoints / 18) * 0.40 : 0) +  // 18 = max points (6 wins)
      ((avgMwp || 0) * 0.25) +
      ((avgGwp || 0) * 0.15) +
      ((avgOmwp || 0) * 0.20);

    // Meta-adjusted score = performance per unit of meta share
    const metaAdjusted = top32Share > 0 ? perfScore / top32Share : 0;

    summaries.push({
      archetype_name: arch,
      date_from: dateFrom,
      date_to: dateTo,
      event_count: data.events.size,
      top32_appearances: top32,
      top8_appearances: data.top8,
      top32_share: Math.round(top32Share * 10000) / 10000,
      top8_rate: Math.round(top8Rate * 10000) / 10000,
      avg_finish: avgFinish != null ? Math.round(avgFinish * 100) / 100 : null,
      avg_points: avgPoints != null ? Math.round(avgPoints * 100) / 100 : null,
      avg_mwp: avgMwp != null ? Math.round(avgMwp * 10000) / 10000 : null,
      avg_gwp: avgGwp != null ? Math.round(avgGwp * 10000) / 10000 : null,
      avg_omwp: avgOmwp != null ? Math.round(avgOmwp * 10000) / 10000 : null,
      performance_score: Math.round(perfScore * 10000) / 10000,
      meta_adjusted_score: Math.round(metaAdjusted * 10000) / 10000,
      last_updated: new Date().toISOString()
    });
  }

  return summaries.sort((a, b) => b.meta_adjusted_score - a.meta_adjusted_score);
}

function computePilotSummary(results, dateFrom, dateTo) {
  const byPilot = {};

  for (const r of results) {
    const p = r.player_name;
    if (!p) continue;
    if (!byPilot[p]) {
      byPilot[p] = {
        appearances: [], top8: 0, events: new Set(),
        points: [], mwp: [], gwp: [], omwp: [],
        archetypes: {}
      };
    }
    byPilot[p].appearances.push(r.finish_position);
    byPilot[p].events.add(r.event_id);
    if (r.finish_position <= 8) byPilot[p].top8++;
    if (r.points != null) byPilot[p].points.push(r.points);
    if (r.match_win_pct != null) byPilot[p].mwp.push(r.match_win_pct);
    if (r.game_win_pct != null) byPilot[p].gwp.push(r.game_win_pct);
    if (r.opp_match_win_pct != null) byPilot[p].omwp.push(r.opp_match_win_pct);
    const arch = r.archetype_canonical;
    byPilot[p].archetypes[arch] = (byPilot[p].archetypes[arch] || 0) + 1;
  }

  const avg = arr => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;

  const summaries = [];
  for (const [player, data] of Object.entries(byPilot)) {
    const avgPoints = avg(data.points);
    const avgMwp = avg(data.mwp);
    const avgGwp = avg(data.gwp);
    const avgOmwp = avg(data.omwp);
    const primaryArch = Object.entries(data.archetypes)
      .sort((a, b) => b[1] - a[1])[0]?.[0] || 'Unknown';
    const totalPoints = data.points.reduce((a, b) => a + b, 0);

    const perfScore =
      (avgPoints != null ? (avgPoints / 18) * 0.40 : 0) +
      ((avgMwp || 0) * 0.25) +
      ((avgGwp || 0) * 0.15) +
      ((avgOmwp || 0) * 0.20);

    summaries.push({
      player_name: player,
      date_from: dateFrom,
      date_to: dateTo,
      top32_appearances: data.appearances.length,
      top8_appearances: data.top8,
      best_finish: Math.min(...data.appearances),
      avg_finish: avg(data.appearances) != null ? Math.round(avg(data.appearances) * 100) / 100 : null,
      avg_points: avgPoints != null ? Math.round(avgPoints * 100) / 100 : null,
      avg_mwp: avgMwp != null ? Math.round(avgMwp * 10000) / 10000 : null,
      avg_gwp: avgGwp != null ? Math.round(avgGwp * 10000) / 10000 : null,
      avg_omwp: avgOmwp != null ? Math.round(avgOmwp * 10000) / 10000 : null,
      total_points: totalPoints,
      events_played: data.events.size,
      primary_archetype: primaryArch,
      performance_score: Math.round(perfScore * 10000) / 10000,
      last_updated: new Date().toISOString()
    });
  }

  return summaries.sort((a, b) => b.performance_score - a.performance_score);
}

// ── MAIN ───────────────────────────────────────────────────────────

async function main() {
  console.log('MTGO scraper starting...', new Date().toISOString());

  // Fetch existing event IDs to avoid re-processing
  const existingEvents = await sbGet('mtgo_events', '?select=event_id');
  const existingIds = new Set((existingEvents || []).map(e => e.event_id));
  console.log(`Existing events in DB: ${existingIds.size}`);

  // Fetch event list from MTGO
  const eventList = await fetchEventList();
  if (!eventList) {
    console.error('Could not fetch event list. Exiting.');
    process.exit(1);
  }

  // Handle different possible response shapes
  const events = eventList.events || eventList.data || eventList || [];
  console.log(`Events found on MTGO: ${events.length}`);

  const newEvents = [];
  const newResults = [];

  for (const event of events) {
    const eventId = event.event_id || event.slug || event.id;
    const eventName = event.name || event.title || eventId;
    const eventDate = event.date || event.event_date;
    const eventType = event.format_name || event.type || 'Challenge';

    if (!eventId) continue;
    if (existingIds.has(eventId)) {
      console.log(`Skipping already processed event: ${eventId}`);
      continue;
    }
    if (!eventName.toLowerCase().includes('modern')) continue;

    console.log(`Processing: ${eventName} (${eventDate})`);
    const data = await fetchEventData(eventId);
    if (!data) continue;

    await new Promise(r => setTimeout(r, 500));

    const standings = data.standings || data.players || data.results || [];
    if (!standings.length) {
      console.log(`No standings found for ${eventId}`);
      continue;
    }

    newEvents.push({
      event_id: eventId,
      event_name: eventName,
      event_date: eventDate,
      event_type: eventType,
      format: 'Modern',
      scraped_at: new Date().toISOString()
    });

    for (const player of standings) {
      const archRaw = player.deck_name || player.archetype || player.deck?.name || '';
      newResults.push({
        event_id: eventId,
        player_name: player.player || player.name || player.username || null,
        archetype_raw: archRaw,
        archetype_canonical: canonicalise(archRaw),
        finish_position: player.rank || player.finish || player.position || null,
        points: player.points != null ? player.points : null,
        match_win_pct: player.match_win_percentage != null
          ? player.match_win_percentage / 100 : null,
        game_win_pct: player.game_win_percentage != null
          ? player.game_win_percentage / 100 : null,
        opp_match_win_pct: player.opponent_match_win_percentage != null
          ? player.opponent_match_win_percentage / 100 : null,
        created_at: new Date().toISOString()
      });
    }
  }

  console.log(`New events to insert: ${newEvents.length}`);
  console.log(`New results to insert: ${newResults.length}`);

  if (newEvents.length) {
    await sbUpsert('mtgo_events', newEvents);
    await sbUpsert('mtgo_results', newResults);
  }

  // Recompute summaries for last 30, 90, 180, 365 days and all time
  console.log('Recomputing summaries...');
  const allResults = await sbGet('mtgo_results',
    '?select=event_id,player_name,archetype_canonical,finish_position,points,match_win_pct,game_win_pct,opp_match_win_pct&limit=50000'
  );

  const windows = [
    { days: 30, label: '30d' },
    { days: 90, label: '90d' },
    { days: 180, label: '180d' },
    { days: 365, label: '365d' },
    { days: null, label: 'all' }
  ];

  // Get event dates for filtering
  const allEvents = await sbGet('mtgo_events', '?select=event_id,event_date');
  const eventDateMap = {};
  for (const e of allEvents || []) {
    eventDateMap[e.event_id] = e.event_date;
  }

  // Clear existing summaries
  await sbDelete('mtgo_archetype_summary',
    '?id=neq.00000000-0000-0000-0000-000000000000');
  await sbDelete('mtgo_pilot_summary',
    '?id=neq.00000000-0000-0000-0000-000000000000');

  for (const window of windows) {
    const now = new Date();
    const cutoff = window.days
      ? new Date(now - window.days * 24 * 60 * 60 * 1000)
      : null;
    const dateFrom = cutoff ? cutoff.toISOString().split('T')[0] : '2000-01-01';
    const dateTo = now.toISOString().split('T')[0];

    const filtered = (allResults || []).filter(r => {
      if (!cutoff) return true;
      const d = eventDateMap[r.event_id];
      return d && new Date(d) >= cutoff;
    });

    if (!filtered.length) continue;
    console.log(`Window ${window.label}: ${filtered.length} results`);

    const archSummary = computeArchetypeSummary(filtered, dateFrom, dateTo);
    const pilotSummary = computePilotSummary(filtered, dateFrom, dateTo);

    await sbUpsert('mtgo_archetype_summary', archSummary);
    await sbUpsert('mtgo_pilot_summary', pilotSummary);
  }

  console.log('MTGO scraper complete.', new Date().toISOString());
}

main().catch(e => { console.error('Fatal error:', e); process.exit(1); });
