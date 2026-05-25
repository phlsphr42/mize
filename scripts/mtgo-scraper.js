const fetch = (...args) => import('node-fetch').then(({default: f}) => f(...args));

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_KEY = process.env.SUPABASE_SERVICE_KEY;
const GITHUB_TOKEN = process.env.GITHUB_TOKEN;
const FBETTEGA_REPO = 'fbettega/MTG_decklistcache';
const FORMAT_REPO = 'Badaro/MTGOFormatData';
const GITHUB_API = 'https://api.github.com';

const sbHeaders = {
  'apikey': SUPABASE_KEY,
  'Authorization': `Bearer ${SUPABASE_KEY}`,
  'Content-Type': 'application/json',
  'Prefer': 'resolution=ignore-duplicates,return=minimal'
};

const ghHeaders = {
  'Accept': 'application/json',
  'User-Agent': 'Mize-Scraper',
  'Authorization': `token ${GITHUB_TOKEN}`
};

// ── SUPABASE HELPERS ───────────────────────────────────────────────

async function sbGet(table, params = '') {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${table}${params}`, { headers: sbHeaders });
  if (!res.ok) { console.error(`sbGet error ${res.status}: ${table}`); return []; }
  return res.json();
}

async function sbInsert(table, rows, batchSize = 300) {
  let total = 0;
  for (let i = 0; i < rows.length; i += batchSize) {
    const batch = rows.slice(i, i + batchSize);
    const res = await fetch(`${SUPABASE_URL}/rest/v1/${table}`, {
      method: 'POST', headers: sbHeaders, body: JSON.stringify(batch)
    });
    if (res.status >= 200 && res.status < 300) total += batch.length;
    else console.error(`Insert error ${table} batch ${i}: ${res.status} ${await res.text()}`);
    await sleep(100);
  }
  return total;
}

async function sbDelete(table, params) {
  await fetch(`${SUPABASE_URL}/rest/v1/${table}${params}`, {
    method: 'DELETE', headers: sbHeaders
  });
}

// ── GITHUB HELPERS ─────────────────────────────────────────────────

async function ghGet(url) {
  const res = await fetch(url, { headers: ghHeaders });
  if (!res.ok) { console.error(`GitHub error ${res.status}: ${url}`); return null; }
  return res.json();
}

async function ghGetRaw(url) {
  const res = await fetch(url, { headers: { 'User-Agent': 'Mize-Scraper', 'Authorization': `token ${GITHUB_TOKEN}` } });
  if (!res.ok) return null;
  return res.text();
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── ARCHETYPE DETECTION ────────────────────────────────────────────

let archetypeDefs = [];

async function loadArchetypeDefs() {
  console.log('Loading archetype definitions from MTGOFormatData...');
  const files = await ghGet(`${GITHUB_API}/repos/${FORMAT_REPO}/contents/Formats/Modern/Archetypes`);
  if (!files) return;
  for (const f of files) {
    if (!f.name.endsWith('.json')) continue;
    const raw = await ghGetRaw(f.download_url);
    if (!raw) continue;
    try {
      const d = JSON.parse(raw);
      d._name = f.name.replace('.json', '');
      archetypeDefs.push(d);
    } catch {}
    await sleep(50);
  }
  console.log(`Loaded ${archetypeDefs.length} archetype definitions`);
}

function testConditions(conditions, mainboard, sideboard) {
  if (!conditions || !conditions.length) return false;
  const mb = {};
  const sb = {};
  for (const [c, q] of Object.entries(mainboard || {})) mb[c.toLowerCase()] = q;
  for (const [c, q] of Object.entries(sideboard || {})) sb[c.toLowerCase()] = q;
  const both = { ...mb };
  for (const [c, q] of Object.entries(sb)) both[c] = (both[c] || 0) + q;

  for (const cond of conditions) {
    const t = cond.Type;
    const cards = (cond.Cards || []).map(c => c.toLowerCase());
    if (t === 'InMainboard') { if (!cards.every(c => mb[c] > 0)) return false; }
    else if (t === 'InSideboard') { if (!cards.every(c => sb[c] > 0)) return false; }
    else if (t === 'InMainOrSideboard') { if (!cards.every(c => both[c] > 0)) return false; }
    else if (t === 'OneOrMoreInMainboard') { if (!cards.some(c => mb[c] > 0)) return false; }
    else if (t === 'OneOrMoreInSideboard') { if (!cards.some(c => sb[c] > 0)) return false; }
    else if (t === 'OneOrMoreInMainOrSideboard') { if (!cards.some(c => both[c] > 0)) return false; }
    else if (t === 'TwoOrMoreInMainboard') { if (cards.filter(c => mb[c] > 0).length < 2) return false; }
    else if (t === 'TwoOrMoreInSideboard') { if (cards.filter(c => sb[c] > 0).length < 2) return false; }
    else if (t === 'TwoOrMoreInMainOrSideboard') { if (cards.filter(c => both[c] > 0).length < 2) return false; }
    else if (t === 'DoesNotContain') { if (cards.some(c => both[c] > 0)) return false; }
    else if (t === 'DoesNotContainMainboard') { if (cards.some(c => mb[c] > 0)) return false; }
    else if (t === 'DoesNotContainSideboard') { if (cards.some(c => sb[c] > 0)) return false; }
  }
  return true;
}

function detectArchetype(deckData) {
  const mainboard = {};
  const sideboard = {};
  for (const c of deckData.Mainboard || []) mainboard[c.CardName] = c.Count || 1;
  for (const c of deckData.Sideboard || []) sideboard[c.CardName] = c.Count || 1;
  for (const d of archetypeDefs) {
    if (testConditions(d.Conditions, mainboard, sideboard)) return d._name;
  }
  return 'Unknown';
}

function determineEventType(name) {
  const n = name.toLowerCase();
  if (n.includes('showcase')) return 'Showcase Challenge';
  if (n.includes('64')) return 'Challenge 64';
  if (n.includes('32')) return 'Challenge 32';
  return 'Challenge';
}

function normPct(val) {
  if (val == null) return null;
  try { const f = parseFloat(val); return Math.round((f > 1 ? f / 100 : f) * 1000000) / 1000000; }
  catch { return null; }
}

// ── SUMMARY COMPUTATION ────────────────────────────────────────────

function avg(arr) {
  const filtered = arr.filter(x => x != null);
  return filtered.length ? filtered.reduce((a, b) => a + b, 0) / filtered.length : null;
}

function computeArchetypeSummary(results, dateFrom, dateTo, windowDays) {
  const byArch = {};
  const total = results.length;
  for (const r of results) {
    const arch = r.archetype_canonical || 'Unknown';
    if (!byArch[arch]) byArch[arch] = { appearances: [], top8: 0, events: new Set(), points: [], mwp: [], gwp: [], omwp: [] };
    byArch[arch].appearances.push(r.finish_position);
    byArch[arch].events.add(r.event_id);
    if (r.finish_position && r.finish_position <= 8) byArch[arch].top8++;
    if (r.points != null) byArch[arch].points.push(r.points);
    if (r.match_win_pct != null) byArch[arch].mwp.push(r.match_win_pct);
    if (r.game_win_pct != null) byArch[arch].gwp.push(r.game_win_pct);
    if (r.opp_match_win_pct != null) byArch[arch].omwp.push(r.opp_match_win_pct);
  }
  const rows = [];
  for (const [arch, d] of Object.entries(byArch)) {
    const top32 = d.appearances.length;
    const top32Share = total > 0 ? top32 / total : 0;
    const top8Rate = top32 > 0 ? d.top8 / top32 : 0;
    const avgPts = avg(d.points);
    const avgMwp = avg(d.mwp);
    const avgGwp = avg(d.gwp);
    const avgOmwp = avg(d.omwp);
    const perf = ((avgPts != null ? (avgPts / 18) * 0.40 : 0) + ((avgMwp || 0) * 0.25) + ((avgGwp || 0) * 0.15) + ((avgOmwp || 0) * 0.20));
    const metaAdj = top32Share > 0 ? perf / top32Share : 0;
    rows.push({
      archetype_name: arch, date_from: dateFrom, date_to: dateTo, window_days: windowDays,
      event_count: d.events.size, top32_appearances: top32, top8_appearances: d.top8,
      top32_share: Math.round(top32Share * 1000000) / 1000000,
      top8_rate: Math.round(top8Rate * 1000000) / 1000000,
      avg_finish: avg(d.appearances) != null ? Math.round(avg(d.appearances) * 100) / 100 : null,
      avg_points: avgPts != null ? Math.round(avgPts * 100) / 100 : null,
      avg_mwp: avgMwp != null ? Math.round(avgMwp * 1000000) / 1000000 : null,
      avg_gwp: avgGwp != null ? Math.round(avgGwp * 1000000) / 1000000 : null,
      avg_omwp: avgOmwp != null ? Math.round(avgOmwp * 1000000) / 1000000 : null,
      performance_score: Math.round(perf * 1000000) / 1000000,
      meta_adjusted_score: Math.round(metaAdj * 10000) / 10000,
      last_updated: new Date().toISOString()
    });
  }
  return rows.sort((a, b) => b.meta_adjusted_score - a.meta_adjusted_score);
}

function computePilotSummary(results, dateFrom, dateTo, windowDays) {
  const byPilot = {};
  for (const r of results) {
    const p = r.player_name;
    if (!p) continue;
    if (!byPilot[p]) byPilot[p] = { appearances: [], top8: 0, events: new Set(), points: [], mwp: [], gwp: [], omwp: [], archetypes: {} };
    byPilot[p].appearances.push(r.finish_position);
    byPilot[p].events.add(r.event_id);
    if (r.finish_position && r.finish_position <= 8) byPilot[p].top8++;
    const arch = r.archetype_canonical || 'Unknown';
    byPilot[p].archetypes[arch] = (byPilot[p].archetypes[arch] || 0) + 1;
    if (r.points != null) byPilot[p].points.push(r.points);
    if (r.match_win_pct != null) byPilot[p].mwp.push(r.match_win_pct);
    if (r.game_win_pct != null) byPilot[p].gwp.push(r.game_win_pct);
    if (r.opp_match_win_pct != null) byPilot[p].omwp.push(r.opp_match_win_pct);
  }
  const rows = [];
  for (const [pilot, d] of Object.entries(byPilot)) {
    const avgPts = avg(d.points);
    const avgMwp = avg(d.mwp);
    const avgGwp = avg(d.gwp);
    const avgOmwp = avg(d.omwp);
    const primaryArch = Object.entries(d.archetypes).sort((a, b) => b[1] - a[1])[0]?.[0] || 'Unknown';
    const totalPts = d.points.reduce((a, b) => a + b, 0);
    const appearances = d.appearances.filter(x => x != null);
    const perf = ((avgPts != null ? (avgPts / 18) * 0.40 : 0) + ((avgMwp || 0) * 0.25) + ((avgGwp || 0) * 0.15) + ((avgOmwp || 0) * 0.20));
    rows.push({
      player_name: pilot, date_from: dateFrom, date_to: dateTo, window_days: windowDays,
      top32_appearances: d.appearances.length, top8_appearances: d.top8,
      best_finish: appearances.length ? Math.min(...appearances) : null,
      avg_finish: avg(d.appearances) != null ? Math.round(avg(d.appearances) * 100) / 100 : null,
      avg_points: avgPts != null ? Math.round(avgPts * 100) / 100 : null,
      avg_mwp: avgMwp != null ? Math.round(avgMwp * 1000000) / 1000000 : null,
      avg_gwp: avgGwp != null ? Math.round(avgGwp * 1000000) / 1000000 : null,
      avg_omwp: avgOmwp != null ? Math.round(avgOmwp * 1000000) / 1000000 : null,
      total_points: totalPts, events_played: d.events.size, primary_archetype: primaryArch,
      performance_score: Math.round(perf * 1000000) / 1000000,
      last_updated: new Date().toISOString()
    });
  }
  return rows.sort((a, b) => b.performance_score - a.performance_score);
}

// ── MAIN ───────────────────────────────────────────────────────────

async function main() {
  console.log('MTGO scraper starting...', new Date().toISOString());

  // Load archetype definitions
  await loadArchetypeDefs();
  if (!archetypeDefs.length) {
    console.error('No archetype definitions loaded. Exiting.');
    process.exit(1);
  }

  // Get existing event IDs
  const existingEvents = await sbGet('mtgo_events', '?select=event_id');
  const existingIds = new Set(existingEvents.map(e => e.event_id));
  console.log(`Existing events in DB: ${existingIds.size}`);

  // Scan fbettega for new Modern Challenge events
  const years = ['2023', '2024', '2025', '2026'];
  const newEventRows = [];
  const newResultRows = [];

  for (const year of years) {
    const months = await ghGet(`${GITHUB_API}/repos/${FBETTEGA_REPO}/contents/Tournaments/MTGO/${year}`);
    if (!months) continue;

    for (const month of months) {
      const days = await ghGet(`${GITHUB_API}/repos/${FBETTEGA_REPO}/contents/Tournaments/MTGO/${year}/${month.name}`);
      if (!days) continue;
      await sleep(150);

      for (const day of days) {
        const files = await ghGet(`${GITHUB_API}/repos/${FBETTEGA_REPO}/contents/Tournaments/MTGO/${year}/${month.name}/${day.name}`);
        if (!files) continue;
        await sleep(150);

        for (const f of files) {
          const fname = f.name.toLowerCase();
          if (!fname.startsWith('modern-challenge') && !fname.startsWith('modern-showcase-challenge')) continue;
          if (!fname.endsWith('.json')) continue;

          const eventId = f.name.replace('.json', '');
          if (existingIds.has(eventId)) continue;

          console.log(`New event: ${eventId}`);
          const raw = await ghGetRaw(f.download_url);
          if (!raw) continue;

          let data;
          try { data = JSON.parse(raw); } catch { continue; }

          const tournament = data.Tournament || {};
          const standings = data.Standings || [];
          const decks = data.Decks || [];

          if (!standings.length) continue;

          const eventDate = (tournament.Date || `${year}-${month.name}-${day.name}`).slice(0, 10);
          const decksById = {};
          for (const d of decks) decksById[d.Player] = d;

          newEventRows.push({
            event_id: eventId,
            event_name: tournament.Name || eventId,
            event_date: eventDate,
            event_type: determineEventType(eventId),
            format: 'Modern',
            scraped_at: new Date().toISOString()
          });

          for (const player of standings) {
            const deck = decksById[player.Player] || {};
            newResultRows.push({
              event_id: eventId,
              player_name: player.Player || null,
              archetype_raw: detectArchetype(deck),
              archetype_canonical: detectArchetype(deck),
              finish_position: player.Rank || null,
              points: player.Points || null,
              match_win_pct: normPct(player.OMWP),
              game_win_pct: normPct(player.GWP),
              opp_match_win_pct: normPct(player.OGWP),
              created_at: new Date().toISOString()
            });
          }
          await sleep(300);
        }
      }
    }
  }

  console.log(`New events: ${newEventRows.length}, new results: ${newResultRows.length}`);

  if (newEventRows.length) {
    await sbInsert('mtgo_events', newEventRows);
    await sbInsert('mtgo_results', newResultRows);
  } else {
    console.log('No new events found. Summaries will still be recomputed.');
  }

  // Recompute summaries
  console.log('Recomputing summaries...');
  const allResults = await sbGet('mtgo_results',
    '?select=event_id,player_name,archetype_canonical,finish_position,points,match_win_pct,game_win_pct,opp_match_win_pct&limit=100000'
  );
  const allEvents = await sbGet('mtgo_events', '?select=event_id,event_date');
  const eventDateMap = {};
  for (const e of allEvents) eventDateMap[e.event_id] = e.event_date;

  console.log(`Total results: ${allResults.length}, total events: ${allEvents.length}`);

  const now = new Date();
  const windows = [
    { days: 30 }, { days: 90 }, { days: 180 }, { days: 365 }, { days: null }
  ];

  const allArchSummaries = [];
  const allPilotSummaries = [];

  for (const w of windows) {
    const cutoff = w.days ? new Date(now - w.days * 86400000) : null;
    const dateFrom = cutoff ? cutoff.toISOString().split('T')[0] : '2000-01-01';
    const dateTo = now.toISOString().split('T')[0];
    const windowDays = w.days || 0;

    const filtered = allResults.filter(r =>
      !cutoff || (eventDateMap[r.event_id] || '0000-00-00') >= dateFrom
    );

    if (!filtered.length) continue;
    console.log(`Window ${w.days || 'all'}: ${filtered.length} results`);

    allArchSummaries.push(...computeArchetypeSummary(filtered, dateFrom, dateTo, windowDays));
    allPilotSummaries.push(...computePilotSummary(filtered, dateFrom, dateTo, windowDays));
  }

  // Clear and rewrite summaries
  await sbDelete('mtgo_archetype_summary', '?id=neq.00000000-0000-0000-0000-000000000000');
  await sbDelete('mtgo_pilot_summary', '?id=neq.00000000-0000-0000-0000-000000000000');
  await sleep(1000);

  console.log(`Writing ${allArchSummaries.length} archetype summary rows...`);
  await sbInsert('mtgo_archetype_summary', allArchSummaries);

  console.log(`Writing ${allPilotSummaries.length} pilot summary rows...`);
  await sbInsert('mtgo_pilot_summary', allPilotSummaries);

  console.log('MTGO scraper complete.', new Date().toISOString());
}

main().catch(e => { console.error('Fatal error:', e); process.exit(1); });
