'use strict';

// Day timeline: one day as places and journeys, each of which can be corrected.
// All text from the data is inserted with textContent, never as HTML.

const MODE_STYLE = {
  walking: { icon: 'directions_walk', colour: '#188038' },
  running: { icon: 'directions_run', colour: '#137333' },
  cycling: { icon: 'directions_bike', colour: '#00897b' },
  driving: { icon: 'directions_car', colour: '#1a73e8' },
  motorcycling: { icon: 'two_wheeler', colour: '#185abc' },
  bus: { icon: 'directions_bus', colour: '#8430ce' },
  train: { icon: 'train', colour: '#7627bb' },
  tram: { icon: 'tram', colour: '#a142f4' },
  metro: { icon: 'subway', colour: '#681da8' },
  ferry: { icon: 'directions_boat', colour: '#007b83' },
  flying: { icon: 'flight', colour: '#5f6368' },
  unknown: { icon: 'commute', colour: '#e37400' },
};

const $ = (id) => document.getElementById(id);
const els = {
  date: $('date'),
  dateButton: $('date-button'),
  prev: $('prev'),
  next: $('next'),
  title: $('day-title'),
  summary: $('summary'),
  snapAll: $('snap-all'),
  reset: $('reset'),
  items: $('items'),
  progress: $('progress'),
  snackbar: $('snackbar'),
  dialog: $('rename-dialog'),
  renameForm: $('rename-form'),
  renameInput: $('rename-input'),
  renameEverywhere: $('rename-everywhere'),
};

const map = L.map('map', { preferCanvas: true, zoomControl: false }).setView([-25.3, 133.8], 4);
L.control.zoom({ position: 'bottomright' }).addTo(map);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19,
  className: 'soft-tiles',
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
const drawn = L.layerGroup().addTo(map);

let day = null;            // the current day, as sent by the server
let busy = false;
let selected = null;       // id of the highlighted place or journey
let openMenu = null;
let renaming = null;
let snackTimer = null;
const shapes = new Map();  // item id -> { line, casing } or { marker }

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function icon(name, extra) {
  return el('span', `material-symbols-outlined${extra ? ` ${extra}` : ''}`, name);
}

function styleOf(mode) {
  return MODE_STYLE[mode] || MODE_STYLE.unknown;
}

function duration(startMs, endMs) {
  const minutes = Math.max(0, Math.round((endMs - startMs) / 60000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours} hr ${minutes % 60} min` : `${hours} hr`;
}

function km(value) {
  return value < 10 ? `${value.toFixed(1)} km` : `${Math.round(value).toLocaleString()} km`;
}

function longDate(text) {
  const [y, m, d] = text.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC',
  });
}

function panelPadding() {
  const panel = document.querySelector('.side-panel');
  if (window.innerWidth <= 720) return { paddingTopLeft: [40, 40], paddingBottomRight: [40, panel.offsetHeight + 20] };
  return { paddingTopLeft: [panel.offsetWidth + 60, 40], paddingBottomRight: [40, 40] };
}

function setWorking(on) {
  els.progress.classList.toggle('active', on);
}

// A message that stays until replaced (for work in progress), or fades after 6 s.
function snack(message, isError, persistent) {
  clearTimeout(snackTimer);
  if (!message) {
    els.snackbar.hidden = true;
    return;
  }
  els.snackbar.querySelector('.message').textContent = message;
  els.snackbar.classList.toggle('error', Boolean(isError));
  els.snackbar.hidden = false;
  if (!isError && !persistent) snackTimer = setTimeout(() => { els.snackbar.hidden = true; }, 6000);
}

// ---------------------------------------------------------------------------
// Talking to the server
// ---------------------------------------------------------------------------

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

async function show(date, keepView) {
  closeMenu();
  setWorking(true);
  try {
    day = await getDay(date);
  } catch (error) {
    snack(error.message, true);
    return;
  } finally {
    setWorking(false);
  }
  const url = new URL(window.location.href);
  url.searchParams.set('date', day.date);
  window.history.replaceState(null, '', url);
  if (!keepView) selected = null;
  render(keepView);
}

async function apply(change, workingText) {
  if (busy) return null;
  busy = true;
  closeMenu();
  setWorking(true);
  if (workingText) snack(workingText, false, true);
  try {
    const result = await sendEdit(change);
    day = result.day;
    render(true);
    snack(result.note || '');
    return result;
  } catch (error) {
    snack(error.message, true);
    return null;
  } finally {
    busy = false;
    setWorking(false);
  }
}

// ---------------------------------------------------------------------------
// Drawing
// ---------------------------------------------------------------------------

function render(keepView) {
  els.date.value = day.date;
  els.prev.disabled = !day.previous;
  els.next.disabled = !day.next;
  els.title.textContent = longDate(day.date);

  const s = day.summary;
  els.summary.replaceChildren();
  if (day.items.length) {
    const chips = [
      ['location_on', `${s.stops} place${s.stops === 1 ? '' : 's'}`],
      ['route', `${s.moves} journey${s.moves === 1 ? '' : 's'}`],
      ['timeline', km(s.km)],
    ];
    if (s.edited) chips.push(['edit', 'Edited']);
    for (const [name, text] of chips) {
      const chip = el('span', 'chip assist');
      chip.append(icon(name), text);
      els.summary.appendChild(chip);
    }
  }
  els.reset.disabled = !s.edited;
  els.snapAll.disabled = !snappable().length;

  drawMap(keepView);
  drawList();
}

function snappable() {
  return day.items.filter((i) => i.kind === 'move' && !i.hidden && !i.snapped && !i.estimated && i.mode !== 'flying');
}

function drawMap(keepView) {
  drawn.clearLayers();
  shapes.clear();
  const bounds = L.latLngBounds([]);
  let number = 0;
  for (const item of day.items) {
    if (item.hidden) continue;
    if (item.kind === 'move' && item.line.length > 1) {
      const style = styleOf(item.mode);
      const dashed = item.estimated || item.mode === 'flying';
      const casing = L.polyline(item.line, { color: '#ffffff', weight: 9, opacity: 0.9 });
      const line = L.polyline(item.line, {
        color: style.colour, weight: 5, opacity: 1, dashArray: dashed ? '2 10' : null, lineCap: 'round',
      });
      for (const layer of [casing, line]) {
        layer.on('click', () => select(item.id, false));
        layer.addTo(drawn);
      }
      shapes.set(item.id, { line, casing });
      bounds.extend(line.getBounds());
    } else if (item.kind === 'stop' && item.lat !== null) {
      number += 1;
      const marker = L.marker([item.lat, item.lng], {
        icon: L.divIcon({ className: 'map-pin', html: String(number), iconSize: [28, 28] }),
        title: item.name,
        riseOnHover: true,
      });
      marker.on('click', () => select(item.id, false));
      marker.addTo(drawn);
      shapes.set(item.id, { marker });
      bounds.extend(marker.getLatLng());
    }
  }
  highlight();
  if (!keepView && bounds.isValid()) map.fitBounds(bounds, { ...panelPadding(), maxZoom: 16 });
}

function highlight() {
  for (const [id, shape] of shapes) {
    const on = id === selected;
    if (shape.line) {
      shape.line.setStyle({ weight: on ? 8 : 5 });
      shape.casing.setStyle({ weight: on ? 13 : 9 });
      if (on) { shape.casing.bringToFront(); shape.line.bringToFront(); }
    }
    if (shape.marker) {
      const node = shape.marker.getElement();
      if (node) node.classList.toggle('selected', on);
      shape.marker.setZIndexOffset(on ? 1000 : 0);
    }
  }
  for (const row of els.items.children) row.classList.toggle('selected', row.dataset.id === selected);
}

function select(id, zoom) {
  selected = id;
  highlight();
  const row = [...els.items.children].find((r) => r.dataset.id === id);
  if (row) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  const shape = shapes.get(id);
  if (zoom && shape) {
    if (shape.line) map.fitBounds(shape.line.getBounds(), { ...panelPadding(), maxZoom: 17 });
    else map.setView(shape.marker.getLatLng(), Math.max(map.getZoom(), 16));
  }
}

function drawList() {
  els.items.replaceChildren();
  if (!day.items.length) {
    const empty = el('li', 'empty-day');
    empty.append(icon('location_on'), 'No places or journeys were recorded on this day.');
    els.items.appendChild(empty);
    return;
  }
  let number = 0;
  for (const item of day.items) {
    if (item.kind === 'stop' && !item.hidden && item.lat !== null) number += 1;
    els.items.appendChild(item.kind === 'stop' ? placeRow(item, number) : travelRow(item));
  }
}

function baseRow(item, kind) {
  const li = el('li', `row ${kind}${item.hidden ? ' is-hidden' : ''}`);
  li.dataset.id = item.id;
  li.tabIndex = 0;
  li.addEventListener('click', () => select(item.id, true));
  li.addEventListener('keydown', (event) => {
    if (event.target === li && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      select(item.id, true);
    }
  });
  return li;
}

function moreButton(item, entries) {
  const holder = el('div', 'row-actions');
  const button = el('button', 'icon-btn');
  button.type = 'button';
  button.setAttribute('aria-label', 'More actions');
  button.setAttribute('aria-haspopup', 'menu');
  button.appendChild(icon('more_vert'));
  button.addEventListener('click', (event) => {
    event.stopPropagation();
    select(item.id, false);
    showMenu(button, entries());
  });
  holder.appendChild(button);
  return holder;
}

function placeRow(item, number) {
  const li = baseRow(item, 'place');
  const rail = el('div', 'rail');
  rail.appendChild(el('span', 'pin', item.hidden ? '' : String(number)));

  const main = el('div', 'row-main');
  const unnamed = item.name_source === 'unknown';
  main.appendChild(el('div', `title-medium place-name${unnamed ? ' unnamed' : ''}`, item.name));
  const detail = item.address
    || { you: 'Named by you', 'your place': 'Your named place', google: 'Name from Google', unknown: 'No name recorded' }[item.name_source];
  main.appendChild(el('div', 'supporting', detail));
  if (item.hidden) {
    const badges = el('div', 'badges');
    const badge = el('span', 'badge');
    badge.append(icon('visibility_off'), 'Hidden');
    badges.appendChild(badge);
    main.appendChild(badges);
  }

  const side = el('div', 'row-side');
  side.appendChild(el('div', 'time', `${item.start_text} – ${item.end_text}`));
  side.appendChild(el('div', 'supporting', duration(item.start, item.end)));

  li.append(rail, main, side, moreButton(item, () => {
    if (item.hidden) return [['visibility', 'Show this place', () => apply({ action: 'show', item_id: item.id })]];
    const entries = [['edit', 'Rename', () => openRename(item)]];
    if (item.name_source === 'you') {
      entries.push(['undo', 'Use original name', () => apply({ action: 'rename', item_id: item.id, name: '' })]);
    }
    entries.push(null, ['visibility_off', 'Hide this place', () => apply({ action: 'hide', item_id: item.id })]);
    return entries;
  }));
  return li;
}

function travelRow(item) {
  const style = styleOf(item.mode);
  const li = baseRow(item, `travel${item.estimated ? ' estimated' : ''}`);
  li.style.setProperty('--mode-colour', style.colour);
  const rail = el('div', 'rail');
  rail.appendChild(icon(style.icon, 'mode-icon'));

  const main = el('div', 'row-main');
  const modeButton = el('button', 'mode-button');
  modeButton.type = 'button';
  modeButton.disabled = item.hidden;
  modeButton.setAttribute('aria-label', `Travel mode: ${item.mode_label}. Change`);
  modeButton.append(item.mode_label, icon('arrow_drop_down'));
  modeButton.addEventListener('click', (event) => {
    event.stopPropagation();
    select(item.id, false);
    showMenu(modeButton, modeEntries(item));
  });
  main.appendChild(modeButton);
  const how = { you: 'set by you', google: 'from Google', guess: 'guessed from speed' }[item.mode_source];
  main.appendChild(el('div', 'supporting', `${km(item.km)} · ${how}`));

  const badges = el('div', 'badges');
  const addBadge = (name, text, extra) => {
    const badge = el('span', `badge${extra ? ` ${extra}` : ''}`);
    badge.append(icon(name), text);
    badges.appendChild(badge);
  };
  if (item.snapped) addBadge('check', 'On roads', 'success');
  if (item.estimated) addBadge('warning', 'No readings');
  if (item.hidden) addBadge('visibility_off', 'Hidden');
  main.appendChild(badges);

  const side = el('div', 'row-side');
  side.appendChild(el('div', 'time', duration(item.start, item.end)));
  side.appendChild(el('div', 'supporting', item.start_text));

  li.append(rail, main, side, moreButton(item, () => {
    if (item.hidden) return [['visibility', 'Show this journey', () => apply({ action: 'show', item_id: item.id })]];
    const entries = [];
    if (item.snapped) {
      entries.push(['undo', 'Undo snap to roads', () => apply({ action: 'unsnap', item_id: item.id })]);
    } else if (!item.estimated && item.mode !== 'flying') {
      entries.push(['route', 'Snap to roads', () => snapOne(item)]);
    }
    entries.push(['edit', 'Change travel mode', () => showMenu(modeButton, modeEntries(item))]);
    if (item.mode_source === 'you') {
      entries.push(['undo', 'Use original mode', () => apply({ action: 'mode', item_id: item.id, mode: null })]);
    }
    entries.push(null, ['visibility_off', 'Hide this journey', () => apply({ action: 'hide', item_id: item.id })]);
    return entries;
  }));
  return li;
}

function modeEntries(item) {
  return Object.entries(day.modes).map(([code, label]) => [
    styleOf(code).icon, label,
    () => apply({ action: 'mode', item_id: item.id, mode: code }),
    code === item.mode,
  ]);
}

// ---------------------------------------------------------------------------
// Menus and the rename dialog
// ---------------------------------------------------------------------------

function showMenu(anchor, entries) {
  closeMenu();
  const menu = el('div', 'menu');
  menu.setAttribute('role', 'menu');
  for (const entry of entries) {
    if (!entry) {
      menu.appendChild(el('div', 'menu-divider'));
      continue;
    }
    const [name, label, action, current] = entry;
    const item = el('button', `menu-item${current ? ' current' : ''}`);
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    item.append(icon(name), label);
    item.addEventListener('click', (event) => {
      event.stopPropagation();
      closeMenu();
      action();
    });
    menu.appendChild(item);
  }
  document.body.appendChild(menu);
  const box = anchor.getBoundingClientRect();
  const height = menu.offsetHeight;
  const width = menu.offsetWidth;
  const top = box.bottom + height + 8 > window.innerHeight ? Math.max(8, box.top - height) : box.bottom;
  menu.style.top = `${top}px`;
  menu.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - width - 8))}px`;
  openMenu = menu;
  const first = menu.querySelector('.menu-item');
  if (first) first.focus();
}

function closeMenu() {
  if (openMenu) {
    openMenu.remove();
    openMenu = null;
  }
}

function openRename(item) {
  renaming = item;
  els.renameInput.value = item.name_source === 'unknown' ? '' : item.name;
  els.renameEverywhere.checked = false;
  els.dialog.showModal();
  els.renameInput.select();
}

els.renameForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const item = renaming;
  els.dialog.close();
  if (!item) return;
  apply({
    action: 'rename', item_id: item.id,
    name: els.renameInput.value, everywhere: els.renameEverywhere.checked,
  });
});
$('rename-cancel').addEventListener('click', () => els.dialog.close());

// ---------------------------------------------------------------------------
// Day-level actions
// ---------------------------------------------------------------------------

function snapOne(item) {
  return apply({ action: 'snap', item_id: item.id },
    'Downloading roads and matching this journey… The first time in an area can take a minute or two.');
}

async function snapAll() {
  const ids = snappable().map((i) => i.id);
  const problems = [];
  for (let n = 0; n < ids.length; n += 1) {
    select(ids[n], false);
    const result = await apply({ action: 'snap', item_id: ids[n] },
      `Snapping journey ${n + 1} of ${ids.length} to roads…`);
    if (!result) problems.push(`Journey ${n + 1}: ${els.snackbar.querySelector('.message').textContent}`);
  }
  if (problems.length) snack(problems.join(' '), true);
  else snack(`${ids.length} journey${ids.length === 1 ? '' : 's'} snapped to roads.`);
}

els.prev.addEventListener('click', () => day && day.previous && show(day.previous));
els.next.addEventListener('click', () => day && day.next && show(day.next));
els.dateButton.addEventListener('click', () => {
  if (typeof els.date.showPicker === 'function') {
    try { els.date.showPicker(); return; } catch (error) { /* fall through */ }
  }
  els.date.focus();
});
els.date.addEventListener('change', () => els.date.value && show(els.date.value));
els.snapAll.addEventListener('click', snapAll);
els.reset.addEventListener('click', () => {
  if (window.confirm('Undo every correction you made to this day? Names you gave to places everywhere are kept.')) {
    apply({ action: 'reset' });
  }
});
$('snackbar-close').addEventListener('click', () => snack(''));
document.addEventListener('click', (event) => {
  if (openMenu && !openMenu.contains(event.target)) closeMenu();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeMenu();
  const target = event.target;
  if (target instanceof HTMLInputElement || target instanceof HTMLSelectElement || els.dialog.open) return;
  if (openMenu) return;
  if (event.key === 'ArrowLeft' && day && day.previous) show(day.previous);
  if (event.key === 'ArrowRight' && day && day.next) show(day.next);
});
window.addEventListener('resize', closeMenu);
els.items.addEventListener('scroll', closeMenu);

(async function start() {
  const wanted = new URL(window.location.href).searchParams.get('date');
  if (wanted) {
    show(wanted);
    return;
  }
  setWorking(true);
  try {
    const response = await fetch('/api/summary');
    const summary = await response.json();
    if (!summary.days || !summary.days.length) {
      els.title.textContent = 'No data yet';
      snack('Nothing imported yet. Import an export with main.py first.', true);
      return;
    }
    show(summary.days[summary.days.length - 1]);
  } catch (error) {
    snack(error.message, true);
  } finally {
    setWorking(false);
  }
}());
