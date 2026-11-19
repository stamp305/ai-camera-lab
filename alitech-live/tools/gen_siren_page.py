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
    .s-name { font-weight: 700; font-size: 15px; } .s-small { font-size: 12.5px; } .s-break { word-break: break-all; }
"""
open(os.path.join(APP, "static/css/siren.css"), "w", encoding="utf-8").write(
    "/* Siren setup (from client/siren_setup/index.html), scoped to the portal page. */\n"
    + "\n".join(keyframes) + "\n.siren-app {\n" + css + portal_fit + "}\n")

html = html.replace("เครื่องนี้", "เครื่องสาขา")

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
        <a class="cam-back sr-back" href="/my-products/ai-camera/security/hardware">&lsaquo; Hardware Integration</a>
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
