// Siren setup, inside the portal (ported from client/siren_setup/index.html). The branch's
// own setup does the work; this page reaches it through /api/siren/<branch>/... (portal ->
// AI Camera admin -> branch), so the siren keys never leave the branch.
(() => {
  "use strict";
  const ROOT = document.querySelector("[data-siren]");
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;
  const select = document.querySelector("[data-siren-branch]");
  const sirenUrl = (p) => `/api/siren/${encodeURIComponent(ROOT.dataset.branch)}${p.replace(/^\/api/, "")}`;
  const sirenGet = (p, o) => fetch(sirenUrl(p), { credentials: "same-origin", cache: "no-store", ...(o || {}) });

  function run() {

    const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
    const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const post = (p, b) => fetch(sirenUrl(p), { method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }, body: JSON.stringify(b || {}) }).then(r => r.json());
    const spin = t => `<p class="msg"><span class="spinner"></span>${t}</p>`;
    const table = (head, rows) => `<div class="table-wrap"><table><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
    const done = new Set();
    let step = 1, found = [];

    function go(n) {
      step = Math.max(1, Math.min(5, n));
      $$(".page").forEach(p => p.classList.toggle("on", +p.dataset.page === step));
      $$(".st").forEach(s => { const k = +s.dataset.go; s.classList.toggle("active", k === step); s.classList.toggle("done", done.has(k) && k !== step); });
      $("#crumb").textContent = $(`.st[data-go="${step}"]`).lastChild.textContent;
      try { localStorage.setItem("siren-step", step); } catch (e) { }
      if (step === 5) liveLoop(); else liveRun++;
    }
    function finish(n) { done.add(n); go(step); }
    $$(".st").forEach(s => s.onclick = () => go(+s.dataset.go));
    $$("[data-next]").forEach(b => b.onclick = () => { done.add(step); go(step + 1); });
    $$("[data-back]").forEach(b => b.onclick = () => go(step - 1));
    $("#added").onchange = e => { $("#next2").disabled = !e.target.checked; };

    sirenGet("/api/state").then(r => r.json()).then(s => {
      $("#net").textContent = s.net.lan ? `${s.net.lan}  (${s.net.ip})` : "ยังไม่ได้ต่อเครือข่าย";
      $("#net-side").textContent = s.net.ip && s.net.lan ? s.net.ip : "ไม่ได้ต่อเครือข่าย";
      if (s.saved.length) {
        done.add(1); done.add(2); done.add(3); $("#added").checked = true; $("#next2").disabled = false; $("#next3").disabled = false;
        $("#qr").innerHTML = `<p class="msg ok">มีไซเรนที่เชื่อมไว้แล้ว: ${s.saved.map(d => esc(d.name) + " " + esc(d.id)).join(", ")} · สแกนใหม่ได้ถ้าเพิ่ม/เปลี่ยนไซเรน</p>`;
      }
      opts = { sounds: s.sounds, volumes: s.volumes }; current = s.sound; vol = s.volume; drawSettings();
      let saved = 1; try { saved = +localStorage.getItem("siren-step") || 1; } catch (e) { }
      go(saved);
    });

    /* 3: QR */
    let run = 0, tick = null;
    $("#b-qr").onclick = async () => {
      const me = ++run; clearInterval(tick);
      $("#qr").innerHTML = spin("กำลังสร้าง QR…");
      const r = await post("/api/qr");
      if (!r.ok) { $("#qr").innerHTML = `<p class="msg bad">${esc(r.why)}</p>`; return; }
      let left = r.seconds;
      $("#qr").innerHTML = `<div class="qrbox"><img src="${r.png}" alt="QR สำหรับแอป Smart Life">
    <ol class="how"><li>เปิดแอป Smart Life → แท็บ <b>ฉัน</b></li><li>กดไอคอน <b>สแกน</b> มุมขวาบน แล้วสแกน QR นี้</li><li>กด <b>ยืนยันการเข้าสู่ระบบ</b> ในแอป</li></ol></div>
    <p class="msg"><span class="spinner"></span>รอสแกน… <span class="count" id="left"></span></p>`;
      tick = setInterval(() => { left--; const el = $("#left"); if (el) el.textContent = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`; if (left <= 0) clearInterval(tick); }, 1000);
      /* one check at a time: after the scan the server spends a few seconds fetching keys, and a
         second check sent meanwhile would get "idle" back and overwrite the result */
      let s;
      do {
        await new Promise(ok => setTimeout(ok, 2000));
        if (me !== run) return;
        try { s = await sirenGet("/api/qr/status").then(r => r.json()); } catch (e) { s = { state: "error", why: String(e) }; }
        if (me !== run) return;
      } while (s.state === "waiting");
      clearInterval(tick);
      if (s.state === "done") {
        const sir = s.devices.filter(d => d.siren);
        $("#qr").innerHTML = `<p class="msg ${sir.length ? "ok" : "bad"}">${sir.length ? `เชื่อมแล้ว · ได้ key ไซเรน ${sir.length} ตัว` : "เชื่อมแล้ว แต่บัญชีนี้ไม่มีไซเรน — กลับไปขั้น 2"}</p>`
          + table(["ชื่อในแอป", "Device ID", ""], s.devices.map(d => `<tr><td>${esc(d.name)}</td><td class="mono">${esc(d.id)}</td>
      <td>${d.siren ? '<span class="pill ok">ไซเรน · เก็บ key</span>' : '<span class="pill mute">อุปกรณ์อื่น · ไม่เก็บ</span>'}</td></tr>`).join(""));
        if (sir.length) { $("#next3").disabled = false; finish(3); }
      } else {
        $("#qr").innerHTML = `<p class="msg bad">${s.state === "expired" ? "QR หมดเวลา — กดสร้าง QR ใหม่" : esc(s.why || "ผิดพลาด")}</p>`;
      }
    };

    /* 4: scan */
    $("#b-scan").onclick = async () => {
      const b = $("#b-scan"); b.disabled = true; $("#scan").innerHTML = spin("กำลังค้นหาในวงแลน (ประมาณ 8 วินาที)…");
      try {
        const r = await post("/api/scan");
        found = (r.rows || []).filter(x => x.ok);
        $("#scan").innerHTML = (r.why ? `<p class="msg bad">${esc(r.why)}</p>` : "") + (r.rows && r.rows.length ? `<p class="msg muted">เครื่องสาขา <span class="mono">${esc(r.own_ip)}</span></p>`
          + table(["ไซเรน", "IP ในวง", "สถานะ"], r.rows.map(x => `<tr>
      <td>${esc(x.name)} <span class="mono">${esc(x.id)}</span></td><td class="mono">${esc(x.ip || "–")}</td>
      <td>${x.ok ? '<span class="pill ok">เจอ · key ถูกต้อง</span>' : `<span class="pill bad">${esc(x.why)}</span>`}</td></tr>`).join("")) : "")
          + (r.search ? `<div class="s-mt12 row"><button class="btn" id="b-detail">ดูรายละเอียดการค้นหา</button></div><div id="detail" hidden>${detail(r)}</div>` : "")
          + (r.ok ? "" : `<div class="s-mt12 tip warn">ไม่เจอ: เช็กว่าไซเรนกับเครื่องสาขาต่อ Wi‑Fi วงเดียวกัน (ขั้น 2) และไซเรนเสียบไฟอยู่</div>`);
        const bd = $("#b-detail");
        if (bd) bd.onclick = () => { const d = $("#detail"); d.hidden = !d.hidden; bd.textContent = d.hidden ? "ดูรายละเอียดการค้นหา" : "ซ่อนรายละเอียด"; };
        if (r.ok) { $("#next4").disabled = false; finish(4); setReady(); }
      } catch (e) { $("#scan").innerHTML = `<p class="msg bad">${esc(e)}</p>`; }
      b.disabled = false;
    };
    const VOL = { low: "เบา", middle: "กลาง", high: "ดัง", mute: "ปิดเสียง" };
    const toneName = t => /^mode_\d+$/.test(t || "") ? `เสียงที่ ${t.slice(5)}` : (t ?? "–");
    function reply(i) {
      const t = [];
      if (i._match) t.push(`Device ID ตรงกับที่ได้จาก QR (${esc(i._match)})`);
      if ("productKey" in i) t.push(`รหัสรุ่นสินค้า ${esc(i.productKey)}`);
      if ("version" in i) t.push(`โปรโตคอล Tuya ${esc(i.version)}`);
      if ("encrypt" in i) t.push(i.encrypt ? "สื่อสารแบบเข้ารหัส" : "ไม่เข้ารหัส");
      if ("active" in i) t.push(i.active === 2 ? "ผูกกับบัญชีแล้ว" : `สถานะผูกบัญชี ${esc(i.active)}`);
      return t.join(" · ");
    }
    function detail(r) {
      const q = r.search, h = q.heard;
      return `<div class="s-mt12 tip">
      <b>วิธีค้นหา</b><br>
      1. ส่งคำถาม "ใครอยู่บ้าง" (แบบเดียวกับแอป Smart Life) ไปที่ UDP 7000 แบบกระจายทั้งวง (255.255.255.255) และส่งตรงทีละ IP ${esc(q.asked || "")}<br>
      2. ฟังคำตอบที่ UDP ${q.listen.join(", ")} สูงสุด ${q.limit} วินาที (ครั้งนี้ใช้ ${q.seconds} วินาที ฟังจนครบเวลาเพื่อให้เห็นทุกตัวที่ตอบ)<br>
      3. เจอ Device ID ตรงกับที่ได้จากขั้น 3 แล้ว ต่อ TCP 6668 จับมือด้วย local key และอ่านสถานะ ถ้าอ่านได้แปลว่า key ถูกต้อง<br>
      <span class="muted">ตอบได้เฉพาะอุปกรณ์ Tuya/Smart Life เท่านั้น อุปกรณ์อื่นในวง (มือถือ คอม กล้อง) ไม่ตอบคำถามนี้ จึงไม่อยู่ในรายการ</span></div>
      <p class="msg"><b>อุปกรณ์ Tuya ที่ตอบกลับ ${h.length} ตัว</b> <span class="muted">· ใต้แต่ละแถวคือข้อความที่อุปกรณ์ตอบมา</span></p>
      ${h.length ? table(["Device ID", "IP", "โปรโตคอล", "ได้ยินที่", ""], h.map(x => `<tr class="has-raw">
        <td class="mono">${esc(x.id)}</td><td class="mono">${esc(x.ip)}</td><td class="mono">${esc(x.version || "–")}</td><td class="mono">UDP ${esc(x.port)}</td>
        <td>${x.ours ? '<span class="pill ok">ไซเรนของเรา</span>' : '<span class="pill mute">อุปกรณ์อื่น</span>'}</td></tr>
        <tr><td colspan="5" class="raw">${reply(x.ours ? { _match: x.id, ...x.info } : x.info)}<div class="mono">${esc(JSON.stringify(x.info))}</div></td></tr>`).join("")) : '<p class="muted">ไม่มีอุปกรณ์ไหนตอบ</p>'}
      ${r.rows.filter(x => x.ok).map(x => `<p class="msg"><b>สถานะที่อ่านได้จาก ${esc(x.name)} ${esc(x.id)}</b><br>
        ระดับเสียง ${esc(VOL[x.volume] || x.volume)} · ดังนาน ${esc(x.seconds)} วินาที · ${esc(toneName(x.sound))} · ตอนนี้${x.ringing ? "กำลังดัง" : "ไม่ได้ดัง"}<br>
        <span class="s-break mono muted">${esc(JSON.stringify(x.dps))}</span></p>`).join("")}`;
    }
    function setReady() {
      if (!found.length) return;
      $("#ready-name").textContent = found.map(f => f.name || "ไซเรน").join(", ");
      $("#ready-info").innerHTML = found.map(f => `<span class="chip">${esc(f.id)}</span> <span class="mono">${esc(f.ip)}</span> · ${esc(toneName(f.sound))} · ${esc(VOL[f.volume] || f.volume)} ${esc(f.seconds)} วินาที`).join("<br>");
    }

    /* 5: alert sound + volume (saved only; the siren gets them on its next ring) */
    const VOL_ORDER = ["mute", "low", "middle", "high"];
    let opts = { sounds: [], volumes: [] }, current = null, vol = null;
    function drawSettings() {
      $("#sounds").innerHTML = opts.sounds.map(t => `<button data-sound="${esc(t)}" class="${t === current ? "active" : ""}">${esc(toneName(t))}${t === "mode_2" ? "<small>ค่าเริ่มต้น</small>" : ""}</button>`).join("");
      $("#volumes").innerHTML = VOL_ORDER.filter(v => opts.volumes.includes(v)).map(v => `<button data-volume="${v}" class="${v === vol ? "active" : ""}">${esc(VOL[v])}</button>`).join("");
      $("#vol-note").textContent = vol === "mute" ? "ปิดเสียง: ตอนแจ้งเตือนไซเรนจะไม่มีเสียง (แค่ทำงานเงียบๆ) เลือกระดับอื่นถ้าต้องการให้ดังจริง" : "";
      $$("#sounds button").forEach(b => b.onclick = () => save({ sound: b.dataset.sound }));
      $$("#volumes button").forEach(b => b.onclick = () => save({ volume: b.dataset.volume }));
    }
    async function save(change) {
      try {
        const r = await post("/api/settings", change);
        if (!r.ok) { $("#sound-msg").innerHTML = `<p class="msg bad">บันทึกไม่สำเร็จ: ${esc(r.why)}</p>`; return; }
        current = r.sound; vol = r.volume; drawSettings(); liveLoop();
        $("#sound-msg").innerHTML = `<p class="msg ok">บันทึกแล้ว: ${esc(toneName(current))} · ระดับ${esc(VOL[vol])} · ไซเรนจะใช้ตอนแจ้งเตือนครั้งถัดไป</p>`;
      } catch (e) { $("#sound-msg").innerHTML = `<p class="msg bad">${esc(e)}</p>`; }
    }

    /* 5: live values from the siren (what the app set), polled one request at a time */
    let liveRun = 0;
    async function liveLoop() {
      const me = ++liveRun;
      while (me === liveRun && step === 5) {
        let wait = 4000;
        if (!document.hidden) {
          try { const r = await sirenGet("/api/live").then(x => x.json()); if (me !== liveRun) return; drawLive(r); if (!r.ok && !r.busy) wait = 15000; }
          catch (e) { $("#live").innerHTML = `<p class="msg bad">${esc(e)}</p>`; wait = 15000; }
        }
        await new Promise(ok => setTimeout(ok, wait));
      }
    }
    function drawLive(r) {
      const at = $("#live-at");
      if (r.busy) return;                       // a ring/search is talking to the siren; keep the last reading
      if (!r.ok) {
        at.classList.remove("on"); at.textContent = "";
        $("#live").innerHTML = `<p class="msg bad">อ่านค่าไม่ได้: ${esc(r.why || (r.sirens || []).map(x => x.why).join(", "))}</p>`;
        return;
      }
      at.classList.add("on"); at.textContent = `อัปเดต ${r.at}`;
      const c = r.chosen, s = r.sirens.find(x => x.ok);
      const row = (label, dev, mine, show) => `<tr><td>${label}</td><td class="${dev === mine ? "" : "diff"}">${esc(show(dev))}</td><td>${esc(show(mine))}</td></tr>`;
      const same = s.sound === c.sound && s.volume === c.volume && s.seconds === c.seconds;
      $("#live").innerHTML = table(["", `ในไซเรนตอนนี้ (${esc(s.name || s.id)})`, "ที่จะใช้ตอนแจ้งเตือน"],
        row("เสียงแจ้งเตือน", s.sound, c.sound, toneName)
        + row("ระดับเสียง", s.volume, c.volume, v => VOL[v] || v)
        + row("ดังนาน", s.seconds, c.seconds, v => `${v ?? "–"} วินาที`)
        + `<tr><td>ตอนนี้</td><td>${s.ringing ? "กำลังดัง" : "ไม่ได้ดัง"}</td><td></td></tr>`)
        + (same ? `<p class="msg ok">ตรงกัน</p>`
          : `<div class="s-mt12 row"><span class="s-small muted">ค่าไม่ตรงกัน: ตอนแจ้งเตือนระบบจะตั้งไซเรนเป็นค่าทางขวา</span>
             <button class="btn" id="b-use-app">ใช้ค่าจากแอป</button></div>`);
      const u = $("#b-use-app");
      if (u) u.onclick = () => save({ sound: s.sound, volume: s.volume, seconds: s.seconds });
    }

    /* 5: ring */
    async function ring(path, audible) {
      const bs = [$("#b-ring"), $("#b-check")]; bs.forEach(b => b.disabled = true);
      $("#ring").innerHTML = spin(audible ? "กำลังสั่งให้ดัง…" : "กำลังคุยกับไซเรน…");
      try {
        const r = await post(path);
        if (r.ok && audible) { const i = $("#ico"); i.classList.remove("ring"); void i.offsetWidth; i.classList.add("ring"); }
        $("#ring").innerHTML = r.ok
          ? `<p class="msg ok">${audible ? `ไซเรนดัง${esc(toneName(current))} แล้ว และคืนระดับเสียงเดิมเรียบร้อย` : "ไซเรนตอบรับ (ไม่มีเสียง)"} · ${r.sirens.filter(s => s.ok).map(s => esc(s.name || s.id) + " " + esc(s.ip)).join(", ")}</p>`
          : `<p class="msg bad">ไม่สำเร็จ: ${esc(r.why || (r.sirens || []).map(s => s.why).join(", "))}</p>`;
        if (r.ok) finish(5);
      } catch (e) { $("#ring").innerHTML = `<p class="msg bad">${esc(e)}</p>`; }
      bs.forEach(b => b.disabled = false);
    }
    $("#b-ring").onclick = () => ring("/api/ring", true);
    $("#b-check").onclick = () => ring("/api/check", false);
  
  }

  // pick the branch (real ones only; ?branch=<id> preselects), then start the steps
  fetch("/api/live/branches", { credentials: "same-origin", cache: "no-store" }).then((r) => r.json()).then((b) => {
    const list = (b.branches || []).filter((x) => !x.demo);
    if (!list.length) { ROOT.replaceChildren(Object.assign(document.createElement("p"), { className: "cam-hint", textContent: "ไม่มีสาขาที่ออนไลน์" })); return; }
    const want = new URLSearchParams(location.search).get("branch");
    for (const x of list) select.append(new Option(`${x.name}${x.province ? " · จ." + x.province : ""}`, x.client_id));
    select.value = list.some((x) => x.client_id === want) ? want : list[0].client_id;
    ROOT.dataset.branch = select.value;
    select.addEventListener("change", () => { location.search = "?branch=" + encodeURIComponent(select.value); });
    run();
  }).catch(() => {});
})();
