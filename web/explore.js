'use strict';

// Timeline map: asks the local server for the chosen dates and draws them.
// All text from the data is inserted with textContent, never as HTML.

const PALETTE = ['#1a73e8', '#d93025', '#188038', '#8430ce', '#e37400', '#007b83', '#c5221f', '#5f6368'];

const els = {
  from: document.getElementById('from'),
  to: document.getElementById('to'),
  prev: document.getElementById('prev'),
  next: document.getElementById('next'),
  span: document.getElementById('span'),
  lines: document.getElementById('lines'),
  clean: document.getElementById('clean'),
  readings: document.getElementById('readings'),
  visits: document.getElementById('visits'),
  heat: document.getElementById('heat'),
  status: document.getElementById('status'),
  progress: document.getElementById('progress'),
};

const map = L.map('map', { preferCanvas: true, zoomControl: false }).setView([-25.3, 133.8], 4);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  className: 'soft-tiles',
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

// Layer switches are Material filter chips; aria-pressed holds their state.
function isOn(chip) {
  return chip.getAttribute('aria-pressed') === 'true';
}

function setChip(chip, on) {
  chip.setAttribute('aria-pressed', on ? 'true' : 'false');
  chip.classList.toggle('selected', on);
}

function topCardPadding() {
  const card = document.querySelector('.top-card');
  return { paddingTopLeft: [30, card.offsetHeight + 40], paddingBottomRight: [30, 30] };
}

const layers = {
  lines: L.layerGroup().addTo(map),
  readings: L.layerGroup().addTo(map),
  visits: L.layerGroup().addTo(map),
  heat: null,
};

let days = [];
let requestNumber = 0;

// ---------------------------------------------------------------------------
// Dates (always 'YYYY-MM-DD' text, calculated in UTC to avoid clock changes)
// ---------------------------------------------------------------------------

function toDate(text) {
  const [y, m, d] = text.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

function toText(date) {
  return date.toISOString().slice(0, 10);
}

function addDays(text, count) {
  const date = toDate(text);
  date.setUTCDate(date.getUTCDate() + count);
  return toText(date);
}

function longDate(text) {
  return toDate(text).toLocaleDateString(undefined, {
    weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC',
  });
}

// Index of the first day in `days` that is >= text.
function lowerBound(text) {
  let low = 0;
  let high = days.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (days[middle] < text) low = middle + 1; else high = middle;
  }
  return low;
}

function setRange(from, to) {
  els.from.value = from;
  els.to.value = to;
  updateButtons();
  load(true);
}

function updateButtons() {
  els.prev.disabled = !days.length || lowerBound(els.from.value) === 0;
  els.next.disabled = !days.length || lowerBound(addDays(els.to.value, 1)) >= days.length;
}

function previousDay() {
  const index = lowerBound(els.from.value) - 1;
  if (index >= 0) setRange(days[index], days[index]);
}

function nextDay() {
  const index = lowerBound(addDays(els.to.value, 1));
  if (index < days.length) setRange(days[index], days[index]);
}

function widen(span) {
  const from = els.from.value;
  if (!from) return;
  const date = toDate(from);
  if (span === 'day') {
    setRange(from, from);
  } else if (span === 'week') {
    const monday = addDays(from, -((date.getUTCDay() + 6) % 7));
    setRange(monday, addDays(monday, 6));
  } else if (span === 'month') {
    const first = toText(new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1)));
    const last = toText(new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)));
    setRange(first, last);
  } else if (span === 'year') {
    setRange(`${date.getUTCFullYear()}-01-01`, `${date.getUTCFullYear()}-12-31`);
  } else if (span === 'all' && days.length) {
    setRange(days[0], days[days.length - 1]);
  }
}

// ---------------------------------------------------------------------------
// Talking to the server
// ---------------------------------------------------------------------------

async function getJson(path, params) {
  const query = new URLSearchParams(params).toString();
  const response = await fetch(query ? `${path}?${query}` : path);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function popup(title, lines) {
  const box = document.createElement('div');
  const heading = document.createElement('div');
  heading.className = 'popup-title';
  heading.textContent = title;
  box.appendChild(heading);
  for (const text of lines) {
    if (!text) continue;
    const line = document.createElement('div');
    line.className = 'popup-line';
    line.textContent = text;
    box.appendChild(line);
  }
  return box;
}

function setStatus(text, isError) {
  els.status.textContent = text;
  els.status.classList.toggle('error', Boolean(isError));
}

async function load(fitView) {
  const from = els.from.value;
  const to = els.to.value;
  if (!from || !to) return;
  if (to < from) {
    setStatus('"To" is before "From".', true);
    return;
  }
  const mine = ++requestNumber;
  setStatus('Loading…');
  els.progress.classList.add('active');
  const range = { from, to };
  const wants = {
    track: isOn(els.lines) ? getJson('/api/track', { ...range, clean: isOn(els.clean) ? '1' : '0' }) : null,
    readings: isOn(els.readings) ? getJson('/api/readings', range) : null,
    visits: isOn(els.visits) ? getJson('/api/visits', range) : null,
    heat: isOn(els.heat) ? getJson('/api/heat', range) : null,
  };

  let results;
  try {
    const keys = Object.keys(wants);
    const values = await Promise.all(keys.map((key) => wants[key]));
    results = Object.fromEntries(keys.map((key, i) => [key, values[i]]));
  } catch (error) {
    if (mine === requestNumber) {
      setStatus(error.message, true);
      els.progress.classList.remove('active');
    }
    return;
  }
  if (mine !== requestNumber) return; // a newer request has started
  els.progress.classList.remove('active');

  draw(results, fitView);
}

function draw(results, fitView) {
  layers.lines.clearLayers();
  layers.readings.clearLayers();
  layers.visits.clearLayers();
  if (layers.heat) {
    map.removeLayer(layers.heat);
    layers.heat = null;
  }

  const bounds = L.latLngBounds([]);
  const notes = [];

  if (results.track) {
    const colours = new Map();
    for (const feature of results.track.features) {
      const day = feature.properties.day;
      if (!colours.has(day)) colours.set(day, PALETTE[colours.size % PALETTE.length]);
      const latLngs = feature.geometry.coordinates.map(([lng, lat]) => [lat, lng]);
      const line = L.polyline(latLngs, { color: colours.get(day), weight: 4, opacity: 0.9, lineCap: 'round' });
      const p = feature.properties;
      line.bindPopup(() => popup(`${p.start} to ${p.end.slice(11)}`, [`${p.km} km`]));
      line.addTo(layers.lines);
      bounds.extend(line.getBounds());
    }
    const s = results.track.stats;
    notes.push(`${s.segments} line${s.segments === 1 ? '' : 's'}, ${s.km.toLocaleString()} km`
      + ` (${s.readings.toLocaleString()} points drawn as ${s.points.toLocaleString()})`);
    if (s.truncated) notes.push('too many points: only the start of this period is drawn');
  }

  if (results.readings) {
    for (const [lat, lng, accuracy, time] of results.readings.points) {
      const dot = L.circleMarker([lat, lng], {
        radius: 2.5, weight: 0, fillColor: '#444746', fillOpacity: 0.7,
      });
      dot.bindPopup(() => popup(time, [accuracy == null ? 'no accuracy given' : `within ${Math.round(accuracy)} m`]));
      dot.addTo(layers.readings);
      bounds.extend([lat, lng]);
    }
    notes.push(`${results.readings.points.length.toLocaleString()} readings`
      + (results.readings.truncated ? ' (first ones only)' : ''));
  }

  if (results.visits) {
    for (const visit of results.visits.visits) {
      const marker = L.circleMarker([visit.lat, visit.lng], {
        radius: 7, color: '#ffffff', weight: 2, fillColor: '#0b57d0', fillOpacity: 1,
      });
      const title = visit.name || (visit.type && visit.type !== 'UNKNOWN' ? visit.type : 'Visit');
      const sure = visit.probability == null ? '' : `Google was ${Math.round(visit.probability * 100)}% sure of the place`;
      marker.bindPopup(() => popup(title, [visit.address, `${visit.start} to ${visit.end}`, sure]));
      marker.addTo(layers.visits);
      bounds.extend([visit.lat, visit.lng]);
    }
    notes.push(`${results.visits.visits.length.toLocaleString()} visits`);
  }

  if (results.heat && results.heat.cells.length) {
    const max = Math.max(...results.heat.cells.map((cell) => cell[2]));
    layers.heat = L.heatLayer(results.heat.cells, { radius: 10, blur: 12, max, minOpacity: 0.3 }).addTo(map);
    for (const [lat, lng] of results.heat.cells) bounds.extend([lat, lng]);
  }

  const range = els.from.value === els.to.value
    ? longDate(els.from.value)
    : `${longDate(els.from.value)} to ${longDate(els.to.value)}`;
  const nothing = !bounds.isValid();
  setStatus(`${range}: ${nothing ? 'nothing recorded' : notes.join(' · ')}`);
  if (fitView && !nothing) map.fitBounds(bounds, { ...topCardPadding(), maxZoom: 17 });
}

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

els.prev.addEventListener('click', previousDay);
els.next.addEventListener('click', nextDay);
els.from.addEventListener('change', () => {
  if (els.to.value < els.from.value) els.to.value = els.from.value;
  updateButtons();
  load(true);
});
els.to.addEventListener('change', () => { updateButtons(); load(true); });
els.span.addEventListener('change', () => {
  widen(els.span.value);
  els.span.value = '';
});
for (const chip of [els.lines, els.clean, els.readings, els.visits, els.heat]) {
  chip.addEventListener('click', () => {
    setChip(chip, !isOn(chip));
    load(false);
  });
}
document.addEventListener('keydown', (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLSelectElement
      || event.target instanceof HTMLButtonElement) return;
  if (event.key === 'ArrowLeft') previousDay();
  if (event.key === 'ArrowRight') nextDay();
});

(async function start() {
  try {
    const summary = await getJson('/api/summary');
    days = summary.days;
  } catch (error) {
    setStatus(error.message, true);
    return;
  }
  if (!days.length) {
    setStatus('Nothing imported yet. Import an export with main.py first.', true);
    return;
  }
  const last = days[days.length - 1];
  setRange(last, last);
}());
