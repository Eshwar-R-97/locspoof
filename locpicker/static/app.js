/* Location Picker — browser UI. Plain JS, Leaflet 1.9, no build step.
   Coordinates are [lat, lon] everywhere except where OSRM is involved ([lon, lat]). */
(() => {
  "use strict";

  // ------------------------------------------------------------------ constants

  const SPEEDS = { walk: 1.4, bike: 5, drive: 11, highway: 27 };
  const ENGINE_PROFILE = { walk: "walk", bike: "bike", drive: "drive", highway: "drive", fly: "fly", custom: "custom" };
  const SLIDER_PROFILES = { fly: "flySpeed", custom: "customSpeed" }; // profiles whose speed comes from the slider
  const OSRM = {
    walk: "https://routing.openstreetmap.de/routed-foot/route/v1/foot/",
    bike: "https://routing.openstreetmap.de/routed-bike/route/v1/bike/",
    drive: "https://routing.openstreetmap.de/routed-car/route/v1/driving/",
  };
  const ROUTE_TIMEOUT_MS = 8000;
  const MAX_PATH = 20000;
  const NOMINATIM = "https://nominatim.openstreetmap.org/search";
  const MODE_LABEL = { idle: "Idle", static: "Holding", travel: "Travelling", wander: "Wandering", paused: "Paused" };
  const CONN_LABEL = { connected: "Connected", reconnecting: "Reconnecting", no_device: "No device", offline: "Server offline" };
  const PROFILE_NOUN = { walk: "on foot", bike: "by bike", drive: "by car", highway: "on the highway", fly: "by air", custom: "at custom speed" };

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // ------------------------------------------------------------------ token (read once from ?token=)

  let token = null;
  try {
    const url = new URL(location.href);
    const t = url.searchParams.get("token");
    if (t) {
      localStorage.setItem("locpicker.token", t);
      url.searchParams.delete("token");
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
    token = localStorage.getItem("locpicker.token");
  } catch (_) { /* storage blocked */ }

  const store = {
    get(k, d) { try { const v = localStorage.getItem("locpicker." + k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem("locpicker." + k, JSON.stringify(v)); } catch (_) {} },
  };

  // ------------------------------------------------------------------ state

  const S = {
    snap: null,
    spots: [],
    pin: null,            // [lat, lon] selected destination
    pinLabel: null,
    start: null,          // [lat, lon] explicit travel start when phone has no position
    startLabel: null,
    stops: [],            // [{latlng, label}] intermediate stops
    pickStart: false,
    tab: store.get("tab", "teleport"),
    profile: store.get("profile", "walk"),
    customSpeed: store.get("customSpeed", 8),
    flySpeed: store.get("flySpeed", 250),   // ≈ 560 mph, airliner cruise
    realistic: store.get("realistic", true),
    matchTime: store.get("matchTime", false),
    wanderOnArrival: store.get("wanderOnArrival", false),
    preview: null,        // {path, distance, duration, straight, key}
    previewSeq: 0,
    routeId: null,
    routePath: null,
    routeCum: null,
    activeSpot: null,
    lastSnapAt: 0,
  };

  // ------------------------------------------------------------------ API

  async function api(method, path, body) {
    const headers = {};
    if (token) headers.Authorization = "Bearer " + token;
    let payload;
    if (body instanceof FormData) payload = body;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    let r;
    try {
      r = await fetch(path, { method, headers, body: payload });
    } catch (e) {
      throw new Error("Can't reach the Location Picker server.");
    }
    let j = null;
    try { j = await r.json(); } catch (_) {}
    if (!r.ok) {
      const msg = (j && j.message) || `${r.status} ${r.statusText}`;
      const err = new Error(msg);
      err.code = j && j.error;
      err.status = r.status;
      throw err;
    }
    return j;
  }

  async function act(fn, okMsg) {
    try {
      const res = await fn();
      if (res && res.mode !== undefined) applySnap(res);
      if (okMsg) toast(okMsg, "ok");
      return res;
    } catch (e) {
      toast(e.message, "err");
      if (e.status === 401) toast("Open the URL printed in the terminal (it includes ?token=…).", "err");
      return null;
    }
  }

  // ------------------------------------------------------------------ toasts / dialog

  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "err" ? 6000 : 2800);
  }

  function confirmDialog(title, body, yes = "Confirm") {
    return new Promise((resolve) => {
      const d = $("#dialog");
      $("#dialog-title").textContent = title;
      $("#dialog-body").textContent = body;
      $("#dialog-yes").textContent = yes;
      d.hidden = false;
      $("#dialog-yes").focus();
      const done = (v) => { d.hidden = true; cleanup(); resolve(v); };
      const onYes = () => done(true), onNo = () => done(false);
      const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); done(false); } };
      const onBg = (e) => { if (e.target === d) done(false); };
      function cleanup() {
        $("#dialog-yes").removeEventListener("click", onYes);
        $("#dialog-no").removeEventListener("click", onNo);
        d.removeEventListener("keydown", onKey, true);
        d.removeEventListener("click", onBg);
      }
      $("#dialog-yes").addEventListener("click", onYes);
      $("#dialog-no").addEventListener("click", onNo);
      d.addEventListener("keydown", onKey, true);
      d.addEventListener("click", onBg);
    });
  }

  // ------------------------------------------------------------------ formatting

  const fmtCoord = (p) => p ? `${p[0].toFixed(6)}, ${p[1].toFixed(6)}` : "—";
  function fmtDist(m) {
    if (m == null) return "—";
    if (m < 1000) return `${Math.round(m)} m`;
    return `${(m / 1000).toFixed(m < 10000 ? 2 : 1)} km`;
  }
  function fmtDur(s) {
    if (s == null || !isFinite(s)) return "—";
    s = Math.round(s);
    if (s < 60) return `${s} s`;
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    if (h) return `${h} h ${m} min`;
    return `${m} min`;
  }
  const fmtSpeed = (v) => `${v.toFixed(1)} m/s · ${(v * 2.23694).toFixed(0)} mph`;

  // ------------------------------------------------------------------ geo (client side, for previews)

  const R = 6371008.8, rad = (d) => d * Math.PI / 180;
  function hav(a, b) {
    const dlat = rad(b[0] - a[0]), dlon = rad(b[1] - a[1]);
    const h = Math.sin(dlat / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dlon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function cumulative(path) {
    const c = [0];
    for (let i = 1; i < path.length; i++) c.push(c[i - 1] + hav(path[i - 1], path[i]));
    return c;
  }
  // Points along the great circle a→b (what the engine flies), so the preview arc matches reality.
  function greatCircle(a, b, n) {
    const [p1, l1, p2, l2] = [rad(a[0]), rad(a[1]), rad(b[0]), rad(b[1])];
    const d = hav(a, b) / R;
    if (d < 1e-9) return [a, b];
    const out = [];
    for (let i = 0; i <= n; i++) {
      const f = i / n, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
      const x = A * Math.cos(p1) * Math.cos(l1) + B * Math.cos(p2) * Math.cos(l2);
      const y = A * Math.cos(p1) * Math.sin(l1) + B * Math.cos(p2) * Math.sin(l2);
      const z = A * Math.sin(p1) + B * Math.sin(p2);
      out.push([Math.atan2(z, Math.hypot(x, y)) * 180 / Math.PI, Math.atan2(y, x) * 180 / Math.PI]);
    }
    return out;
  }
  function flightPath(wps) {
    const out = [wps[0]];
    for (let i = 1; i < wps.length; i++) {
      const n = Math.min(256, Math.max(1, Math.ceil(hav(wps[i - 1], wps[i]) / 5000)));
      out.push(...greatCircle(wps[i - 1], wps[i], n).slice(1));
    }
    return out;
  }

  function splitAt(path, cum, d) {
    if (!path || path.length < 2) return [path || [], []];
    if (d <= 0) return [[path[0]], path];
    if (d >= cum[cum.length - 1]) return [path, [path[path.length - 1]]];
    let lo = 0, hi = cum.length - 1;
    while (lo < hi - 1) { const mid = (lo + hi) >> 1; if (cum[mid] <= d) lo = mid; else hi = mid; }
    const t = (d - cum[lo]) / Math.max(1e-9, cum[hi] - cum[lo]);
    const p = [path[lo][0] + (path[hi][0] - path[lo][0]) * t, path[lo][1] + (path[hi][1] - path[lo][1]) * t];
    return [path.slice(0, lo + 1).concat([p]), [p].concat(path.slice(hi))];
  }
  function decimate(path, max) {
    if (path.length <= max) return path;
    const step = Math.ceil(path.length / (max - 1));
    const out = [];
    for (let i = 0; i < path.length; i += step) out.push(path[i]);
    if (out[out.length - 1] !== path[path.length - 1]) out.push(path[path.length - 1]);
    return out;
  }
  function parseCoords(text) {
    const m = text.trim().match(/^\s*\(?\s*(-?\d+(?:\.\d+)?)\s*[,\s;]\s*(-?\d+(?:\.\d+)?)\s*\)?\s*$/);
    if (!m) return null;
    const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
    return [lat, lon];
  }

  // ------------------------------------------------------------------ map

  const map = L.map("map", { zoomControl: false, worldCopyJump: true }).setView(store.get("view", [41.8781, -87.6298]), store.get("zoom", 13));
  L.control.zoom({ position: "bottomright" }).addTo(map);
  const dark = matchMedia("(prefers-color-scheme: dark)");
  // Standard OSM tiles (no key), desaturated in CSS (.leaflet-tile-pane) to match the monochrome UI.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors · Routing <a href="https://routing.openstreetmap.de/">FOSSGIS OSRM</a>',
  }).addTo(map);
  map.on("moveend", () => { store.set("view", [map.getCenter().lat, map.getCenter().lng]); store.set("zoom", map.getZoom()); });

  const pinIcon = (cls = "") => L.divIcon({ className: "pin-icon " + cls, html: '<div class="pin"></div>', iconSize: [30, 40], iconAnchor: [15, 40] });
  const phoneIcon = (stale) => L.divIcon({ className: "phone-icon" + (stale ? " stale" : ""), html: '<div class="halo"></div><div class="core"></div>', iconSize: [34, 34], iconAnchor: [17, 17] });

  const pinMarker = L.marker([0, 0], { icon: pinIcon(), draggable: true, keyboard: false, zIndexOffset: 500 });
  const startMarker = L.marker([0, 0], { icon: pinIcon("start"), draggable: true, keyboard: false, zIndexOffset: 400 });
  const phoneMarker = L.marker([0, 0], { icon: phoneIcon(false), interactive: false, keyboard: false, zIndexOffset: 1000 });
  let phoneStale = false;
  const stopLayer = L.layerGroup().addTo(map);
  const previewLine = L.polyline([], { color: getCss("--ink"), weight: 4, opacity: .8, dashArray: "2 9", lineCap: "round" });
  const doneLine = L.polyline([], { color: "#7a7a7a", weight: 5, opacity: .9, lineCap: "round" });
  const todoLine = L.polyline([], { color: getCss("--violet"), weight: 5, opacity: .95, lineCap: "round" });
  const wanderCircle = L.circle([0, 0], { radius: 20, color: getCss("--ink"), weight: 1.5, dashArray: "4 6", fillColor: "#FFF083", fillOpacity: .28 });

  function getCss(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || "#222"; }

  pinMarker.on("dragend", () => { const ll = pinMarker.getLatLng(); setPin([ll.lat, ll.lng], null, false); });
  startMarker.on("dragend", () => { const ll = startMarker.getLatLng(); setStart([ll.lat, ll.lng], "Dropped pin"); });

  map.on("click", (e) => {
    closeMenus();
    const p = [e.latlng.lat, wrapLon(e.latlng.lng)];
    if (S.pickStart) { S.pickStart = false; setStart(p, "Dropped pin"); renderTravel(); return; }
    setPin(p, null, false);
  });

  function wrapLon(lon) { return ((lon + 540) % 360) - 180; }

  function setPin(p, label, fly = true, spotId = null) {
    S.pin = p;
    S.pinLabel = label;
    S.activeSpot = spotId;
    pinMarker.setLatLng(p);
    if (!map.hasLayer(pinMarker)) pinMarker.addTo(map);
    if (fly) map.flyTo(p, Math.max(map.getZoom(), 15), { duration: .6 });
    renderPin();
    renderChips();
    schedulePreview();
  }

  function setStart(p, label) {
    S.start = p;
    S.startLabel = label;
    if (p) { startMarker.setLatLng(p); if (!map.hasLayer(startMarker)) startMarker.addTo(map); }
    else startMarker.remove();
    renderTravel();
    schedulePreview();
  }

  // ------------------------------------------------------------------ status rendering

  function applySnap(snap) {
    const prevRoute = S.snap && S.snap.travel && S.snap.travel.route_id;
    S.snap = snap;
    S.lastSnapAt = Date.now();
    renderStatus();
    renderLayers();
    renderTravel();
    renderWander();
    renderResume();
    const rid = snap.travel && snap.travel.route_id;
    if (rid && rid !== S.routeId) loadRoute(rid);
    if (!rid) { S.routeId = null; S.routePath = null; doneLine.remove(); todoLine.remove(); }
    if (prevRoute && !rid && snap.mode !== "travel") {/* finished */}
  }

  function renderStatus() {
    const s = S.snap;
    if (!s) return;
    const conn = $("#conn");
    const offline = Date.now() - S.lastSnapAt > 8000;
    const cs = offline ? "offline" : s.connection;
    conn.dataset.state = cs;
    $("#conn-label").textContent = CONN_LABEL[cs] || cs;
    conn.title = s.last_error || CONN_LABEL[cs] || "";

    const mc = $("#st-mode");
    mc.dataset.mode = s.mode;
    mc.textContent = MODE_LABEL[s.mode] || s.mode;
    $("#st-coords").textContent = s.position ? fmtCoord(s.position) : "Real GPS";

    const t = s.travel;
    $("#st-travel").hidden = !t;
    if (t) {
      $("#st-bar").style.width = (Math.min(1, t.progress) * 100).toFixed(2) + "%";
      $("#st-left").textContent = fmtDist(t.remaining_m);
      $("#st-eta").textContent = t.remaining_m <= 0 ? "arrived" : fmtDur(t.eta_s);
      let sp = fmtSpeed(t.current_speed || 0);
      if (s.mode === "paused") sp = "paused";
      else if (t.stop_reason === "traffic") sp = "stopped · traffic";
      else if (t.stop_reason === "pause") sp = "pausing";
      $("#st-speed").textContent = sp;
    }

    const extra = $("#st-extra");
    const bits = [];
    let err = false;
    if (s.last_error && s.connection !== "connected") { bits.push(s.last_error); err = true; }
    if (s.wander) bits.push(`Wandering within ${s.wander.radius_m} m`);
    if (s.auto_reset_in != null) bits.push(`Auto-reset in ${fmtDur(s.auto_reset_in)}`);
    extra.hidden = !bits.length;
    extra.textContent = bits.join(" · ");
    extra.classList.toggle("err", err);

    const d = s.device || {};
    $("#device").textContent = d.name ? `${d.name} · iOS ${d.ios} · ${d.backend}` : "";
    $("#reset").disabled = false;
  }

  function renderLayers() {
    const s = S.snap;
    if (s.position) {
      phoneMarker.setLatLng(s.position);
      if (!map.hasLayer(phoneMarker)) phoneMarker.addTo(map);
      const stale = s.connection !== "connected";
      if (stale !== phoneStale) { phoneStale = stale; phoneMarker.setIcon(phoneIcon(stale)); }
    } else {
      phoneMarker.remove();
    }
    if (s.wander) {
      wanderCircle.setLatLng(s.wander.center).setRadius(s.wander.radius_m);
      if (!map.hasLayer(wanderCircle)) wanderCircle.addTo(map);
    } else wanderCircle.remove();

    if (S.routePath && s.travel) {
      const [done, todo] = splitAt(S.routePath, S.routeCum, s.travel.traveled_m);
      doneLine.setLatLngs(done);
      todoLine.setLatLngs(todo);
      if (!map.hasLayer(doneLine)) doneLine.addTo(map);
      if (!map.hasLayer(todoLine)) todoLine.addTo(map);
      previewLine.remove();
    }
    if (s.position && S.start && s.mode !== "idle") setStart(null, null);
  }

  async function loadRoute(rid) {
    S.routeId = rid;
    try {
      const r = await api("GET", "/api/route");
      if (r.route_id !== rid || !r.path) return;
      S.routePath = r.path;
      S.routeCum = cumulative(r.path);
      renderLayers();
    } catch (e) { S.routeId = null; }
  }

  function renderResume() {
    const s = S.snap;
    const box = $("#resume");
    if (!s.resume_available || !s.previous_session) { box.hidden = true; return; }
    const p = s.previous_session;
    const what = { travel: "Travelling", wander: "Wandering", static: "Holding" }[p.mode] || p.mode;
    let txt = `${what} at ${fmtCoord(p.position)}`;
    if (p.remaining_m != null) txt += ` · ${fmtDist(p.remaining_m)} left`;
    $("#resume-text").textContent = txt;
    box.hidden = false;
  }

  // ------------------------------------------------------------------ live updates (SSE → polling fallback)

  let es = null, pollTimer = null, sseRetry = null;
  function connectSSE() {
    if (es) es.close();
    const url = "/api/events" + (token ? "?token=" + encodeURIComponent(token) : "");
    es = new EventSource(url);
    es.onmessage = (ev) => {
      try { applySnap(JSON.parse(ev.data)); } catch (_) {}
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    };
    es.onerror = () => {
      es.close(); es = null;
      if (!pollTimer) pollTimer = setInterval(poll, 2000);
      clearTimeout(sseRetry);
      sseRetry = setTimeout(connectSSE, 10000);
    };
  }
  async function poll() {
    try { applySnap(await api("GET", "/api/status")); }
    catch (_) { renderStatus(); }
  }
  setInterval(renderStatus, 3000); // flip to "server offline" if snapshots stop arriving

  // ------------------------------------------------------------------ tabs

  function setTab(t) {
    S.tab = t;
    store.set("tab", t);
    $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === t)));
    $$(".tab-pane").forEach((p) => (p.hidden = p.dataset.pane !== t));
    if (t === "travel") schedulePreview();
    else { previewLine.remove(); }
    renderStops();
  }
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

  // ------------------------------------------------------------------ teleport

  function renderPin() {
    $("#tp-pin").textContent = S.pin ? (S.pinLabel ? `${S.pinLabel} · ${fmtCoord(S.pin)}` : fmtCoord(S.pin)) : "Click the map, search, or pick a spot";
    $("#tp-go").disabled = !S.pin;
    $("#tr-add-stop").disabled = !S.pin;
    $("#chip-add") && ($("#chip-add").disabled = !S.pin);
  }
  $("#tp-wander").checked = store.get("tpWander", false);
  $("#tp-wander").addEventListener("change", (e) => store.set("tpWander", e.target.checked));

  async function teleportToPin() {
    if (!S.pin) { toast("Drop a pin first — click the map."); return; }
    await act(() => api("POST", "/api/teleport", { lat: S.pin[0], lon: S.pin[1], wander: $("#tp-wander").checked }),
      "Teleported");
  }
  $("#tp-go").addEventListener("click", teleportToPin);

  // ------------------------------------------------------------------ travel

  function routeOrigin() {
    if (S.snap && S.snap.position) return { p: S.snap.position, label: "Current position" };
    if (S.start) return { p: S.start, label: S.startLabel || "Start" };
    return null;
  }

  // Custom speed slider is logarithmic so 0.5–1000 m/s (≈1–2237 mph) stays usable at the low end.
  const CUSTOM_MIN = 0.5, CUSTOM_MAX = 1000;
  const sliderToSpeed = (x) => {
    const v = CUSTOM_MIN * Math.pow(CUSTOM_MAX / CUSTOM_MIN, x / 1000);
    return v < 10 ? Math.round(v * 10) / 10 : Math.round(v);
  };
  const speedToSlider = (v) =>
    Math.round(1000 * Math.log(Math.min(CUSTOM_MAX, Math.max(CUSTOM_MIN, v)) / CUSTOM_MIN) / Math.log(CUSTOM_MAX / CUSTOM_MIN));

  function travelSpeed() {
    if (SLIDER_PROFILES[S.profile]) return S[SLIDER_PROFILES[S.profile]];
    return SPEEDS[S.profile];
  }

  function effectiveSpeed() {
    let v = travelSpeed();
    if (S.matchTime && S.preview && !S.preview.straight && S.preview.duration > 0) {
      v = S.preview.distance / S.preview.duration;
    }
    return Math.min(1000, Math.max(0.1, v));
  }

  function renderStops() {
    const ol = $("#tr-stops");
    ol.textContent = "";
    stopLayer.clearLayers();
    const o = routeOrigin();
    const li = (cls, name, sub, onX) => {
      const el = document.createElement("li");
      el.className = cls;
      el.innerHTML = '<span class="stop-dot"></span><span class="stop-name"></span>';
      el.querySelector(".stop-name").textContent = name;
      if (sub) { const sm = document.createElement("small"); sm.textContent = "  " + sub; el.querySelector(".stop-name").appendChild(sm); }
      if (onX) { const b = document.createElement("button"); b.className = "x"; b.textContent = "✕"; b.title = "Remove stop"; b.onclick = onX; el.appendChild(b); }
      ol.appendChild(el);
    };
    li("from", o ? o.label : "Pick a start", o ? fmtCoord(o.p) : "", null);
    S.stops.forEach((st, i) => {
      li("", st.label || `Stop ${i + 1}`, fmtCoord(st.latlng), () => { S.stops.splice(i, 1); renderStops(); schedulePreview(); });
      if (S.tab === "travel") L.marker(st.latlng, { icon: pinIcon("stop"), keyboard: false }).addTo(stopLayer);
    });
    li("to", S.pin ? (S.pinLabel || "Pin") : "Pick a destination", S.pin ? fmtCoord(S.pin) : "", null);
  }

  $("#tr-add-stop").addEventListener("click", () => {
    if (!S.pin) return;
    S.stops.push({ latlng: S.pin, label: S.pinLabel || `Stop ${S.stops.length + 1}` });
    S.pin = null; S.pinLabel = null; pinMarker.remove();
    renderPin(); renderStops(); schedulePreview();
    toast("Stop added — now pick the next destination.");
  });

  function renderTravel() {
    const s = S.snap;
    const moving = s && (s.mode === "travel" || (s.mode === "paused" && s.paused_from === "travel"));
    const needStart = !(s && s.position);
    $("#tr-start").hidden = !needStart || moving;
    $("#tr-start-text").textContent = S.pickStart ? "Click the map to set the start…" : (S.start ? `${S.startLabel} · ${fmtCoord(S.start)}` : "The phone has no simulated position yet.");
    $("#tr-start-pick").textContent = S.pickStart ? "Cancel" : "Pick on map";

    $$("#tr-profiles button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.p === S.profile)));
    const sliderKey = SLIDER_PROFILES[S.profile];
    $("#tr-custom").hidden = !sliderKey;
    if (sliderKey) {
      $("#tr-speed").value = speedToSlider(S[sliderKey]);
      $("#tr-speed-val").textContent = fmtSpeed(+S[sliderKey]);
    }
    $("#tr-real").checked = S.realistic;
    $("#tr-match").checked = S.matchTime;
    $("#tr-wander").checked = S.wanderOnArrival;

    $("#tr-go").hidden = moving;
    $("#tr-pause").hidden = !moving;
    $("#tr-stop").hidden = !moving;
    $("#tr-pause").textContent = s && s.mode === "paused" ? "Resume" : "Pause";
    $("#tr-go").disabled = !(S.preview && S.preview.path && routeOrigin() && S.pin);
    renderStops();
    renderSummary();
  }

  function renderSummary() {
    const box = $("#tr-preview");
    const p = S.preview;
    if (!p || S.tab !== "travel") { box.hidden = true; return; }
    box.hidden = false;
    const sum = $("#tr-summary");
    if (p.loading) { sum.innerHTML = '<small>Finding a route…</small>'; $("#tr-route-warn").hidden = true; $("#tr-straight").hidden = true; return; }
    if (p.error) {
      sum.innerHTML = "<small>No route</small>";
      $("#tr-route-warn").textContent = p.error;
      $("#tr-route-warn").hidden = false;
      $("#tr-straight").hidden = false;
      return;
    }
    const v = effectiveSpeed();
    const t = p.distance / v;
    sum.textContent = `${fmtDist(p.distance)} · ${fmtDur(t)} `;
    const sm = document.createElement("small");
    sm.textContent = PROFILE_NOUN[S.profile] + (S.matchTime && !p.straight ? " (route's time)" : "");
    sum.appendChild(sm);
    $("#tr-route-warn").hidden = !p.straight;
    $("#tr-route-warn").textContent = p.fly ? "Flight path — flies the shortest great-circle line, ignoring roads."
      : p.straight ? "Straight line — ignores roads, buildings and water." : "";
    $("#tr-straight").hidden = true;
  }

  $$("#tr-profiles button").forEach((b) => b.addEventListener("click", () => {
    S.profile = b.dataset.p; store.set("profile", S.profile);
    renderTravel(); schedulePreview();
  }));
  $("#tr-speed").addEventListener("input", (e) => {
    const k = SLIDER_PROFILES[S.profile];
    if (!k) return;
    S[k] = sliderToSpeed(+e.target.value); store.set(k, S[k]); renderTravel();
  });
  $("#tr-real").addEventListener("change", (e) => { S.realistic = e.target.checked; store.set("realistic", S.realistic); });
  $("#tr-match").addEventListener("change", (e) => { S.matchTime = e.target.checked; store.set("matchTime", S.matchTime); renderSummary(); });
  $("#tr-wander").addEventListener("change", (e) => { S.wanderOnArrival = e.target.checked; store.set("wanderOnArrival", S.wanderOnArrival); });
  $("#tr-start-pick").addEventListener("click", () => { S.pickStart = !S.pickStart; renderTravel(); });
  $("#tr-start-spot").addEventListener("change", (e) => {
    const sp = S.spots.find((x) => x.id === e.target.value);
    if (sp) { setStart([sp.lat, sp.lon], sp.name); map.flyTo([sp.lat, sp.lon], Math.max(map.getZoom(), 14), { duration: .5 }); }
    e.target.value = "";
  });

  let previewTimer = null;
  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(buildPreview, 350);
  }

  function waypoints() {
    const o = routeOrigin();
    if (!o || !S.pin) return null;
    return [o.p, ...S.stops.map((s) => s.latlng), S.pin];
  }

  let routeAbort = null;
  async function buildPreview(forceStraight = false) {
    if (S.tab !== "travel") return;
    const s = S.snap;
    if (s && (s.mode === "travel" || (s.mode === "paused" && s.paused_from === "travel"))) { previewLine.remove(); return; }
    const wps = waypoints();
    if (!wps) { S.preview = null; previewLine.remove(); renderTravel(); return; }
    const fly = S.profile === "fly";
    const router = fly ? "fly" : S.profile === "walk" ? "walk" : S.profile === "bike" ? "bike" : "drive";
    const key = router + "|" + wps.map((p) => p[0].toFixed(6) + "," + p[1].toFixed(6)).join(";") + (forceStraight ? "|straight" : "");
    if (S.preview && S.preview.key === key && !S.preview.error && !S.preview.loading) { renderTravel(); return; }
    if (wps.some((p, i) => i && Math.abs(p[1] - wps[i - 1][1]) > 180)) {
      S.preview = { key, error: "Routes across the ±180° meridian aren't supported." };
      renderTravel(); return;
    }
    const seq = ++S.previewSeq;
    if (fly) {
      const path = flightPath(wps);
      setPreview({ key, path, distance: cumulative(path).pop(), duration: null, straight: true, fly: true });
      return;
    }
    if (forceStraight) {
      setPreview({ key, path: wps, distance: cumulative(wps).pop(), duration: null, straight: true });
      return;
    }
    S.preview = { key, loading: true };
    renderTravel();
    if (routeAbort) routeAbort.abort();
    routeAbort = new AbortController();
    const timer = setTimeout(() => routeAbort.abort(), ROUTE_TIMEOUT_MS);
    try {
      const coords = wps.map((p) => `${p[1].toFixed(6)},${p[0].toFixed(6)}`).join(";"); // OSRM wants lon,lat
      const url = `${OSRM[router]}${coords}?overview=full&geometries=geojson`;
      const r = await fetch(url, { signal: routeAbort.signal });
      if (!r.ok) throw new Error(`routing server returned ${r.status}`);
      const j = await r.json();
      if (j.code !== "Ok" || !j.routes || !j.routes.length) throw new Error(j.message || j.code || "no route");
      const route = j.routes[0];
      let path = route.geometry.coordinates.map(([lon, lat]) => [lat, lon]); // → [lat, lon]
      path = decimate(path, MAX_PATH);
      if (seq !== S.previewSeq) return;
      setPreview({ key, path, distance: route.distance, duration: route.duration, straight: false });
    } catch (e) {
      if (seq !== S.previewSeq) return;
      const why = e.name === "AbortError" ? "Routing timed out after 8 s." : `Routing failed: ${e.message}.`;
      S.preview = { key, error: why };
      previewLine.remove();
      renderTravel();
    } finally {
      clearTimeout(timer);
    }
  }

  function setPreview(p) {
    S.preview = p;
    previewLine.setLatLngs(p.path);
    previewLine.setStyle({ color: getCss("--ink") });
    if (!map.hasLayer(previewLine)) previewLine.addTo(map);
    const b = previewLine.getBounds();
    if (b.isValid() && !map.getBounds().contains(b)) map.fitBounds(b, { paddingTopLeft: [innerWidth > 720 ? 420 : 20, 40], paddingBottomRight: [40, innerWidth > 720 ? 40 : innerHeight * .45] });
    renderTravel();
  }

  $("#tr-straight").addEventListener("click", () => buildPreview(true));

  async function startTravel() {
    const p = S.preview;
    if (!p || !p.path) return;
    const body = {
      path: p.path,
      speed_mps: +effectiveSpeed().toFixed(3),
      profile: ENGINE_PROFILE[S.profile],
      wander_on_arrival: S.wanderOnArrival,
      realistic: S.realistic,
    };
    if (!(S.snap && S.snap.position)) {
      const st = routeOrigin();
      if (!st) { toast("Pick a start first."); return; }
      const ok = await act(() => api("POST", "/api/teleport", { lat: st.p[0], lon: st.p[1] }));
      if (!ok) return;
    }
    const res = await act(() => api("POST", "/api/travel", body), "On the way");
    if (res) {
      previewLine.remove();
      S.preview = null;
      S.stops = [];
      setStart(null, null);
      renderStops();
    }
  }
  $("#tr-go").addEventListener("click", startTravel);

  async function togglePause() {
    const s = S.snap;
    if (!s) return;
    if (s.mode === "paused") return act(() => api("POST", "/api/travel/resume"));
    if (s.mode === "travel" || s.mode === "wander") return act(() => api("POST", "/api/travel/pause"));
  }
  $("#tr-pause").addEventListener("click", togglePause);
  const stopMoving = () => act(() => api("POST", "/api/stop"), "Stopped — holding position");
  $("#tr-stop").addEventListener("click", stopMoving);

  // ------------------------------------------------------------------ wander

  let wanderPreset = store.get("wanderPreset", "default");
  const PRESET_RADIUS = { default: 20, indoors: 10, walking: 40, drift: 5 };
  if (!PRESET_RADIUS[wanderPreset]) wanderPreset = "default";
  $("#wd-radius").value = PRESET_RADIUS[wanderPreset];

  function renderWander() {
    const s = S.snap;
    const on = !!(s && (s.mode === "wander" || (s.mode === "paused" && s.paused_from === "wander")));
    const onArrival = !!(s && s.travel && s.travel.wander_on_arrival);
    $("#wd-on").checked = on || onArrival;
    if (s && s.wander) {
      wanderPreset = s.wander.preset;
      if (document.activeElement !== $("#wd-radius")) $("#wd-radius").value = s.wander.radius_m;
    }
    $$("#wd-presets button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.preset === wanderPreset)));
    $("#wd-radius-val").textContent = `${$("#wd-radius").value} m`;
  }

  async function sendWander(extra = {}) {
    const enabled = $("#wd-on").checked;
    if (enabled && !(S.snap && S.snap.position)) {
      $("#wd-on").checked = false;
      toast("Teleport somewhere first, then turn on wander.", "err");
      return;
    }
    await act(() => api("POST", "/api/wander", { enabled, preset: wanderPreset, ...extra }));
  }
  $("#wd-on").addEventListener("change", () => sendWander({ radius_m: +$("#wd-radius").value }));
  $$("#wd-presets button").forEach((b) => b.addEventListener("click", () => {
    const preset = b.dataset.preset, radius = PRESET_RADIUS[preset];
    wanderPreset = preset; store.set("wanderPreset", preset);
    $("#wd-radius").value = radius;
    $("#wd-radius-val").textContent = `${radius} m`;
    $$("#wd-presets button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    // Send the chosen values explicitly; renderWander() would re-read the (stale) snapshot.
    if ($("#wd-on").checked) sendWander({ preset, radius_m: radius });
  }));
  let radiusTimer = null;
  $("#wd-radius").addEventListener("input", () => {
    $("#wd-radius-val").textContent = `${$("#wd-radius").value} m`;
    if (S.snap && S.snap.wander) wanderCircle.setRadius(+$("#wd-radius").value);
    clearTimeout(radiusTimer);
    radiusTimer = setTimeout(() => { if ($("#wd-on").checked) sendWander({ radius_m: +$("#wd-radius").value }); }, 300);
  });

  // ------------------------------------------------------------------ spots

  async function loadSpots() {
    try {
      const r = await api("GET", "/api/spots");
      S.spots = r.spots;
      if (r.warning) { $("#warn").textContent = r.warning; $("#warn").hidden = false; }
      renderChips();
    } catch (e) { toast(e.message, "err"); }
  }

  function renderChips() {
    const box = $("#chips");
    box.textContent = "";
    for (const sp of S.spots) {
      const chip = document.createElement("div");
      chip.className = "chip" + (S.activeSpot === sp.id ? " active" : "");
      chip.dataset.id = sp.id;
      const main = document.createElement("button");
      main.className = "chip-main";
      main.textContent = (sp.emoji ? sp.emoji + " " : "") + sp.name;
      main.title = `${sp.name} · ${fmtCoord([sp.lat, sp.lon])}`;
      main.onclick = () => setPin([sp.lat, sp.lon], sp.name, true, sp.id);
      const more = document.createElement("button");
      more.className = "chip-more";
      more.textContent = "⋯";
      more.setAttribute("aria-label", `More actions for ${sp.name}`);
      more.onclick = (e) => { e.stopPropagation(); openSpotMenu(sp, more); };
      chip.append(main, more);
      box.appendChild(chip);
    }
    const add = document.createElement("button");
    add.id = "chip-add";
    add.className = "chip-add";
    add.textContent = "＋ Save pin";
    add.disabled = !S.pin;
    add.title = S.pin ? "Save the current pin" : "Drop a pin first";
    add.onclick = openSaveForm;
    box.appendChild(add);

    const sel = $("#tr-start-spot");
    sel.length = 1;
    for (const sp of S.spots) sel.add(new Option((sp.emoji ? sp.emoji + " " : "") + sp.name, sp.id));
  }

  function openSaveForm() {
    if (!S.pin) return;
    $("#sp-form").hidden = false;
    $("#sp-name").value = S.pinLabel && !S.activeSpot ? S.pinLabel.split(",")[0].slice(0, 40) : "";
    $("#sp-emoji").value = "";
    $("#sp-name").focus();
  }
  $("#sp-cancel").addEventListener("click", () => ($("#sp-form").hidden = true));
  $("#sp-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!S.pin) return;
    const res = await act(() => api("POST", "/api/spots", {
      name: $("#sp-name").value, lat: S.pin[0], lon: S.pin[1], emoji: $("#sp-emoji").value || null,
    }), "Saved");
    if (res) {
      $("#sp-form").hidden = true;
      S.spots.push(res);
      S.activeSpot = res.id;
      S.pinLabel = res.name;
      renderChips(); renderPin();
    }
  });

  let menuSpot = null;
  function openSpotMenu(sp, anchor) {
    menuSpot = sp;
    const m = $("#spot-menu");
    m.hidden = false;
    const r = anchor.getBoundingClientRect();
    const mw = m.offsetWidth, mh = m.offsetHeight;
    m.style.left = Math.min(innerWidth - mw - 8, Math.max(8, r.left)) + "px";
    m.style.top = (r.bottom + mh + 8 > innerHeight ? r.top - mh - 6 : r.bottom + 6) + "px";
    $("[data-act=move]", m).disabled = !S.pin;
    m.querySelector("button").focus();
  }
  function closeMenus() {
    $("#spot-menu").hidden = true;
    $("#results").hidden = true;
  }
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#spot-menu") && !e.target.closest(".chip-more")) $("#spot-menu").hidden = true;
    if (!e.target.closest(".search")) $("#results").hidden = true;
  });

  $("#spot-menu").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b || !menuSpot) return;
    const sp = menuSpot;
    $("#spot-menu").hidden = true;
    switch (b.dataset.act) {
      case "rename": return inlineRename(sp);
      case "move": {
        if (!S.pin) return;
        const r = await act(() => api("PATCH", `/api/spots/${sp.id}`, { lat: S.pin[0], lon: S.pin[1] }), `Moved “${sp.name}” to the pin`);
        if (r) { Object.assign(sp, r); renderChips(); }
        return;
      }
      case "start":
        setTab("travel");
        if (S.snap && S.snap.position) {
          if (await confirmDialog("Start from this spot?", `The phone is already at a simulated position. Teleport to “${sp.name}” first, then plan the route from there.`, "Teleport there")) {
            await act(() => api("POST", "/api/teleport", { lat: sp.lat, lon: sp.lon }), `At ${sp.name}`);
          }
        } else setStart([sp.lat, sp.lon], sp.name);
        return;
      case "delete": {
        if (!(await confirmDialog(`Delete “${sp.name}”?`, "This can't be undone (unless you exported your spots).", "Delete"))) return;
        const r = await act(() => api("DELETE", `/api/spots/${sp.id}`), "Deleted");
        if (r) { S.spots = S.spots.filter((x) => x.id !== sp.id); if (S.activeSpot === sp.id) S.activeSpot = null; renderChips(); }
      }
    }
  });

  function inlineRename(sp) {
    const chip = $(`.chip[data-id="${sp.id}"]`);
    if (!chip) return;
    chip.textContent = "";
    const inp = document.createElement("input");
    inp.className = "chip-rename";
    inp.value = sp.name;
    inp.maxLength = 40;
    chip.appendChild(inp);
    inp.focus(); inp.select();
    let done = false;
    const finish = async (save) => {
      if (done) return; done = true;
      const name = inp.value.trim();
      if (save && name && name !== sp.name) {
        const r = await act(() => api("PATCH", `/api/spots/${sp.id}`, { name }), "Renamed");
        if (r) { Object.assign(sp, r); if (S.activeSpot === sp.id) S.pinLabel = r.name; renderPin(); }
      }
      renderChips();
    };
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") { e.stopPropagation(); finish(false); }
    });
    inp.addEventListener("blur", () => finish(true));
  }

  // export needs the token when one is set (download links can't send headers)
  $("#sp-export").addEventListener("click", (e) => {
    if (token) $("#sp-export").href = "/api/spots/export?token=" + encodeURIComponent(token);
  });
  $("#sp-import").addEventListener("click", () => $("#sp-import-file").click());
  $("#sp-import-file").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    const replace = S.spots.length > 0 && await confirmDialog("Import spots",
      "Replace all your current spots with the file's spots? Choose Cancel to merge instead (names that already exist are skipped).", "Replace");
    const fd = new FormData();
    fd.append("file", f);
    const r = await act(() => api("POST", "/api/spots/import" + (replace ? "?mode=replace" : ""), fd));
    if (r) { S.spots = r.spots; renderChips(); toast(`Imported ${r.added} spot${r.added === 1 ? "" : "s"}${r.skipped ? `, skipped ${r.skipped}` : ""}`, "ok"); }
  });

  // ------------------------------------------------------------------ search (Nominatim, ≤1 req/s)

  let lastSearch = 0, searchSeq = 0, results = [], resultIdx = -1;
  const input = $("#search");

  async function runSearch() {
    const q = input.value.trim();
    if (!q) return;
    const c = parseCoords(q);
    if (c) { setPin(c, null, true); $("#results").hidden = true; input.blur(); return; }
    const wait = Math.max(0, 1000 - (Date.now() - lastSearch));
    const seq = ++searchSeq;
    if (wait) await new Promise((r) => setTimeout(r, wait));
    if (seq !== searchSeq) return;
    lastSearch = Date.now();
    showResults(null);
    try {
      const b = map.getBounds();
      const url = `${NOMINATIM}?format=jsonv2&limit=6&addressdetails=0&q=${encodeURIComponent(q)}` +
        `&viewbox=${b.getWest()},${b.getNorth()},${b.getEast()},${b.getSouth()}`;
      const r = await fetch(url, { headers: { "Accept-Language": navigator.language || "en" }, referrerPolicy: "strict-origin-when-cross-origin" });
      if (!r.ok) throw new Error(`search returned ${r.status}`);
      const j = await r.json();
      if (seq !== searchSeq) return;
      showResults(j);
    } catch (e) {
      if (seq === searchSeq) showResults([], e.message);
    }
  }

  function showResults(list, err) {
    const ul = $("#results");
    ul.textContent = "";
    ul.hidden = false;
    results = list || [];
    resultIdx = -1;
    if (list === null) { ul.innerHTML = '<li class="empty">Searching…</li>'; return; }
    if (!results.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = err ? `Search failed: ${err}` : "No results"; ul.appendChild(li); return; }
    results.forEach((r, i) => {
      const li = document.createElement("li");
      li.setAttribute("role", "option");
      const parts = r.display_name.split(", ");
      li.textContent = parts[0];
      const sm = document.createElement("small");
      sm.textContent = parts.slice(1).join(", ");
      li.appendChild(sm);
      li.onclick = () => pickResult(i);
      ul.appendChild(li);
    });
  }
  function pickResult(i) {
    const r = results[i];
    if (!r) return;
    setPin([+r.lat, +r.lon], r.display_name.split(", ")[0], true);
    $("#results").hidden = true;
    input.blur();
  }
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); if (resultIdx >= 0 && !$("#results").hidden) pickResult(resultIdx); else runSearch(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if ($("#results").hidden || !results.length) return;
      e.preventDefault();
      resultIdx = (resultIdx + (e.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
      $$("#results li").forEach((li, i) => li.setAttribute("aria-selected", String(i === resultIdx)));
    } else if (e.key === "Escape") { $("#results").hidden = true; input.blur(); e.stopPropagation(); }
  });
  input.addEventListener("paste", () => setTimeout(() => { const c = parseCoords(input.value); if (c) runSearch(); }, 0));

  // ------------------------------------------------------------------ reset, resume, sheet

  async function resetReal(ask = true) {
    if (ask && !(await confirmDialog("Reset to real location?", "Stops all simulation. Your phone goes back to its real GPS position.", "Reset"))) return;
    const r = await act(() => api("POST", "/api/clear"), "Real location restored");
    if (r) { S.preview = null; previewLine.remove(); }
  }
  $("#reset").addEventListener("click", () => resetReal(false));
  $("#resume-yes").addEventListener("click", () => act(() => api("POST", "/api/session/resume"), "Session resumed"));
  $("#resume-no").addEventListener("click", () => act(() => api("POST", "/api/session/discard")));

  $("#sheet-handle").addEventListener("click", () => {
    const p = $("#panel");
    p.classList.toggle("collapsed");
    $("#sheet-handle").setAttribute("aria-expanded", String(!p.classList.contains("collapsed")));
    $("#sheet-handle").setAttribute("aria-label", p.classList.contains("collapsed") ? "Expand panel" : "Collapse panel");
  });

  // ------------------------------------------------------------------ keyboard

  document.addEventListener("keydown", (e) => {
    if (!$("#dialog").hidden) return;
    const t = e.target;
    if (t.closest && t.closest("input, select, textarea, [contenteditable]")) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape") {
      if (!$("#spot-menu").hidden || !$("#results").hidden) { closeMenus(); return; }
      if (S.pickStart) { S.pickStart = false; renderTravel(); return; }
      const m = S.snap && S.snap.mode;
      if (m === "travel" || m === "wander" || m === "paused") { e.preventDefault(); stopMoving(); }
    } else if (e.key === "t" || e.key === "T") { e.preventDefault(); teleportToPin(); }
    else if (e.key === " ") {
      if (t.closest && t.closest("button, a")) return;
      e.preventDefault(); togglePause();
    } else if (e.key === "r" || e.key === "R") { e.preventDefault(); resetReal(true); }
    else if (e.key === "/") { e.preventDefault(); input.focus(); }
  });

  // ------------------------------------------------------------------ boot

  setTab(S.tab);
  renderPin();
  renderTravel();
  poll().then(() => {
    const s = S.snap;
    if (s && s.position && !store.get("view", null)) map.setView(s.position, 15);
  });
  connectSSE();
  loadSpots();
})();
