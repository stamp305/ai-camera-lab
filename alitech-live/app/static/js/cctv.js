// Security > CCTV Streaming. Live and playback share one view: layout 1x1 / 2x2 / 4x4 (after
// Feedboard, MIT) with pages, click a channel to enlarge it (full picture), ⛶ for full screen.
// Playback plays every channel of the page from the same moment on one timeline.
// Filters like Alitech's Zone & Behavior page: regions many, branch one (with search).
// Every stream is asked for through this site's server, which checks the viewer and writes
// the audit; the browser only gets one WHEP URL + token per camera, valid for that session.
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const csrf = $('meta[name="csrf-token"]').content;
  const ERR = {
    branch_offline: "สาขาออฟไลน์", branch_timeout: "สาขาไม่ตอบ", too_many_streams: "เกินจำนวนที่สาขาส่งได้พร้อมกัน",
    too_many_main: "ภาพชัดเปิดพร้อมกันได้ไม่เกิน 2 จอ", too_many_playback: "ย้อนหลังแบบ 2×2 เปิดพร้อมกันได้ไม่เกิน 4 จอ",
    unknown_camera: "ไม่พบกล้องนี้", no_ffmpeg: "เครื่องสาขายังไม่มีตัวส่งภาพ", start_failed: "เริ่มส่งภาพไม่ได้",
    media_server_down: "เซิร์ฟเวอร์ภาพไม่ทำงาน", bad_time: "ช่วงเวลาไม่ถูกต้อง", nvr_unreachable: "ติดต่อเครื่องบันทึกไม่ได้",
    nvr_refused: "เครื่องบันทึกปฏิเสธ", whep_401: "ไม่มีสิทธิ์", timeout: "ไม่มีสัญญาณจากกล้องช่องนี้", demo_branch: "สาขาตัวอย่าง ไม่มีภาพ",
    no_clip: "ไม่มีคลิปของช่องนี้ในช่วงเวลานี้",
  };
  const say = (c) => ERR[c] || c || "ผิดพลาด";
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hms = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const NO_REGION = "ไม่ระบุภาค";

  const S = { tab: "live", branches: [], regions: new Set(), branch: "", cameras: [], selected: new Set(), grid: 2, page: 0, demo: false, focus: null };
  const live = [];          // [{sid, client, tile, timer}]
  const pb = [];
  let liveTimer = null;

  const key = (c) => `${c.camera}:${c.channel}`;
  const camOf = (k) => S.cameras.find((c) => key(c) === k);
  const camName = (c) => `CH ${c.channel}` + (c.nvr ? ` · ${c.nvr}` : "");
  const regionOf = (b) => b.region || NO_REGION;
  const branchOf = (id) => S.branches.find((b) => b.client_id === id);
  const branchName = (b) => (b ? `${b.name}${b.province ? " · จ." + b.province : ""}` : "");
  const inRegions = (b) => !S.regions.size || S.regions.has(regionOf(b));

  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: "same-origin", cache: "no-store", ...opts,
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, ...(opts.headers || {}) } });
    if (res.status === 401) { location.href = "/login"; throw new Error("signed_out"); }
    let body = {};
    try { body = await res.json(); } catch (_) {}
    if (!res.ok && !body.error) body.error = `http_${res.status}`;
    return body;
  }

  // ---- filters (top bar) ----------------------------------------------------------------
  const bar = $("[data-filters]");
  const fRegion = Filters.create(bar, { key: "region", name: "ภาค", icon: "region" });
  const fBranch = Filters.create(bar, { key: "branch", name: "สาขา", icon: "branch" });
  const fCams = Filters.create(bar, { key: "camera", name: "กล้อง", icon: "camera" });
  const allRegions = () => [...new Set(S.branches.map(regionOf))].sort((a, b) => a.localeCompare(b, "th"));

  fRegion.build = (menu) => Filters.checkMenu(menu, {
    heading: "เลือกภาค", allText: "ทุกภาค", selected: S.regions.size ? S.regions : new Set(allRegions()),
    options: allRegions().map((r) => ({ value: r, text: r, small: `${S.branches.filter((b) => regionOf(b) === r).length} สาขา` })),
    onApply: (picked) => {
      S.regions = new Set(picked.length === allRegions().length ? [] : picked);
      fRegion.close();
      const cur = branchOf(S.branch);
      if (cur && !inRegions(cur)) { const first = S.branches.find(inRegions); if (first) selectBranch(first.client_id); }
      renderFilterLabels();
    } });
  fBranch.build = (menu) => Filters.radioMenu(menu, {
    heading: "เลือกสาขา", current: S.branch, search: "ค้นหาสาขาหรือจังหวัด",
    options: S.branches.filter(inRegions).map((b) => ({ value: b.client_id, text: b.name, small: (b.demo ? "ตัวอย่าง · " : "") + (b.province ? "จ." + b.province : ""),
                                                     search: `${b.name} ${b.province} ${b.region}` })),
    onPick: (v) => { fBranch.close(); selectBranch(v); } });
  fCams.build = (menu) => Filters.checkMenu(menu, {
    heading: "เลือกกล้อง", allText: "ทุกกล้อง", selected: S.selected,
    options: S.cameras.map((c) => ({ value: key(c), text: camName(c), small: c.active ? "ใช้วิเคราะห์" : "" })),
    onApply: (picked) => { S.selected = new Set(picked); S.page = 0; S.focus = null; fCams.close(); renderFilterLabels(); refresh(); } });

  function renderFilterLabels() {
    fRegion.setValue(S.regions.size ? (S.regions.size === 1 ? [...S.regions][0] : `${S.regions.size} ภาค`) : "ทุกภาค");
    fBranch.setValue(branchName(branchOf(S.branch)) || "เลือกสาขา");
    fCams.setValue(S.cameras.length ? `${S.selected.size}/${S.cameras.length}` : "-");
  }

  async function loadBranches() {
    const body = await api("/api/live/branches");
    S.branches = (body.branches || []).sort((a, b) => (a.demo === b.demo ? a.name.localeCompare(b.name, "th") : a.demo ? 1 : -1));
    let saved = "";
    try { saved = localStorage.getItem("cctv-branch") || ""; } catch (_) {}
    renderFilterLabels();
    const linked = LINK && S.branches.find((b) => b.client_id === LINK.branch);
    if (LINK && !linked) { history.replaceState(null, "", location.pathname); }
    const pick = linked || S.branches.find((b) => b.client_id === saved) || S.branches[0];
    if (linked) setTab("playback", { draw: false });
    if (pick) { await selectBranch(pick.client_id); if (linked) openLink(); }
    else viewEl().replaceChildren(note("ไม่มีสาขาที่ออนไลน์และรองรับการดูกล้อง"));
  }

  async function selectBranch(id) {
    stopAll();
    S.branch = id;
    try { localStorage.setItem("cctv-branch", id); } catch (_) {}
    S.cameras = []; S.selected = new Set(); S.page = 0; S.focus = null;
    S.demo = !!(branchOf(id) || {}).demo;
    P.days.clear(); T.clear();
    renderFilterLabels();
    drawTiles([]);
    const body = await api(`/api/live/${encodeURIComponent(id)}/cameras`);
    if (S.branch !== id) return;
    if (!body.ok) { viewEl().replaceChildren(note(say(body.error))); return; }
    S.cameras = body.cameras || [];
    S.selected = new Set(S.cameras.map(key));      // every camera the branch's NVR has
    renderFilterLabels();
    refresh();
  }

  // ---- the shared view: which cameras, how many columns ---------------------------------
  const viewEl = () => (S.tab === "live" ? $("[data-grid-view]") : $("[data-pb-view]"));
  const cols = () => (S.focus ? 1 : S.grid);

  function viewCams() {
    if (S.focus) { const c = camOf(S.focus); return { cams: c ? [c] : [], pages: 1 }; }
    const sel = S.cameras.filter((c) => S.selected.has(key(c)));
    const per = S.grid * S.grid;
    const pages = Math.max(1, Math.ceil(sel.length / per));
    S.page = Math.min(S.page, pages - 1);
    return { cams: sel.slice(S.page * per, S.page * per + per), pages };
  }

  function updatePager(pages) {
    const pager = $("[data-pager]");
    pager.hidden = pages <= 1 || !!S.focus;
    $("[data-page-info]").textContent = `${S.page + 1} / ${pages}`;
    $$("[data-page]").forEach((b) => (b.disabled = (b.dataset.page === "-1" && S.page === 0) || (b.dataset.page === "1" && S.page >= pages - 1)));
  }

  // Redraw for the current tab after a change of cameras / layout / page / enlarge.
  function refresh() {
    if (S.tab === "live") scheduleLive();
    else renderPlayback();
    requestAnimationFrame(fit);
  }

  // ---- tiles ----------------------------------------------------------------------------
  function note(text) { const d = document.createElement("div"); d.className = "cam-note"; d.textContent = text; return d; }

  // Live view: the zones the branch's analytics draw on this channel (In-Pass lines, Hot Zone
  // areas, ...), with their names. Points are 0..1 of the picture; the SVG uses the video's own
  // size with "meet", so it lines up with the picture exactly as object-fit: contain shows it.
  const SVGNS = "http://www.w3.org/2000/svg";
  const HEX = /^#[0-9a-fA-F]{6}$/;
  // Same look as the branch's own Display page: closed shape, colour at 1/3 opacity, white
  // corner dots, white label in the middle with a dark outline. Dots and text stay a fixed
  // size on screen whatever the tile size (the SVG scales with the picture).
  function zoneLayer(cam, video) {
    const zones = cam && Array.isArray(cam.zones) ? cam.zones : [];
    if (S.tab !== "live" || !zones.length) return null;
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("class", "cam-zones");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    let w = 1600, h = 900;
    const dots = [], labels = [];
    const resize = () => {
      const scale = Math.min(svg.clientWidth / w, svg.clientHeight / h) || 1;
      for (const c of dots) c.setAttribute("r", String(4 / scale));
      for (const t of labels) { t.setAttribute("font-size", String(13 / scale)); t.style.strokeWidth = `${3 / scale}px`; }
    };
    const el = (tag, attrs) => {
      const n = document.createElementNS(SVGNS, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
      return n;
    };
    const draw = () => {
      svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
      svg.replaceChildren();
      dots.length = 0; labels.length = 0;
      for (const z of zones) {
        const pts = (z.points || []).filter((p) => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite)).map(([x, y]) => [x * w, y * h]);
        if (pts.length < 2) continue;
        const color = HEX.test(z.color || "") ? z.color : "#f57c00";
        const g = el("g", {});
        g.append(el("path", {
          d: pts.map((p, i) => `${i ? "L" : "M"}${p[0]} ${p[1]}`).join(" ") + " Z",
          fill: color + "55", stroke: color, "stroke-width": 2.5, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke",
        }));
        for (const p of pts) {
          const c = el("circle", { cx: p[0], cy: p[1], r: 4, fill: "#fff", stroke: color, "stroke-width": 2, "vector-effect": "non-scaling-stroke" });
          dots.push(c); g.append(c);
        }
        const label = String(z.label || "").slice(0, 60);
        if (label && pts.length >= 3) {
          const t = el("text", {
            x: pts.reduce((s2, p) => s2 + p[0], 0) / pts.length, y: pts.reduce((s2, p) => s2 + p[1], 0) / pts.length,
            "text-anchor": "middle", "dominant-baseline": "central", fill: "#fff", "font-size": 13, class: "cam-zone-label",
          });
          t.textContent = label;
          labels.push(t); g.append(t);
        }
        svg.append(g);
      }
      resize();
    };
    draw();
    video.addEventListener("resize", () => {
      if (video.videoWidth && video.videoHeight && (video.videoWidth !== w || video.videoHeight !== h)) { w = video.videoWidth; h = video.videoHeight; draw(); }
    });
    if (window.ResizeObserver) new ResizeObserver(resize).observe(svg);
    return svg;
  }

  function makeTile(cam, text, busy) {
    const tile = document.createElement("div");
    tile.className = "cam-tile" + (cam ? "" : " is-empty");
    const video = Object.assign(document.createElement("video"), { muted: true, autoplay: true, playsInline: true });
    const freeze = document.createElement("canvas"); freeze.className = "cam-freeze"; freeze.hidden = true;
    const name = document.createElement("span"); name.className = "cam-tile-name"; name.textContent = cam ? camName(cam) : "";
    const state = document.createElement("span"); state.className = "cam-tile-state";
    const spin = Spinner.make("lg"); spin.classList.add("cam-spinner");
    const msg = document.createElement("span"); msg.className = "cam-tile-msg";
    state.append(spin, msg);
    const stats = document.createElement("span"); stats.className = "cam-tile-stats";
    tile.append(video, freeze, name, state, stats);
    const zl = zoneLayer(cam, video);
    if (zl) tile.insertBefore(zl, name);
    if (cam) {
      tile.dataset.key = key(cam);
      const tools = document.createElement("span"); tools.className = "cam-tile-tools";
      const fs = Object.assign(document.createElement("button"), { type: "button", className: "cam-tool", title: "เต็มจอ", textContent: "⛶" });
      fs.addEventListener("click", (e) => { e.stopPropagation(); fullScreen(key(cam)); });
      // In a grid, clicking the picture enlarges that channel and clicking again shrinks it
      // back (no icon for it). 1x1 already shows one channel big: nothing to click there.
      if (!(S.grid === 1 && !S.focus)) {
        tile.addEventListener("click", () => toggleFocus(key(cam)));
        tile.title = S.focus ? "กดที่ภาพเพื่อย่อกลับ (Esc)" : "กดที่ภาพเพื่อขยาย";
      } else {
        tile.classList.add("is-single");
      }
      tools.append(fs);
      tile.append(tools);
    }
    setState(tile, text || "", !!busy);
    return tile;
  }

  function setState(tile, text, busy) {
    $(".cam-tile-msg", tile).textContent = text;
    tile.classList.toggle("is-busy", !!busy);
    $(".cam-tile-state", tile).hidden = !text && !busy;
  }

  // Keep the last picture on screen while a stream is replaced (seek, enlarge, page).
  function freezeFrame(tile) {
    const video = $("video", tile), canvas = $(".cam-freeze", tile);
    if (!video || !video.videoWidth) return;
    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    try { canvas.getContext("2d").drawImage(video, 0, 0); canvas.hidden = false; } catch (_) {}
  }

  function drawTiles(cams, text, busy) {
    const view = viewEl();
    view.style.setProperty("--cols", String(cols()));
    view.classList.toggle("is-focus", !!S.focus);
    view.replaceChildren();
    const tiles = new Map();
    for (const c of cams) { const t = makeTile(c, text, busy); tiles.set(key(c), t); view.append(t); }
    for (let i = cams.length; i < cols() * cols(); i++) view.append(makeTile(null));
    return tiles;
  }

  function toggleFocus(k) {
    S.focus = S.focus === k ? null : k;
    refresh();
  }

  // Full screen needs the click itself, so it is asked for before the streams change.
  function fullScreen(k) {
    const target = S.tab === "live" ? $(".cam-stage") : $(".cam-player");
    if (document.fullscreenElement) { document.exitFullscreen().catch(() => {}); return; }
    if (target.requestFullscreen) target.requestFullscreen().catch(() => {});
    if (cols() !== 1 && S.focus !== k) { S.focus = k; refresh(); }   // from a grid: that channel only
  }

  async function connect(stream, tile, holder) {
    const video = $("video", tile);
    const entry = { sid: stream.session, client: null, tile, timer: null, onplaying: null };
    holder.push(entry);
    const deadline = Date.now() + 20000;      // a channel with no camera fails on the branch within seconds
    while (Date.now() < deadline && holder.includes(entry)) {
      const client = new WhepClient(stream.whep_url, stream.token);
      try {
        const media = await client.connect();
        if (!holder.includes(entry)) { client.close(); return entry; }
        entry.client = client;
        video.srcObject = media;
        // autoplay alone is not always honoured (e.g. after following a link): ask for it
        video.play().catch(() => {});
        client.onstate = (s) => { if (s === "failed" || s === "disconnected") setState(tile, "สัญญาณหลุด", false); };
        video.addEventListener("playing", () => {
          setState(tile, "", false); $(".cam-freeze", tile).hidden = true;
          tile.classList.add("is-live"); entry.onplaying && entry.onplaying();
        }, { once: true });
        entry.timer = setInterval(() => stats(client, tile), 2000);
        return entry;
      } catch (err) {
        client.close();
        if (err.message !== "whep_404") { setState(tile, say(err.message), false); return entry; }
        setState(tile, "รอภาพจากสาขา…", true);
        await new Promise((r) => setTimeout(r, 1200));
      }
    }
    if (holder.includes(entry) && !entry.client) setState(tile, say(S.tab === "playback" ? "no_clip" : "timeout"), false);
    return entry;
  }

  const last = new WeakMap();
  async function stats(client, tile) {
    const report = await client.pc?.getStats();
    if (!report) return;
    report.forEach((s) => {
      if (s.type !== "inbound-rtp" || s.kind !== "video") return;
      const p = last.get(tile); last.set(tile, { b: s.bytesReceived, t: s.timestamp });
      const kbps = p ? Math.round(((s.bytesReceived - p.b) * 8) / (s.timestamp - p.t)) : 0;
      $(".cam-tile-stats", tile).textContent = `${s.frameWidth || "?"}×${s.frameHeight || "?"} · ${Math.round(s.framesPerSecond || 0)}fps · ${kbps} kbps`;
    });
  }

  // Ends sessions without making the page wait (the server stops them in parallel).
  function stopList(holder) {
    const sids = holder.map((e) => e.sid).filter(Boolean);
    holder.splice(0).forEach((e) => { clearInterval(e.timer); e.client?.close(); });
    if (sids.length) api("/api/live/stop", { method: "POST", body: JSON.stringify({ sessions: sids }) }).catch(() => {});
  }
  function stopAll() { clearTimeout(liveTimer); stopList(live); pbHalt(); stopList(pb); }

  // ---- live -----------------------------------------------------------------------------
  function scheduleLive() { clearTimeout(liveTimer); liveTimer = setTimeout(runLive, 200); }

  async function runLive() {
    if (S.tab !== "live" || !S.branch) return;
    stopList(live);
    const { cams, pages } = viewCams();
    updatePager(pages);
    const tiles = drawTiles(cams, S.demo ? say("demo_branch") : "กำลังขอภาพ…", !S.demo);
    requestAnimationFrame(fit);
    if (!cams.length) { viewEl().prepend(note("ยังไม่ได้เลือกกล้อง")); return; }
    if (S.demo) return;
    const ctx = `${S.branch}|${S.grid}|${S.page}|${S.focus}`;
    const subtype = cols() === 1 ? 0 : 1;          // one channel on screen = the full picture
    const body = await api(`/api/live/${encodeURIComponent(S.branch)}/start`, { method: "POST",
      body: JSON.stringify({ mode: "live", items: cams.map((c) => ({ camera: c.camera, channel: c.channel, subtype })) }) });
    if (S.tab !== "live" || ctx !== `${S.branch}|${S.grid}|${S.page}|${S.focus}`) {
      if (body.ok) api("/api/live/stop", { method: "POST", body: JSON.stringify({ sessions: body.streams.filter((s) => s.ok).map((s) => s.session) }) });
      return;
    }
    if (!body.ok) { tiles.forEach((t) => setState(t, say(body.error), false)); return; }
    for (const s of body.streams) {
      const tile = tiles.get(`${s.camera}:${s.channel}`);
      if (!tile) continue;
      if (!s.ok) { setState(tile, say(s.error), false); continue; }
      if (subtype === 0) $(".cam-tile-name", tile).textContent += " · ภาพชัด";
      connect(s, tile, live);
    }
  }

  $$("[data-grid]").forEach((b) => b.addEventListener("click", () => {
    S.grid = Number(b.dataset.grid); S.page = 0; S.focus = null;
    $$("[data-grid]").forEach((x) => x.classList.toggle("is-on", x === b));
    refresh();
  }));
  $$("[data-page]").forEach((b) => b.addEventListener("click", () => { S.page += Number(b.dataset.page); S.focus = null; refresh(); }));

  // ---- playback -------------------------------------------------------------------------
  // Every channel has its own timeline (T): its own ▶/⏸ and seek bar on the tile, so channels
  // can be watched at different times. The bar under the grid moves all of them together.
  const P = { date: "", startSec: 0, endSec: 0, pos: 0, days: new Map(), month: null };
  const T = new Map();          // camera key -> {pos, playing, run, entry, playAt, posAt}
  let tick = null;
  const el = { form: $("[data-pb-form]"), start: $("[data-pb-start]"), end: $("[data-pb-end]"),
    play: $("[data-pb-play]"), stop: $("[data-pb-stop]"), seek: $("[data-pb-seek]"), clock: $("[data-pb-clock]"),
    endclock: $("[data-pb-endclock]"), dateBtn: $("[data-date-button]"), dateLabel: $("[data-date-label]"), cal: $("[data-calendar]"),
    dateFilter: $("[data-date-filter]") };
  const secToClock = (s) => `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(Math.floor(s % 60))}`;
  const clockToSec = (v) => { const [h, m, s] = (v || "0:0:0").split(":").map(Number); return h * 3600 + m * 60 + (s || 0); };
  const ready = () => P.endSec > P.startSec && !!P.date;
  const quality = () => (cols() === 1 ? "full" : S.grid === 2 ? "grid" : "still");
  const clamp = (s) => Math.min(Math.max(s, P.startSec), P.endSec);
  const tl = (k) => { if (!T.has(k)) T.set(k, { pos: P.startSec, playing: false, run: 0, entry: null, playAt: 0, posAt: 0 }); return T.get(k); };
  const tileOf = (k) => $$(".cam-tile[data-key]", viewEl()).find((t) => t.dataset.key === k);
  const onScreen = () => viewCams().cams.map(key);

  // The per-channel bar on a tile: ▶/⏸, time, seek. Clicks here never enlarge the tile.
  function addTileBar(tile, k) {
    if (!tile || $(".cam-tbar", tile)) return;
    const barEl = document.createElement("div"); barEl.className = "cam-tbar";
    const btn = Object.assign(document.createElement("button"), { type: "button", className: "cam-tbtn", title: "เล่น / หยุดชั่วคราว (ช่องนี้)" });
    const time = document.createElement("span"); time.className = "cam-ttime";
    const rng = Object.assign(document.createElement("input"), { type: "range", className: "cam-seek cam-tseek", min: "0", step: "1" });
    rng.setAttribute("aria-label", "ตำแหน่งเวลาของช่องนี้");
    const track = document.createElement("div"); track.className = "cam-track cam-ttrack";
    const spans = document.createElement("div"); spans.className = "cam-spans";
    track.append(spans, rng);
    barEl.append(btn, time, track);
    barEl.addEventListener("click", (e) => e.stopPropagation());
    btn.addEventListener("click", () => { const t = tl(k); t.playing ? pauseTile(k) : playTiles([k], t.pos >= P.endSec ? P.startSec : t.pos); });
    rng.addEventListener("input", () => { tl(k).dragging = true; time.textContent = secToClock(P.startSec + Number(rng.value)); });
    rng.addEventListener("change", () => {
      const t = tl(k); t.dragging = false;
      const target = clamp(P.startSec + Number(rng.value));
      if (t.playing) playTiles([k], target); else { t.pos = target; paintTile(k); }
    });
    tile.append(barEl);
    paintTile(k);
  }

  function paintTile(k) {
    const tile = tileOf(k); if (!tile) return;
    const t = tl(k), barEl = $(".cam-tbar", tile); if (!barEl) return;
    const ok = ready() && !S.demo;
    barEl.hidden = !ok;
    $(".cam-tbtn", barEl).textContent = t.playing ? "⏸" : "▶";
    if (!t.dragging) {
      const rng = $(".cam-tseek", barEl);
      rng.max = String(Math.max(0, P.endSec - P.startSec));
      rng.value = String(Math.max(0, t.pos - P.startSec));
      rng.style.setProperty("--pct", Number(rng.max) > 0 ? `${(Number(rng.value) / Number(rng.max)) * 100}%` : "0%");
      $(".cam-ttime", barEl).textContent = secToClock(t.pos);
    }
  }

  // The bar under the grid: the first channel on screen's time; ▶ when any channel plays.
  function setControls() {
    const ok = ready() && !S.demo;
    [el.play, el.stop, el.seek, ...$$("[data-skip]")].forEach((b) => (b.disabled = !ok));
    const keys = onScreen();
    const playing = keys.some((k) => tl(k).playing);
    if (keys.length && !el.seek.matches(":active")) P.pos = tl(keys[0]).pos;
    el.play.classList.toggle("is-playing", playing);
    el.play.setAttribute("aria-label", playing ? "หยุดชั่วคราวทุกช่อง" : "เล่นทุกช่อง");
    el.seek.max = String(Math.max(0, P.endSec - P.startSec));
    el.seek.value = String(Math.max(0, P.pos - P.startSec));
    // channels at different times (moved one by one): say so instead of showing one of them
    const times = keys.map((k) => tl(k).pos);
    const apart = ok && keys.length > 1 && Math.max(...times) - Math.min(...times) > 5;   // a few s of start-up lag is not "apart"
    $("[data-pb-label]").hidden = cols() === 1;
    $("[data-pb-sync]").hidden = !apart;
    el.clock.classList.toggle("is-apart", apart);
    el.clock.textContent = !ok ? "--:--:--" : apart ? "หลายเวลา" : secToClock(P.pos);
    el.endclock.textContent = ok ? secToClock(P.endSec) : "--:--:--";
    el.seek.style.setProperty("--pct", Number(el.seek.max) > 0 && !apart ? `${(Number(el.seek.value) / Number(el.seek.max)) * 100}%` : "0%");
    el.seek.classList.toggle("is-apart", apart);
  }

  // "⇆ ให้ทุกช่องเวลาเดียวกัน": every channel on screen to the first channel's time
  $("[data-pb-sync]").addEventListener("click", () => {
    const keys = onScreen();
    if (!keys.length) return;
    const target = Math.round(tl(keys[0]).pos);
    if (keys.some((k) => tl(k).playing)) playTiles(keys, target);
    else { keys.forEach((k) => { tl(k).pos = target; paintTile(k); }); setControls(); }
  });

  function ensureTick() {
    if (tick) return;
    tick = setInterval(() => {
      const now = performance.now();
      for (const [k, t] of T) {
        if (!t.playing || !t.playAt) continue;
        t.pos = Math.min(P.endSec, t.posAt + (now - t.playAt) / 1000);
        if (t.pos >= P.endSec) pauseTile(k); else paintTile(k);
      }
      setControls();
    }, 250);
  }

  // Close a channel's stream. silent: do not tell the server (the caller does, and waits).
  function stopEntry(t, silent) {
    const e = t.entry; t.entry = null;
    if (!e) return null;
    clearInterval(e.timer); e.client?.close();
    const i = pb.indexOf(e); if (i >= 0) pb.splice(i, 1);
    if (e.sid && !silent) api("/api/live/stop", { method: "POST", body: JSON.stringify({ sessions: [e.sid] }) }).catch(() => {});
    return e.sid || null;
  }

  // Pause keeps the last picture on the tile (not a black box) and drops the "live" marks.
  function pauseTile(k) {
    const t = tl(k), tile = tileOf(k);
    if (tile) { freezeFrame(tile); tile.classList.remove("is-live"); $(".cam-tile-stats", tile).textContent = ""; }
    t.playing = false; t.playAt = 0; t.run += 1; stopEntry(t); paintTile(k); setControls();
  }
  function pbHalt() { for (const k of T.keys()) { const t = T.get(k); t.playing = false; t.playAt = 0; t.run += 1; stopEntry(t); } }

  // Start the given channels at `pos` (one request), each then runs on its own clock.
  async function playTiles(keys, pos) {
    if (!ready() || S.demo || S.tab !== "playback" || !keys.length) return;
    pos = clamp(pos);
    if (pos >= P.endSec) return;
    const q = quality();
    const runs = new Map();
    const old = [];
    for (const k of keys) {
      const t = tl(k), tile = tileOf(k);
      t.run += 1; runs.set(k, t.run);
      if (tile) freezeFrame(tile);
      const sid = stopEntry(t, true);
      if (sid) old.push(sid);
      t.pos = pos; t.playing = true; t.playAt = 0;
      if (tile) {
        setState(tile, "กำลังโหลดภาพย้อนหลัง…", true);
        $(".cam-tile-name", tile).textContent = q === "still" ? `CH ${camOf(k).channel}` : camName(camOf(k)) + (q === "full" ? " · ภาพชัด" : "");
        paintTile(k);
      }
    }
    setControls();
    ensureTick();
    // The old streams must be gone first: the branch counts them against its limit (2x2 = 4),
    // and the stop reaches it ahead of the start on the same link.
    if (old.length) await api("/api/live/stop", { method: "POST", body: JSON.stringify({ sessions: old }) }).catch(() => {});
    const cams = keys.map(camOf).filter(Boolean);
    const body = await api(`/api/live/${encodeURIComponent(S.branch)}/start`, { method: "POST",
      body: JSON.stringify({ mode: "playback", quality: q, items: cams.map((c) => ({ camera: c.camera, channel: c.channel, subtype: 0 })),
                             start: `${P.date}T${secToClock(pos)}`, end: `${P.date}T${secToClock(P.endSec)}` }) });
    for (const s of body.streams || []) {
      const k = `${s.camera}:${s.channel}`, t = tl(k), tile = tileOf(k);
      if (runs.get(k) !== t.run || !tile) { if (s.ok) api("/api/live/stop", { method: "POST", body: JSON.stringify({ sessions: [s.session] }) }); continue; }
      if (!s.ok) { setState(tile, say(s.error), false); t.playing = false; paintTile(k); continue; }
      connect(s, tile, pb).then((entry) => {
        if (runs.get(k) !== t.run) { stopEntry({ entry }); return; }
        t.entry = entry;
        entry.onplaying = () => { if (runs.get(k) === t.run) { t.playAt = performance.now(); t.posAt = t.pos; } };
      });
    }
    if (!body.ok) keys.forEach((k) => { const tile = tileOf(k); if (tile) setState(tile, say(body.error), false); tl(k).playing = false; paintTile(k); });
    setControls();
  }

  // ---- recorded spans: green on the seek bars where the NVR has video -------------------
  const SP = new Map();              // `${branch}|${key}|${date}` -> [[startSec, endSec], ...]
  const spKey = (k) => `${S.branch}|${k}|${P.date}`;

  async function loadSpans(keys) {
    if (!ready() || S.demo) return;
    const todo = keys.filter((k) => !SP.has(spKey(k)));
    todo.forEach((k) => SP.set(spKey(k), null));
    await Promise.all(todo.map(async (k) => {
      const id = spKey(k), c = camOf(k);
      if (!c) return;
      const b = await api(`/api/live/${encodeURIComponent(S.branch)}/record-spans?camera=${c.camera}&channel=${c.channel}&day=${P.date}`).catch(() => ({}));
      SP.set(id, b.ok ? b.spans || [] : []);
      paintSpans();
    }));
  }

  function spansGradient(spans) {
    const w = P.endSec - P.startSec;
    if (w <= 0 || !spans) return "none";
    const stops = [];
    for (const [a, b] of spans) {
      const x = Math.max(a, P.startSec), y = Math.min(b, P.endSec);
      if (y <= x) continue;
      const l = (((x - P.startSec) / w) * 100).toFixed(3), r = (((y - P.startSec) / w) * 100).toFixed(3);
      stops.push(`transparent ${l}%, var(--span) ${l}%, var(--span) ${r}%, transparent ${r}%`);
    }
    return stops.length ? `linear-gradient(to right, ${stops.join(", ")})` : "none";
  }

  function unionSpans(lists) {
    const all = lists.flat().sort((p, q) => p[0] - q[0]), out = [];
    for (const [a, b] of all) { if (out.length && a <= out[out.length - 1][1]) out[out.length - 1][1] = Math.max(out[out.length - 1][1], b); else out.push([a, b]); }
    return out;
  }

  function paintSpans() {
    const keys = onScreen();
    keys.forEach((k) => {
      const tile = tileOf(k), el2 = tile && $(".cam-spans", tile);
      if (el2) el2.style.backgroundImage = spansGradient(SP.get(spKey(k)));
    });
    const lists = keys.map((k) => SP.get(spKey(k))).filter(Boolean);
    $("[data-spans]").style.backgroundImage = lists.length ? spansGradient(unionSpans(lists)) : "none";
  }

  // Draw the playback view; channels that were playing carry on from their own time.
  async function renderPlayback() {
    const { cams, pages } = viewCams();
    updatePager(pages);
    const msg = S.demo ? say("demo_branch") : (ready() ? "" : "เลือกวันที่และช่วงเวลา แล้วกด เปิดดู");
    const view = viewEl();
    const same = view.classList.contains("is-focus") === !!S.focus && view.style.getPropertyValue("--cols") === String(cols())
      && cams.length && cams.every((c, i) => view.children[i] && view.children[i].dataset.key === key(c));
    const keys = cams.map(key);
    const wasPlaying = [...T.values()].some((t) => t.playing);
    const groupPos = P.pos;
    if (!same) {
      // New layout / page / enlarge: every old tile is gone, so every old stream goes too (the
      // quality follows the layout). Wait for the branch to drop them before asking again —
      // it counts them against its limits (2x2 = 4, full picture = 2).
      const old = [];
      for (const t of T.values()) { const sid = stopEntry(t, true); if (sid) old.push(sid); }
      drawTiles(cams, msg, false);
      if (!cams.length && S.cameras.length) view.prepend(note("ยังไม่ได้เลือกกล้อง"));
      // if the clip was playing, what is now on screen carries on from the same moment
      if (wasPlaying) keys.forEach((k) => { const t = tl(k); if (!t.playing) { t.playing = true; t.pos = groupPos; } });
      if (old.length) await api("/api/live/stop", { method: "POST", body: JSON.stringify({ sessions: old }) }).catch(() => {});
    } else {
      for (const [k, t] of T) if (!keys.includes(k) && t.entry) stopEntry(t);     // left the screen
    }
    keys.forEach((k) => addTileBar(tileOf(k), k));
    if (ready() && !S.demo) {
      keys.forEach((k) => { const t = tl(k); if (!t.playing) setState(tileOf(k), t.pos > P.startSec ? "หยุดชั่วคราว" : "กด ▶ เพื่อเล่น", false); });
      const groups = new Map();      // restart playing channels, grouped by time (one request each)
      keys.filter((k) => tl(k).playing && !tl(k).entry).forEach((k) => {
        const p = Math.round(tl(k).pos); groups.set(p, [...(groups.get(p) || []), k]);
      });
      groups.forEach((ks, p) => playTiles(ks, p));
      paintSpans();
      loadSpans(keys);
    } else {
      $("[data-spans]").style.backgroundImage = "none";
    }
    setControls();
  }

  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (S.demo) return;
    if (!P.date) { openCalendar(); return; }
    const a = clockToSec(el.start.value), b = clockToSec(el.end.value);
    if (!(b > a)) { el.end.setCustomValidity("เวลาหยุดต้องหลังเวลาเริ่ม"); el.end.reportValidity(); return; }
    el.end.setCustomValidity("");
    pbHalt(); T.clear();
    P.startSec = a; P.endSec = b; P.pos = a;
    renderPlayback();
    playTiles(onScreen(), a);
  });
  el.end.addEventListener("input", () => el.end.setCustomValidity(""));
  // the group bar: all channels on screen together
  el.play.addEventListener("click", () => {
    const keys = onScreen();
    if (keys.some((k) => tl(k).playing)) { keys.forEach(pauseTile); return; }
    const groups = new Map();
    keys.forEach((k) => { const p = tl(k).pos >= P.endSec ? P.startSec : Math.round(tl(k).pos); groups.set(p, [...(groups.get(p) || []), k]); });
    groups.forEach((ks, p) => playTiles(ks, p));
  });
  el.stop.addEventListener("click", () => {
    onScreen().forEach((k) => { pauseTile(k); tl(k).pos = P.startSec; paintTile(k); const tile = tileOf(k); if (tile) setState(tile, "กด ▶ เพื่อเล่น", false); });
    setControls();
  });
  el.seek.addEventListener("input", () => {
    el.clock.textContent = secToClock(P.startSec + Number(el.seek.value));
    el.seek.style.setProperty("--pct", `${(Number(el.seek.value) / Number(el.seek.max)) * 100}%`);
  });
  el.seek.addEventListener("change", () => {
    const target = clamp(P.startSec + Number(el.seek.value)), keys = onScreen();
    if (keys.some((k) => tl(k).playing)) playTiles(keys, target);
    else { keys.forEach((k) => { tl(k).pos = target; paintTile(k); }); setControls(); }
  });
  $$("[data-skip]").forEach((b) => b.addEventListener("click", () => {
    const d = Number(b.dataset.skip), keys = onScreen();
    const groups = new Map();
    keys.forEach((k) => {
      const t = tl(k), target = Math.round(clamp(t.pos + d));
      if (t.playing) groups.set(target, [...(groups.get(target) || []), k]); else { t.pos = target; paintTile(k); }
    });
    groups.forEach((ks, p) => playTiles(ks, p));
    setControls();
  }));

  // Picking a day fills the whole day: 00:00:00 to the latest moment of it — now for today,
  // 23:59:59 for a day that is over. The times stay editable.
  function setDayRange(iso) {
    const now = new Date();
    el.start.value = "00:00:00";
    el.end.value = iso === ymd(now) ? hms(now) : "23:59:59";
    el.end.setCustomValidity("");
  }

  // calendar: days with no video on the NVR are faded and cannot be picked (checked on the
  // first camera on screen; an NVR records its channels together)
  const TH_MONTH = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน", "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"];
  function openCalendar() {
    Filters.closeAll();
    const d = P.date ? new Date(P.date + "T00:00:00") : new Date();
    P.month = new Date(d.getFullYear(), d.getMonth(), 1);
    el.cal.hidden = false;
    requestAnimationFrame(() => el.dateFilter.classList.add("is-open"));
    el.dateBtn.setAttribute("aria-expanded", "true");
    drawCalendar();
  }
  function closeCalendar() {
    el.dateFilter.classList.remove("is-open");
    el.dateBtn.setAttribute("aria-expanded", "false");
    setTimeout(() => { if (!el.dateFilter.classList.contains("is-open")) el.cal.hidden = true; }, 180);
  }
  async function drawCalendar() {
    const m = P.month;
    const monthKey = `${m.getFullYear()}-${pad(m.getMonth() + 1)}`;
    const cam = viewCams().cams[0];
    el.cal.replaceChildren();
    const head = document.createElement("div"); head.className = "cam-cal-head";
    const prev = Object.assign(document.createElement("button"), { type: "button", className: "cam-cal-nav", textContent: "‹", title: "เดือนก่อน" });
    const next = Object.assign(document.createElement("button"), { type: "button", className: "cam-cal-nav", textContent: "›", title: "เดือนถัดไป" });
    const ttl = document.createElement("strong"); ttl.textContent = `${TH_MONTH[m.getMonth()]} ${m.getFullYear() + 543}`;
    const today = new Date();
    next.disabled = m.getFullYear() === today.getFullYear() && m.getMonth() >= today.getMonth();
    prev.addEventListener("click", () => { P.month = new Date(m.getFullYear(), m.getMonth() - 1, 1); drawCalendar(); });
    next.addEventListener("click", () => { P.month = new Date(m.getFullYear(), m.getMonth() + 1, 1); drawCalendar(); });
    head.append(prev, ttl, next);
    const grid = document.createElement("div"); grid.className = "cam-cal-grid";
    for (const w of ["อา", "จ", "อ", "พ", "พฤ", "ศ", "ส"]) { const s = document.createElement("span"); s.className = "cam-cal-wd"; s.textContent = w; grid.append(s); }
    const status = document.createElement("p"); status.className = "analytics-menu-note cam-cal-status";
    el.cal.append(head, grid, status);
    if (!cam) { status.textContent = "เลือกกล้องก่อน"; return; }
    if (S.demo) status.textContent = say("demo_branch");
    const dayKey = `${S.branch}|${key(cam)}|${monthKey}`;
    let days = S.demo ? [] : P.days.get(dayKey);
    const lead = new Date(m.getFullYear(), m.getMonth(), 1).getDay();
    const count = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
    const paint = () => {
      $$(".cam-cal-day, .cam-cal-pad", grid).forEach((n) => n.remove());
      for (let i = 0; i < lead; i++) { const s = document.createElement("span"); s.className = "cam-cal-pad"; grid.append(s); }
      for (let d = 1; d <= count; d++) {
        const iso = `${monthKey}-${pad(d)}`;
        const b = Object.assign(document.createElement("button"), { type: "button", className: "cam-cal-day", textContent: String(d) });
        const has = !!days && days.includes(iso);
        b.disabled = !has;
        b.classList.toggle("is-selected", iso === P.date);
        b.classList.toggle("is-today", iso === ymd(today));
        b.title = has ? "มีคลิป" : (days ? "ไม่มีคลิป" : "กำลังตรวจ…");
        b.addEventListener("click", () => {
          P.date = iso;
          el.dateLabel.textContent = new Date(iso + "T00:00:00").toLocaleDateString("th-TH", { day: "numeric", month: "short", year: "numeric" });
          setDayRange(iso);
          closeCalendar();
        });
        grid.append(b);
      }
    };
    paint();
    if (!days) {
      status.replaceChildren(Spinner.make("sm"), document.createTextNode(" กำลังถามเครื่องบันทึกว่าวันไหนมีคลิป…"));
      const body = await api(`/api/live/${encodeURIComponent(S.branch)}/record-days?camera=${cam.camera}&channel=${cam.channel}&month=${monthKey}`);
      if (P.month !== m) return;
      if (!body.ok) { status.textContent = say(body.error); return; }
      days = body.days || [];
      P.days.set(dayKey, days);
      paint();
    }
    if (!S.demo) status.textContent = days.length ? `มีคลิป ${days.length} วันในเดือนนี้ · วันที่สีจาง = ไม่มีคลิป` : "เดือนนี้ไม่มีคลิป";
  }
  el.dateBtn.addEventListener("click", (e) => { e.stopPropagation(); el.cal.hidden ? openCalendar() : closeCalendar(); });
  el.cal.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => { if (!el.cal.hidden) closeCalendar(); });

  // ---- tabs / keys / leave --------------------------------------------------------------
  // The tab changes at once; the old streams are ended in the background.
  function setTab(name, { draw = true } = {}) {
    if (S.tab === name) return;
    stopAll();
    S.tab = name; S.focus = null;
    $$("[data-tab]").forEach((x) => { const on = x.dataset.tab === name; x.classList.toggle("is-active", on); x.setAttribute("aria-selected", String(on)); });
    $$("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== S.tab));
    $$("[data-pb-only]").forEach((n) => (n.hidden = S.tab !== "playback"));
    if (draw) refresh();
  }
  $$("[data-tab]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

  // Opened from an alert: ?branch=<id>&ch=<channel>&at=YYYY-MM-DDTHH:MM -> playback of that
  // channel, enlarged, from one minute before up to ten minutes after (or now).
  const LINK = (() => {
    const q = new URLSearchParams(location.search);
    const b = q.get("branch") || "", ch = Number(q.get("ch")), at = q.get("at") || "";
    return /^[A-Za-z0-9-]{4,40}$/.test(b) && Number.isInteger(ch) && ch > 0 && ch <= 256 && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(at)
      ? { branch: b, ch, at, aid: /^[a-z]{2}\d{3}$/.test(q.get("aid") || "") ? q.get("aid") : "" } : null;
  })();

  function openLink() {
    if (!LINK) return;
    history.replaceState(null, "", location.pathname);     // a reload shows the page, not the jump again
    const cam = S.cameras.find((c) => c.channel === LINK.ch);
    if (!cam) { viewEl().replaceChildren(note(`ไม่พบกล้องช่อง ${LINK.ch} ในสาขานี้`)); return; }
    const day = LINK.at.slice(0, 10), now = new Date();
    const t = clockToSec(LINK.at.slice(11) + ":00");
    const last = day === ymd(now) ? clockToSec(hms(now)) : 86399;
    // 1x1 on that channel (its page), so the view has only full screen, no enlarge
    S.grid = 1; S.focus = null;
    $$("[data-grid]").forEach((x) => x.classList.toggle("is-on", x.dataset.grid === "1"));
    const sel = S.cameras.filter((c) => S.selected.has(key(c)));
    if (!sel.includes(cam)) { S.selected.add(key(cam)); renderFilterLabels(); }
    S.page = Math.max(0, S.cameras.filter((c) => S.selected.has(key(c))).indexOf(cam));
    P.date = day;
    el.dateLabel.textContent = new Date(day + "T00:00:00").toLocaleDateString("th-TH", { day: "numeric", month: "short", year: "numeric" });
    P.startSec = Math.max(0, t - 60); P.endSec = Math.max(P.startSec + 60, Math.min(last, t + 10 * 60));
    el.start.value = secToClock(P.startSec); el.end.value = secToClock(P.endSec);
    pbHalt(); T.clear();
    renderPlayback();
    playTiles([key(cam)], P.startSec);
    if (LINK.aid) showAlert(LINK.aid);
  }

  // The alert this clip was opened from: its message above the player, and the way back to
  // the same tab with the same alert open.
  async function showAlert(aid) {
    const barEl = $("[data-alertbar]");
    let row = null;
    try { const b = await api(`/api/alerts/item/${aid}`); row = b.ok ? b.row : null; } catch (_) {}
    if (!row) return;
    $("[data-alert-title]").textContent = row.kind === "intrusion" ? "🚨 พบคนในร้านหลังเวลาปิด" : `⚠️ ${row.detail}`;
    // one line (title · camera · time); the branch and the full message open on request
    $("[data-alert-meta]").textContent = `${row.camera} · ${row.at}`;
    $("[data-alert-branch]").textContent = row.branch;
    $("[data-alert-msg]").textContent = row.message;
    $("[data-alert-back]").href = `/my-products/ai-camera/security/alerts?tab=${row.kind === "intrusion" ? "intrusion" : "camera"}&open=${encodeURIComponent(aid)}`;
    barEl.hidden = false;
    requestAnimationFrame(fit);
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.focus && !document.fullscreenElement) { S.focus = null; refresh(); }
  });
  document.addEventListener("fullscreenchange", () => requestAnimationFrame(fit));
  window.addEventListener("pagehide", () => {
    const sids = [...live, ...pb].map((e) => e.sid).filter(Boolean);
    if (sids.length) fetch("/api/live/stop", { method: "POST", keepalive: true, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify({ sessions: sids }) });
  });

  // Fit the grid to the height the screen has left below it (HD 1280x720 and up), so the
  // controls never fall below the fold. A grid of 16:9 tiles is itself 16:9.
  function fit() {
    const box = S.tab === "live" ? $(".cam-stage") : $("[data-pb-view]");
    if (!box || box.offsetParent === null) return;
    if (document.fullscreenElement) { box.style.maxWidth = ""; return; }   // full screen: CSS sizes it
    const below = S.tab === "live" ? 0 : $("[data-controls]").offsetHeight;
    const top = box.getBoundingClientRect().top + window.scrollY;
    const room = window.innerHeight - top - below - 16;
    box.style.maxWidth = `${Math.max(420, Math.round(room * 16 / 9) + 12)}px`;
  }
  window.addEventListener("resize", () => requestAnimationFrame(fit));
  $("[data-alert-close]").addEventListener("click", () => { $("[data-alertbar]").hidden = true; requestAnimationFrame(fit); });
  $("[data-alert-toggle]").addEventListener("click", (e) => {
    const btn = e.currentTarget, open = btn.getAttribute("aria-expanded") !== "true";
    btn.setAttribute("aria-expanded", String(open));
    btn.firstChild.textContent = open ? "ย่อ " : "รายละเอียด ";
    $("[data-alert-more]").hidden = !open;
    requestAnimationFrame(fit);
  });

  setDayRange(ymd(new Date()));
  setControls();
  loadBranches().catch(() => {});
})();
