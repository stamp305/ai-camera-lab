"""Build the portal's siren page (template + css + js) from the branch setup page's HTML."""
import os
import re

LAB = os.path.expanduser("~/Documents/ai-camera-lab")
SRC = os.path.join(LAB, "client/siren_setup/index.html")
APP = os.path.join(LAB, "alitech-live/app")
html = open(SRC, encoding="utf-8").read()

# inline style attributes -> classes (the portal's CSP allows no inline styles)
STYLE_CLASS = {
    'margin-top:10px': "s-mt10", 'margin-top:8px': "s-mt8", 'margin-top:12px': "s-mt12", 'margin-top:16px': "s-mt16",
    'font-weight:700;font-size:15px': "s-name", 'font-size:12.5px': "s-small", 'word-break:break-all': "s-break",
}


def destyle(text: str) -> str:
    def one(m):
        tag_classes, style = m.group(1), m.group(2).strip().rstrip(";")
        cls = STYLE_CLASS.get(style)
        assert cls, f"unmapped inline style: {style}"
        return tag_classes.replace('class="', f'class="{cls} ', 1) if 'class="' in tag_classes else tag_classes + f' class="{cls}"'
    # <tag ... class="x" style="..."> or <tag ... style="...">
    text = re.sub(r'(<[a-z]+[^<>]*?)\s+style="([^"]*)"', one, text)
    assert 'style="' not in text, "inline style left"
    return text


# ---- CSS: everything nested under .siren-app; @keyframes stay at the top level ----------
css = html[html.index("<style>") + 7: html.index("</style>")]
keyframes = []


def take_keyframes(src: str) -> str:
    out, i = [], 0
    while True:
        j = src.find("@keyframes", i)
        if j < 0:
            out.append(src[i:]); return "".join(out)
        out.append(src[i:j])
        k, depth = src.index("{", j), 0
        for p in range(k, len(src)):
            depth += src[p] == "{"
            depth -= src[p] == "}"
            if depth == 0:
                keyframes.append(src[j:p + 1]); i = p + 1; break


css = take_keyframes(css)
css = re.sub(r"(^|\})\s*html,\s*body\s*\{[^}]*\}", r"\1", css)        # page-level rules go
css = re.sub(r"(^|\})\s*html,\s*body\s*\{[^}]*\}", r"\1", css)
css = re.sub(r"(^|\})\s*html, body\s*\{[^}]*\}", r"\1", css)
css = re.sub(r":root\s*\{", "& {", css)
portal_fit = """
    /* inside the portal: our sidebar and title already frame the page */
    .app { min-height: 0; }
    .sidebar { position: static; margin: 0 0 16px; padding: 10px 14px; border: 1px solid #dedede; border-radius: 12px; }
    .sidebar-logo, .page-title, .breadcrumb, .sidebar-bottom { display: none; }
    .sidebar-nav { flex-wrap: wrap; overflow: visible; row-gap: 6px; }
    .main { max-width: none; margin: 0; padding: 0; }
    .s-mt8 { margin-top: 8px; } .s-mt10 { margin-top: 10px; } .s-mt12 { margin-top: 12px; } .s-mt16 { margin-top: 16px; }
    [data-tone="good"] { color: #1b7f3b; font-weight: 600; } [data-tone="warn"] { color: #9a5b00; font-weight: 600; } [data-tone="bad"] { color: #c0392b; font-weight: 600; }
    .s-name { font-weight: 700; font-size: 15px; } .s-small { font-size: 12.5px; } .s-break { word-break: break-all; }
"""
open(os.path.join(APP, "static/css/siren.css"), "w", encoding="utf-8").write(
    "/* Siren setup (from client/siren_setup/index.html), scoped to the portal page. */\n"
    + "\n".join(keyframes) + "\n.siren-app {\n" + css + portal_fit + "}\n")

html = html.replace("เครื่องนี้", "เครื่องสาขา")
# No LAN addresses on the page: the side box and the step-2 tip show the LAN check instead
# (does this browser reach the branch? see lanCheck below).
html = html.replace('<div class="company-name">เครื่องสาขาอยู่วง</div>', '<div class="company-name">การเชื่อมต่อ</div>', 1)
html = html.replace('<div class="company-role" id="net-side">…</div>', '<div class="company-role" id="net-side">กำลังตรวจ…</div>', 1)
html, n_tip = re.subn(r'<b>เครื่องสาขาอยู่วง</b>\s*<span class="mono" id="net">…</span><br>ไซเรนต้องต่อ Wi‑Fi\s*ที่ได้ IP ขึ้นต้นแบบเดียวกัน',
                      '<b>การเชื่อมต่อ</b> <span id="net">กำลังตรวจ…</span><br>ไซเรนต้องต่อ Wi‑Fi วงเดียวกับเครื่องสาขา'
                      '<br><button type="button" class="btn s-mt8" id="lan-recheck">ตรวจอีกครั้ง</button>', html, count=1)
assert n_tip == 1, "step-2 tip not found"

# ---- markup -------------------------------------------------------------------------------
body = html[html.index('<div class="app">'): html.index("<script>")]
body = body.rstrip()
assert body.endswith("</div>")
body = destyle(body)

# ---- script: runs once a branch is chosen; talks to /api/siren/<branch>/... ---------------
js = html[html.index("<script>") + 8: html.rindex("</script>")]
js = destyle(js)
old_post = re.search(r"const post = \(p, b\) => fetch\(p, \{[^\n]*\n?[^\n]*\.then\(r => r\.json\(\)\);", js)
assert old_post, "post() not found"
js = js.replace(old_post.group(0), """const post = (p, b) => fetch(sirenUrl(p), { method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }, body: JSON.stringify(b || {}) }).then(r => r.json());""")
js, n_net = re.subn(r'\n\s*\$\("#net"\)\.textContent = [^\n]*\n\s*\$\("#net-side"\)\.textContent = [^\n]*', "", js, count=1)
assert n_net == 1, "net lines not found"
# the step number lives only in this page's memory: leaving the page forgets everything
js = js.replace('localStorage.setItem("siren-step", step)', "MEM.step = step")
js = js.replace('+localStorage.getItem("siren-step") || 1', "+MEM.step || 1")
assert "localStorage" not in js, "localStorage left in siren.js"
n = js.count('fetch("/api/')
js = js.replace('fetch("/api/', 'sirenGet("/api/')
assert n >= 3, n
prelude = """// Siren setup, inside the portal (ported from client/siren_setup/index.html). The branch's
// own setup does the work; this page reaches it through /api/siren/<branch>/... (portal ->
// AI Camera admin -> branch), so the siren keys never leave the branch.
(() => {
  "use strict";
  const ROOT = document.querySelector("[data-siren]");
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;
  const select = document.querySelector("[data-siren-branch]");
  const sirenUrl = (p) => `/api/siren/${encodeURIComponent(ROOT.dataset.branch)}${p.replace(/^\\/api/, "")}`;
  const sirenGet = (p, o) => fetch(sirenUrl(p), { credentials: "same-origin", cache: "no-store", ...(o || {}) });
  const MEM = {};   // page memory only: nothing about the siren is kept in the browser

  // LAN check: the admin arms the branch with a one-use ticket; this browser then calls the
  // branch's LAN address itself. An answer = this computer is on the shop's network.
  // Only two answers: connected or not. Same public IP counts as connected (same shop
  // network seen from outside); a wrong store code or no network info yet is "not".
  const LAN_TEXT = {
    ok: ["Connected to device", "Connected to device · เครื่องนี้อยู่วงเดียวกับเครื่องสาขา", "good"],
    off: ["Not connected", "Not connected · เครื่องนี้ไม่ได้อยู่วงเดียวกับเครื่องสาขา", "bad"],
  };
  function showLan(key) {
    const [side, long, tone] = LAN_TEXT[key] || LAN_TEXT.off;
    const a = document.getElementById("net-side"), b = document.getElementById("net");
    if (a) { a.textContent = side; a.dataset.tone = tone; }
    if (b) { b.textContent = long; b.dataset.tone = tone; }
  }
  async function lanCheck() {
    const a = document.getElementById("net-side"), b = document.getElementById("net");
    if (a) { a.textContent = "กำลังตรวจ…"; a.dataset.tone = ""; }
    if (b) { b.textContent = "กำลังตรวจ…"; b.dataset.tone = ""; }
    let t;
    try {
      t = await fetch(sirenUrl("/lan-ticket"), { method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF }, body: "{}" }).then((r) => r.json());
    } catch (e) { return showLan("off"); }
    if (!t.ok) return showLan("off");
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 4000);
    try {
      const r = await fetch(t.url, { method: "POST", mode: "cors", credentials: "omit", cache: "no-store",
        targetAddressSpace: "local", signal: ctl.signal, headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ store_code: t.store_code, ticket: t.ticket }) });
      const j = await r.json().catch(() => ({}));
      if (r.ok && j.ok) return showLan("ok");
    } catch (e) { /* unreachable, refused or blocked by the browser */ }
    finally { clearTimeout(timer); }
    showLan(t.same_public ? "ok" : "off");
  }

  function run() {
"""
postlude = """
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
    lanCheck();
    const again = document.getElementById("lan-recheck");
    if (again) again.addEventListener("click", lanCheck);
  }).catch(() => {});
})();
"""
open(os.path.join(APP, "static/js/siren.js"), "w", encoding="utf-8").write(prelude + js + postlude)

# ---- template -----------------------------------------------------------------------------
tpl = """<!doctype html>
<html lang="th">
  <head>
{% include "_head.html" %}
    <link rel="stylesheet" href="/static/css/siren.css?v={{ asset_v }}">
    <title>3 in 1 WiFi Siren Alarm · ALITECH</title>
  </head>
  <body class="portal-page">
    {% include "_sidebar.html" %}
    <main class="portal-main">
      <header class="portal-topbar"><span>Security / Hardware Integration / <b>3 in 1 WiFi Siren Alarm</b></span><button class="portal-bell" type="button" aria-label="Notifications"><svg viewBox="0 0 24 24"><path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg></button></header>
      {% include "_secnav.html" %}
      <section class="portal-content cam-content" aria-labelledby="sr-title">
        <a class="cam-back sr-back" href="/my-products/ai-camera/security/hardware?type=siren">&lsaquo; Hardware Integration</a>
        <div class="sr-head">
          <div><h1 id="sr-title">ตั้งค่าไซเรนในร้าน</h1><p class="analytics-updated">Tuya · 3 in 1 WiFi Siren Alarm รุ่น A03Pro · 🔒 key ของไซเรนเก็บที่เครื่องสาขาเท่านั้น · ทุกการสแกน / ทดสอบเสียงถูกบันทึก</p></div>
          <label class="sr-branch"><span>สาขา</span><select data-siren-branch></select></label>
        </div>
        <div class="siren-app" data-siren data-branch="">
""" + body + """
        </div>
      </section>
    </main>
    <script src="/static/js/portal-lite.js?v={{ asset_v }}" defer></script>
    <script src="/static/js/siren.js?v={{ asset_v }}" defer></script>
  </body>
</html>
"""
open(os.path.join(APP, "templates/siren.html"), "w", encoding="utf-8").write(tpl)
print("ok: posts ->", js.count("post("), "| gets ->", js.count("sirenGet("), "| keyframes", len(keyframes))
