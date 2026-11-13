"""Runs a zone page's real scripts (static/js/app.js, static/js/zone_editor.js and the
inline script of a template) in node against a stub DOM and a fake fetch, and reports
the URLs the page asked for and what it told the technician."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "frontend_web"
NODE = shutil.which("node")

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [appJs, editorJs, pageJs, scenarioJson] = process.argv.slice(1);
const sc = JSON.parse(scenarioJson);
const any = new Proxy(function () {}, {
    get: (t, k) => (k === 'then' ? undefined : k === Symbol.toPrimitive ? () => '' : any),
    apply: () => any, construct: () => any,
});
function element() {
    const classes = new Set(['ac-hidden']);
    const on = {}, q = {}, qa = {};
    return {
        innerHTML: '', textContent: '', style: {}, dataset: {}, src: '', value: '',
        classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c),
                     toggle: (c, yes) => (yes ? classes.add(c) : classes.delete(c)), contains: (c) => classes.has(c) },
        addEventListener(type, fn) { (on[type] ||= []).push(fn); },
        fire(type, e = {}) { return Promise.all((on[type] || []).map((fn) => fn({ stopPropagation() {}, ...e }))); },
        appendChild() {}, closest: () => element(),
        querySelector(sel) { return (q[sel] ||= element()); },
        // the saved list's Delete buttons and the side panel's direction buttons, as many as
        // the rendered HTML has (kept while it is unchanged)
        querySelectorAll(sel) {
            const m = /^\[data-(delete|dir)\]$/.exec(sel);
            if (!m) return [];
            if (!qa[sel] || qa[sel].html !== this.innerHTML) {
                qa[sel] = { html: this.innerHTML, list: [...this.innerHTML.matchAll(new RegExp(`data-${m[1]}="([^"]*)"`, 'g'))]
                    .map(([, v]) => Object.assign(element(), { dataset: { [m[1]]: v } })) };
            }
            return qa[sel].list;
        },
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 100 }),
    };
}
const els = {}, byId = {}, created = [];
const root = element();
root.querySelector = (sel) => { const m = /data-el="([^"]+)"/.exec(sel); return m ? (els[m[1]] ||= element()) : any; };
root.querySelectorAll = () => [];
const document = new Proxy({
    getElementById: (id) => (id === 'zone-editor' ? root : id === 'ze-delete' ? created.find((e) => e.id === id) || null
                             : (byId[id] ||= element())),
    createElement: () => { const e = element(); created.push(e); return e; },
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
}, { get: (t, k) => (k in t ? t[k] : any) });
const fetched = [], calls = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const sandbox = {
    document, console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
    location: sc.location, navigator: any, localStorage: any, sessionStorage: any,
    ResizeObserver: class { observe() {} }, MutationObserver: class { observe() {} },
    addEventListener() {}, crypto: { randomUUID: () => 'uuid' }, URL, Promise, AbortController,
    acEscape: (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (m) => `&#${m.charCodeAt(0)};`),  // layout_navbar's
};
sandbox.window = sandbox;
sandbox.fetch = async (url, init) => {
    fetched.push(String(url));
    calls.push(`${(init && init.method) || 'GET'} ${url}`);
    const hit = sc.answers.find(([part]) => String(url).includes(part));
    const [, status, headers, body] = hit || ['', 200, {}, []];
    if (status === 0) throw new TypeError('Failed to fetch');  // no answer at all
    if (status === -1) {  // connects but never answers: only the page's own abort ends it
        return new Promise((resolve, reject) => init && init.signal
            && init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))));
    }
    return { status, ok: status < 400, headers: { get: (k) => headers[k] ?? null }, json: async () => body };
};
vm.createContext(sandbox);
for (const file of [appJs, editorJs, pageJs]) vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: file });
const get = (name) => (els[name] ||= element());
let menu = [];
(async () => {
    await sleep(sc.wait || 200);
    if (sc.action === 'save') {  // draw a triangle, name it, Save Polygon
        document.getElementById('zone-name').value = 'Entrance';
        document.getElementById('zone-duration').value = '60';
        for (const [id, value] of sc.inputs || []) {  // side-panel fields, e.g. a speed threshold
            document.getElementById(id).value = value;
            await document.getElementById(id).fire('input');
        }
        for (const sel of sc.pick || []) await get('side').querySelectorAll(sel)[0].fire('click');
        await get('draw').fire('click');
        for (const [x, y] of [[10, 10], [50, 10], [50, 50]]) await get('canvas').fire('click', { clientX: x, clientY: y });
        await get('save').fire('click');
    } else if (sc.action === 'menu') {  // open a pill menu of the page and pick the option labelled sc.choose
        const [btn, list] = sc.menu, before = created.length;
        document.getElementById(list).hidden = true;
        await document.getElementById(btn).fire('click');
        const options = created.slice(before);
        menu = options.map((e) => e.textContent);
        const opt = options.find((e) => e.textContent === sc.choose);
        if (opt) await opt.fire('click');
    } else if (sc.action === 'delete') {  // Delete on the first saved polygon, then confirm
        await get('list').querySelectorAll('[data-delete]')[0].fire('click');
        await created.find((e) => e.id === 'ze-delete').querySelector('[data-ok]').onclick();
    }
    for (const [id, type] of sc.fire || []) await document.getElementById(id).fire(type);  // e.g. the img's error
    await sleep(100);
    process.stdout.write(JSON.stringify({
        fetched, calls, blocked: get('blocked').innerHTML, blockedShown: !get('blocked').classList.contains('ac-hidden'),
        mainShown: !get('main').classList.contains('ac-hidden'), img: get('img').src, menu,
        ids: Object.fromEntries(Object.entries(byId).map(([id, e]) => [id, { html: e.innerHTML, text: e.textContent, src: e.src,
                                                                              shown: !e.classList.contains('ac-hidden') }])),
    }));
    process.exit(0);
})();
"""


def page_runner(template, tmp_path):
    """run(location, answers, action=None, **scenario) for the page of ``template``."""
    if NODE is None:
        pytest.skip("node is not installed")
    html = (WEB / "templates" / template).read_text()
    inline = re.search(r"<script>\s*(.*?)\s*</script>", html, re.S).group(1)  # the page's one inline script
    page_js = tmp_path / f"{template}.inline.js"
    page_js.write_text(inline)

    def run(location, answers, action=None, **scenario):
        out = subprocess.run([NODE, "-e", HARNESS, str(WEB / "static/js/app.js"), str(WEB / "static/js/zone_editor.js"),
                              str(page_js), json.dumps({"location": location, "answers": answers, "action": action,
                                                        **scenario})],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    return run
