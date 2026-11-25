'use strict';

// Day timeline: one day as stops and journeys, each of which can be corrected.
// All text from the data is inserted with textContent, never as HTML.

const MODE_COLOURS = {
  walking: '#16a34a', running: '#15803d', cycling: '#0891b2', driving: '#2563eb',
  motorcycling: '#1d4ed8', bus: '#9333ea', train: '#7c3aed', tram: '#a855f7',
  metro: '#6d28d9', ferry: '#0e7490', flying: '#64748b', unknown: '#ea580c',
};

const els = {
  date: document.getElementById('date'),
  prev: document.getElementById('prev'),
  next: document.getElementById('next'),
  title: document.getElementById('day-title'),
  summary: document.getElementById('summary'),
  snapAll: document.getElementById('snap-all'),
  reset: document.getElementById('reset'),
  note: document.getElementById('note'),
  items: document.getElementById('items'),
};

const map = L.map('map', { preferCanvas: true }).setView([-25.3, 133.8], 4);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
const drawn = L.layerGroup().addTo(map);

let day = null;          // the current day's data from the server
let busy = false;
let selected = null;     // id of the highlighted item
const shapes = new Map(); // item id -> Leaflet layer

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function button(text, onClick, title) {
  const node = el('button', 'link', text);
  node.type = 'button';
  if (title) node.title = title;
  node.addEventListener('click', (event) => {
    event.stopPropagation();
    onClick();
  });
  return node;
}

function duration(startMs, endMs) {
  const minutes = Math.max(0, Math.round((endMs - startMs) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
}

function longDate(text) {
  const [y, m, d] = text.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
  });
}

function setNote(text, isError) {
  els.note.textContent = text || '';
  els.note.classList.toggle('error', Boolean(isError));
}

async function getDay(date) {
  const response = await fetch(`/api/day?date=${encodeURIComponent(date)}`);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Could not load the day');
  return body;
}

async function sendEdit(change) {
  const response = await fetch('/api/edit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Timeline-Edit': '1' },
    body: JSON.stringify({ date: day.date, ...change }),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'The change was not saved');
  return body;
}

// ---------------------------------------------------------------------------
// Loading and drawing a day
// ---------------------------------------------------------------------------

async function show(date, keepView) {
  setNote('Loading…');
  try {
    day = await getDay(date);
  } catch (error) {
    setNote(error.message, true);
    return;
  }
  const url = new URL(window.location.href);
  url.searchParams.set('date', day.date);
  window.history.replaceState(null, '', url);
  setNote('');
  render(keepView);
}

async function apply(change, workingText) {
  if (busy) return null;
  busy = true;
  setNote(workingText || 'Saving…');
  try {
    const result = await sendEdit(change);
    day = result.day;
    render(true);
    setNote(result.note || '');
    return result;
  } catch (error) {
    setNote(error.message, true);
    return null;
  } finally {
    busy = false;
  }
}

function render(keepView) {
  els.date.value = day.date;
  els.prev.disabled = !day.previous;
  els.next.disabled = !day.next;
  els.title.textContent = longDate(day.date);
  const s = day.summary;
  const parts = [`${s.stops} stop${s.stops === 1 ? '' : 's'}`,
    `${s.moves} journey${s.moves === 1 ? '' : 's'}`, `${s.km.toLocaleString()} km`];
  if (s.source === 'readings') parts.push('stops found from readings');
  if (s.edited) parts.push('edited');
  els.summary.textContent = day.items.length ? parts.join(' · ') : 'Nothing recorded on this day.';
  els.reset.disabled = !s.edited;
  els.snapAll.disabled = !day.items.some((i) => i.kind === 'move' && !i.hidden && !i.snapped && i.mode !== 'flying');

  drawMap(keepView);
  drawList();
}

function drawMap(keepView) {
  drawn.clearLayers();
  shapes.clear();
  const bounds = L.latLngBounds([]);
  let number = 0;
  for (const item of day.items) {
    if (item.hidden) continue;
    let layer;
    if (item.kind === 'move' && item.line.length > 1) {
      layer = L.polyline(item.line, {
        color: MODE_COLOURS[item.mode] || MODE_COLOURS.unknown,
        weight: item.id === selected ? 7 : 4,
        opacity: 0.9,
        dashArray: item.estimated || item.mode === 'flying' ? '6 8' : null,
      });
    } else if (item.kind === 'stop' && item.lat !== null) {
      number += 1;
      layer = L.marker([item.lat, item.lng], {
        icon: L.divIcon({ className: 'stop-pin', html: String(number), iconSize: [24, 24] }),
        title: item.name,
      });
    }
    if (!layer) continue;
    layer.on('click', () => select(item.id, false));
    layer.addTo(drawn);
    shapes.set(item.id, layer);
    if (layer.getBounds) bounds.extend(layer.getBounds()); else bounds.extend(layer.getLatLng());
  }
  if (!keepView && bounds.isValid()) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
}

function drawList() {
  els.items.replaceChildren();
  let number = 0;
  for (const item of day.items) {
    if (item.kind === 'stop' && !item.hidden && item.lat !== null) number += 1;
    els.items.appendChild(item.kind === 'stop' ? stopCard(item, number) : moveCard(item));
  }
}

function select(id, zoom) {
  selected = id;
  for (const card of els.items.children) card.classList.toggle('selected', card.dataset.id === id);
  const card = [...els.items.children].find((c) => c.dataset.id === id);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  for (const [itemId, layer] of shapes) {
    if (layer.setStyle) layer.setStyle({ weight: itemId === id ? 7 : 4 });
  }
  const layer = shapes.get(id);
  if (zoom && layer) {
    if (layer.getBounds) map.fitBounds(layer.getBounds(), { padding: [60, 60], maxZoom: 17 });
    else map.setView(layer.getLatLng(), Math.max(map.getZoom(), 16));
  }
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

function card(item) {
  const li = el('li', `card ${item.kind}${item.hidden ? ' hidden-item' : ''}${item.id === selected ? ' selected' : ''}`);
  li.dataset.id = item.id;
  li.tabIndex = 0;
  li.addEventListener('click', () => select(item.id, true));
  li.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && event.target === li) select(item.id, true);
  });
  return li;
}

function stopCard(item, number) {
  const li = card(item);
  const head = el('div', 'card-head');
  if (!item.hidden) head.appendChild(el('span', 'stop-number', String(number)));
  head.appendChild(el('span', 'stop-name', item.name));
  li.appendChild(head);
  if (item.address) li.appendChild(el('div', 'muted', item.address));
  const sourceText = {
    you: 'named by you', 'your place': 'your named place', google: 'name from Google',
    unknown: 'no name recorded',
  }[item.name_source];
  li.appendChild(el('div', 'muted', `${item.start_text} – ${item.end_text} · ${duration(item.start, item.end)} · ${sourceText}`));

  const actions = el('div', 'card-actions');
  if (item.hidden) {
    actions.appendChild(el('span', 'badge', 'Hidden'));
    actions.appendChild(button('Show', () => apply({ action: 'show', item_id: item.id })));
  } else {
    actions.appendChild(button('Rename', () => openRename(li, item)));
    if (item.name_source === 'you') {
      actions.appendChild(button('Use original name', () => apply({ action: 'rename', item_id: item.id, name: '' })));
    }
    actions.appendChild(button('Hide', () => apply({ action: 'hide', item_id: item.id }), 'Leave this stop out of the day'));
  }
  li.appendChild(actions);
  return li;
}

function openRename(li, item) {
  if (li.querySelector('form')) return;
  const form = el('form', 'rename');
  form.addEventListener('click', (event) => event.stopPropagation());
  const input = el('input');
  input.type = 'text';
  input.maxLength = 120;
  input.value = item.name_source === 'unknown' ? '' : item.name;
  input.placeholder = 'Name of this place';
  input.setAttribute('aria-label', 'Name of this place');
  const everywhereLabel = el('label', 'everywhere');
  const everywhere = el('input');
  everywhere.type = 'checkbox';
  everywhereLabel.append(everywhere, ' Use this name for every visit within 75 m');
  const save = el('button', null, 'Save');
  save.type = 'submit';
  const cancel = button('Cancel', () => form.remove());
  form.append(input, everywhereLabel, save, cancel);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    apply({ action: 'rename', item_id: item.id, name: input.value, everywhere: everywhere.checked });
  });
  li.appendChild(form);
  input.focus();
}

function moveCard(item) {
  const li = card(item);
  li.style.setProperty('--mode-colour', MODE_COLOURS[item.mode] || MODE_COLOURS.unknown);
  const head = el('div', 'card-head');
  const select = el('select', 'mode');
  select.setAttribute('aria-label', 'Travel mode');
  select.disabled = item.hidden;
  for (const [code, label] of Object.entries(day.modes)) {
    const option = el('option', null, label);
    option.value = code;
    option.selected = code === item.mode;
    select.appendChild(option);
  }
  select.addEventListener('click', (event) => event.stopPropagation());
  select.addEventListener('change', () => apply({ action: 'mode', item_id: item.id, mode: select.value }));
  head.appendChild(select);
  head.appendChild(el('span', 'move-facts', `${item.km.toLocaleString()} km · ${duration(item.start, item.end)}`));
  li.appendChild(head);

  const notes = [`${item.start_text} – ${item.end_text}`];
  notes.push({ you: 'mode set by you', google: 'mode from Google', guess: 'mode guessed from speed' }[item.mode_source]);
  li.appendChild(el('div', 'muted', notes.join(' · ')));

  const actions = el('div', 'card-actions');
  if (item.snapped) actions.appendChild(el('span', 'badge good', 'Follows roads'));
  if (item.estimated) actions.appendChild(el('span', 'badge', 'No readings: straight line'));
  if (item.hidden) {
    actions.appendChild(el('span', 'badge', 'Hidden'));
    actions.appendChild(button('Show', () => apply({ action: 'show', item_id: item.id })));
  } else {
    if (item.snapped) {
      actions.appendChild(button('Undo snap', () => apply({ action: 'unsnap', item_id: item.id })));
    } else if (!item.estimated && item.mode !== 'flying') {
      actions.appendChild(button('Snap to roads', () => snapOne(item)));
    }
    if (item.mode_source === 'you') {
      actions.appendChild(button('Use original mode', () => apply({ action: 'mode', item_id: item.id, mode: null })));
    }
    actions.appendChild(button('Hide', () => apply({ action: 'hide', item_id: item.id }), 'Leave this journey out of the day'));
  }
  li.appendChild(actions);
  return li;
}

function snapOne(item) {
  return apply({ action: 'snap', item_id: item.id }, 'Downloading roads and matching this journey…');
}

// ---------------------------------------------------------------------------
// Day-level actions
// ---------------------------------------------------------------------------

async function snapAll() {
  const ids = day.items
    .filter((i) => i.kind === 'move' && !i.hidden && !i.snapped && !i.estimated && i.mode !== 'flying')
    .map((i) => i.id);
  const notes = [];
  for (let n = 0; n < ids.length; n += 1) {
    const item = day.items.find((i) => i.id === ids[n]);
    if (!item) continue;
    select(item.id, false);
    const result = await apply({ action: 'snap', item_id: item.id },
      `Snapping journey ${n + 1} of ${ids.length}…`);
    if (!result) notes.push(`Journey ${n + 1}: ${els.note.textContent}`);
  }
  setNote(notes.length ? notes.join(' ') : `Done: ${ids.length} journey${ids.length === 1 ? '' : 's'} checked.`, notes.length > 0);
}

els.prev.addEventListener('click', () => day && day.previous && show(day.previous));
els.next.addEventListener('click', () => day && day.next && show(day.next));
els.date.addEventListener('change', () => els.date.value && show(els.date.value));
els.snapAll.addEventListener('click', snapAll);
els.reset.addEventListener('click', () => {
  if (window.confirm('Undo every correction you made to this day? Names you gave to places everywhere are kept.')) {
    apply({ action: 'reset' });
  }
});
document.addEventListener('keydown', (event) => {
  const target = event.target;
  if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement) return;
  if (event.key === 'ArrowLeft' && day && day.previous) show(day.previous);
  if (event.key === 'ArrowRight' && day && day.next) show(day.next);
});

(async function start() {
  const wanted = new URL(window.location.href).searchParams.get('date');
  if (wanted) {
    show(wanted);
    return;
  }
  try {
    const response = await fetch('/api/summary');
    const summary = await response.json();
    if (!summary.days || !summary.days.length) {
      setNote('Nothing imported yet. Import an export with main.py first.', true);
      return;
    }
    show(summary.days[summary.days.length - 1]);
  } catch (error) {
    setNote(error.message, true);
  }
}());
