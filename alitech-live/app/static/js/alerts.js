// Security > Alert & Log: after-hours people / camera quality, DEMO rows for now.
// Filters like Alitech's Zone & Behavior page (regions: many, branch: one with search).
// The table scrolls under its header and loads 20 more rows when it reaches the bottom.
// Row text goes in with textContent; pictures come from this site's server (admin only, audited).
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const PER = 20;
  // picked / chosen empty = all (nothing filtered)
  const S = { tab: "intrusion", regions: [], branches: [], picked: new Set(), chosen: new Set(), q: "", next: 0, total: 0, loading: false, gen: 0 };
  const TITLES = { intrusion: "แจ้งเตือนคนในร้านหลังร้านปิด", camera: "คุณภาพกล้องลดลง" };
  const KIND_CLASS = { intrusion: "is-red", angle: "is-amber", swap: "is-amber", black: "is-red", degraded: "is-amber", rtsp_down: "is-grey", no_frames: "is-grey" };
  const scroller = $("[data-scroll]");
  const tbody = $("[data-rows]");
  const more = $("[data-more]");

  const bar = $("[data-filters]");
  const fRegion = Filters.create(bar, { key: "region", name: "ภาค", icon: "region" });
  const fBranch = Filters.create(bar, { key: "branch", name: "สาขา", icon: "branch" });
  const inRegions = (b) => !S.picked.size || S.picked.has(b.region);
  fRegion.build = (menu) => Filters.checkMenu(menu, {
    heading: "เลือกภาค", allText: "ทุกภาค", selected: S.picked.size ? S.picked : new Set(S.regions),
    options: S.regions.map((r) => ({ value: r, text: r, small: `${S.branches.filter((b) => b.region === r).length} สาขา` })),
    onApply: (picked) => {
      S.picked = new Set(picked.length === S.regions.length ? [] : picked);
      S.chosen = new Set([...S.chosen].filter((c) => inRegions(S.branches.find((x) => x.code === c) || {})));
      fRegion.close(); reload();
    } });
  fBranch.build = (menu) => {
    const list = S.branches.filter(inRegions);
    Filters.checkMenu(menu, {
      heading: "เลือกสาขา", allText: "ทุกสาขา", search: "ค้นหาสาขาหรือจังหวัด",
      selected: S.chosen.size ? S.chosen : new Set(list.map((b) => b.code)),
      options: list.map((b) => ({ value: b.code, text: `${b.code} ${b.place}`, small: "จ." + b.province, search: `${b.code} ${b.place} ${b.province}` })),
      onApply: (picked) => { S.chosen = new Set(picked.length === list.length ? [] : picked); fBranch.close(); reload(); } });
  };

  function labels() {
    fRegion.setValue(S.picked.size ? (S.picked.size === 1 ? [...S.picked][0] : `${S.picked.size} ภาค`) : "ทุกภาค");
    const one = S.chosen.size === 1 ? S.branches.find((x) => x.code === [...S.chosen][0]) : null;
    fBranch.setValue(!S.chosen.size ? "ทุกสาขา" : one ? `${one.code} ${one.place}` : `${S.chosen.size} สาขา`);
    const scope = $("[data-scope]");
    scope.replaceChildren();
    const lead = document.createElement("span"); lead.textContent = "แสดง:"; scope.append(lead);
    const chips = S.picked.size ? [...S.picked] : ["ทุกภาค"];
    for (const c of chips) { const s = document.createElement("span"); s.className = "scope-chip is-zone"; s.textContent = c; scope.append(s); }
    const codes = [...S.chosen];
    for (const c of (codes.length ? codes.slice(0, 6) : [""])) {
      const x = S.branches.find((y) => y.code === c);
      const bc = document.createElement("span"); bc.className = "scope-chip"; bc.textContent = x ? `${x.code} จ.${x.province}` : "ทุกสาขา"; scope.append(bc);
    }
    if (codes.length > 6) { const m = document.createElement("span"); m.className = "scope-chip"; m.textContent = `+${codes.length - 6}`; scope.append(m); }
    const count = document.createElement("span"); count.className = "breakdown-row-count";
    count.textContent = `${S.total} รายการ`; scope.append(count);
  }

  function reload() {
    S.gen += 1; S.next = 0; S.total = 0;
    tbody.replaceChildren();
    scroller.scrollTop = 0;
    $("[data-table-title]").textContent = TITLES[S.tab];
    loadMore();
  }

  async function loadMore() {
    if (S.loading || S.next === null) return;
    S.loading = true;
    const gen = S.gen;
    more.replaceChildren(Spinner.make("sm"), document.createTextNode(" กำลังโหลด…"));
    more.classList.add("is-loading");
    const params = new URLSearchParams({ kind: S.tab, offset: String(S.next), limit: String(PER), regions: [...S.picked].join(","), branches: [...S.chosen].join(","), q: S.q });
    try {
      const res = await fetch(`/api/alerts?${params}`, { credentials: "same-origin", cache: "no-store" });
      if (res.status === 401) { location.href = "/login"; return; }
      const body = await res.json();
      if (gen !== S.gen) return;
      S.regions = body.regions || S.regions;
      S.branches = body.branches || S.branches;
      S.total = body.total || 0;
      S.next = body.next;
      for (const r of body.rows || []) tbody.append(row(r));
      if (!tbody.rows.length) { const tr = tbody.insertRow(); tr.className = "breakdown-empty"; const td = tr.insertCell(); td.colSpan = 5; td.textContent = "ไม่มีรายการ"; }
      labels();
      more.textContent = S.next === null ? (tbody.rows.length > PER ? "แสดงครบทุกรายการแล้ว" : "") : "";
    } catch (_) {
      more.textContent = "โหลดไม่สำเร็จ เลื่อนลงเพื่อลองใหม่";
    } finally {
      more.classList.remove("is-loading");
      S.loading = false;
      // a short page on a tall screen: keep loading until the box can scroll
      if (gen === S.gen && S.next !== null && scroller.scrollHeight <= scroller.clientHeight + 4) loadMore();
    }
  }

  function row(r) {
    const tr = document.createElement("tr");
    tr.tabIndex = 0;
    const td = (text) => { const c = tr.insertCell(); if (text !== undefined) c.textContent = text; return c; };
    td(r.at);
    const b = td(); const bs = document.createElement("span"); bs.className = "branch-cell";
    const bn = document.createElement("span"); bn.textContent = `${r.code} จ.${r.province}`;
    const bp = document.createElement("small"); bp.textContent = r.place;
    bs.append(bn, bp); b.append(bs);
    td(r.camera);
    const d = td(); const chip = document.createElement("span");
    chip.className = `al-chip ${KIND_CLASS[r.kind] || ""}`; chip.textContent = r.detail; d.append(chip);
    td(r.images.length ? "ดูรูป" : "–").classList.add(r.images.length ? "al-has-img" : "al-no-img");
    tr.addEventListener("click", () => openRow(r));
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter") openRow(r); });
    return tr;
  }

  scroller.addEventListener("scroll", () => {
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 80) loadMore();
  }, { passive: true });

  const dlg = $("[data-dialog]");
  function openRow(r) {
    $("[data-d-title]").textContent = r.kind === "intrusion" ? "🚨 พบคนในร้านหลังเวลาปิด" : `⚠️ ${r.detail}`;
    $("[data-d-sub]").textContent = `${r.branch} · ${r.camera} · ${r.at}`;
    $("[data-d-message]").textContent = (r.kind === "intrusion" ? "[ALERT] แจ้งเตือน AI Camera\n" : "") + r.message;
    // Jump to the recording: CCTV Streaming > playback, that branch, channel and minute
    const jump = $("[data-d-jump]"), jnote = $("[data-d-jump-note]");
    const okJump = /^[A-Za-z0-9-]{4,40}$/.test(r.client_id || "") && Number.isInteger(r.channel) && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(r.at);
    // only after-hours people lead to the recording; camera-quality alerts have no clip button
    const noJump = r.kind !== "intrusion";
    $(".al-actions").hidden = noJump;
    jump.hidden = !okJump;
    jnote.textContent = okJump ? (String(r.client_id).startsWith("DEMO-") ? "สาขาตัวอย่าง: เปิดหน้าได้แต่ไม่มีภาพ" : "เปิดย้อนหลังก่อนเหตุการณ์ 1 นาที")
                               : "สาขานี้ออฟไลน์ ยังเปิดคลิปไม่ได้";
    if (okJump) {
      const q = new URLSearchParams({ branch: r.client_id, ch: String(r.channel), at: r.at.replace(" ", "T"), aid: r.id });
      jump.href = `/my-products/ai-camera/security/cctv?${q}`;
    }
    const box = $("[data-d-images]");
    box.replaceChildren();
    if (!r.images.length) { const p = document.createElement("p"); p.className = "cam-hint"; p.textContent = "ไม่มีรูป (กล้องไม่ส่งภาพ)"; box.append(p); }
    for (const id of r.images) {
      const fig = document.createElement("figure");
      const img = Object.assign(document.createElement("img"), { src: `/api/alerts/image/${encodeURIComponent(id)}`, alt: `${r.camera} ${r.at}` });
      const cap = document.createElement("figcaption");
      cap.textContent = id.endsWith("-before") ? "ภาพอ้างอิง (ก่อน)" : id.endsWith("-now") ? "ภาพล่าสุด" : "ภาพจากกล้อง";
      fig.append(img, cap); box.append(fig);
    }
    box.classList.toggle("is-pair", r.images.length > 1);
    document.documentElement.classList.add("modal-open");
    dlg.showModal();
  }
  dlg.addEventListener("close", () => document.documentElement.classList.remove("modal-open"));
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  $("[data-d-close]").addEventListener("click", () => dlg.close());

  let typing = null;
  $("[data-search]").addEventListener("input", (e) => { clearTimeout(typing); typing = setTimeout(() => { S.q = e.target.value.trim().toLowerCase(); reload(); }, 250); });
  $$("[data-tab]").forEach((b) => b.addEventListener("click", () => {
    if (S.tab === b.dataset.tab) return;
    S.tab = b.dataset.tab;
    $$("[data-tab]").forEach((x) => { x.classList.toggle("is-active", x === b); x.setAttribute("aria-selected", String(x === b)); });
    reload();
  }));
  // Back from a clip: ?tab=intrusion|camera&open=<alert id> -> that tab, that alert open again
  const back = new URLSearchParams(location.search);
  const backTab = back.get("tab"), backId = back.get("open") || "";
  if (backTab === "intrusion" || backTab === "camera") {
    S.tab = backTab;
    $$("[data-tab]").forEach((x) => { const on = x.dataset.tab === backTab; x.classList.toggle("is-active", on); x.setAttribute("aria-selected", String(on)); });
  }
  labels();
  reload();
  if (/^[a-z]{2}\d{3}$/.test(backId)) {
    history.replaceState(null, "", location.pathname);
    fetch(`/api/alerts/item/${backId}`, { credentials: "same-origin", cache: "no-store" })
      .then((r) => r.json()).then((b) => { if (b.ok && b.row) openRow(b.row); }).catch(() => {});
  }
})();
