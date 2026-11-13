// Shared polygon editor for the per-camera service pages (walk in/pass, hot zone, rate, attendance).
// Each page calls ZoneEditor.mount(rootEl, config); see draw.html for the fullest config.
(function () {
    const COLORS = ['#f57c00', '#16a34a', '#2563eb', '#9333ea', '#dc2626', '#0891b2', '#ca8a04', '#db2777'];
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
    const caret = '<svg class="ac-caret" viewBox="0 0 24 24"><path d="m7 10 5 5 5-5"/></svg>';

    function mount(root, cfg) {
        const API = getApiUrl();
        // Zone Settings live in the feature's own DLC service (cfg.svc), reached through the
        // frontend route; built like getApiUrl() because images load outside the fetch hook.
        if (!cfg.svc) throw new Error('ZoneEditor.mount: cfg.svc (the DLC service folder) is required');
        const SVC = `${window.location.protocol}//${window.location.host}${appUrl(`api/svc/${cfg.svc}`)}`;
        const state = {
            cameras: [], network: null, camera: null, polygons: [],
            points: [], redo: [], drawing: false,
            // Edit (cfg.editable): {id, orig (the polygon as saved), points, rect, history:[snapshots]}
            edit: null,
            color: COLORS[0], type: cfg.types ? cfg.types[0].value : null, extras: {},
        };

        root.innerHTML = `
            <div class="ac-page">
                <div class="ac-page-head">
                    <h1 class="ac-page-title">${esc(cfg.title)}</h1>
                    <p class="ac-page-sub">${esc(cfg.subtitle || 'Draw detection zones on each camera')}</p>
                </div>
                <div data-el="blocked" class="ac-empty ac-hidden"></div>
                <div data-el="loading" class="ac-empty"><div class="ac-spinner"></div></div>
                <div data-el="main" class="ac-hidden">
                    <div class="ac-pill-row">
                        <div class="ac-pill-wrap">
                            <button class="ac-pill" data-el="net-btn">Network<span class="val" data-el="net-val">-</span>${caret}</button>
                            <div class="ac-menu" data-el="net-menu" hidden></div>
                        </div>
                        <div class="ac-pill-wrap">
                            <button class="ac-pill" data-el="cam-btn">Camera<span class="val" data-el="cam-val">-</span>${caret}</button>
                            <div class="ac-menu" data-el="cam-menu" hidden></div>
                        </div>
                    </div>
                    <div class="ac-card" style="display:grid;grid-template-columns:minmax(0,1fr) 360px;">
                        <div style="padding:24px;border-right:1px solid #e5e5e5;">
                            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px;min-height:46px;">
                                <h2 class="ac-card-title" data-el="card-title"></h2>
                                ${cfg.types ? `<div class="ac-seg" data-el="seg">${cfg.types.map(t => `<button data-type="${esc(t.value)}">${esc(t.label)}</button>`).join('')}</div>` : ''}
                            </div>
                            <div data-el="canvas" style="position:relative;border-radius:6px;overflow:hidden;background:#1f2937;user-select:none;-webkit-user-select:none;">
                                <img data-el="img" alt="" style="display:block;width:100%;height:auto;pointer-events:none;">
                                <svg data-el="svg" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none;"></svg>
                                <div data-el="handles" style="position:absolute;inset:0;pointer-events:none;"></div>
                                <div data-el="hint" class="ac-hidden" style="position:absolute;left:50%;bottom:16px;transform:translateX(-50%);padding:6px 12px;border-radius:6px;background:rgba(0,0,0,.6);color:#fff;font-size:13px;pointer-events:none;">Click on the image to add points</div>
                            </div>
                            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:20px;flex-wrap:wrap;">
                                <div style="display:flex;gap:12px;">
                                    <button class="ac-btn" data-el="undo"><svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>Undo</button>
                                    <button class="ac-btn" data-el="redo"><svg viewBox="0 0 24 24"><path d="m15 14 5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/></svg>Redo</button>
                                    <button class="ac-btn ac-btn-danger-outline" data-el="clear"><svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>Clear</button>
                                </div>
                                <div class="ac-seg ze-shape ac-hidden" data-el="shape" title="The shape of the zone being edited">
                                    <button data-shape="polygon" title="Free corners: drag a corner or the zone, click to add a corner, double-click to remove one">Polygon</button>
                                    <button data-shape="square" title="Make it a rectangle: drag a corner to resize, the zone to move, the turn handle to rotate">Square</button>
                                </div>
                                <!-- One bordered group, as in the mockup: "Color: [swatch] | Draw Line" -->
                                <div class="ze-tools">
                                    <div class="ac-pill-wrap ze-color">
                                        Color:
                                        <button data-el="color" title="Change color"></button>
                                        <div class="ac-menu right ac-color-menu" data-el="color-menu" hidden style="top:auto;bottom:calc(100% + 6px);"></div>
                                    </div>
                                    <span class="ze-sep"></span>
                                    <button class="ze-draw" data-el="draw"><svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>Draw Line</button>
                                </div>
                            </div>
                        </div>
                        <div style="padding:24px;display:flex;flex-direction:column;gap:20px;">
                            <div data-el="side"></div>
                            <div data-el="edit-banner" class="ze-edit-banner ac-hidden"></div>
                            <button class="ac-btn ac-btn-dark ac-btn-lg" data-el="save" style="width:100%;">Save Polygon</button>
                            <button class="ac-btn ac-btn-lg ac-hidden" data-el="cancel-edit" style="width:100%;margin-top:-8px;">Cancel</button>
                            <div class="ze-saved">
                                <h3 class="ac-card-title" style="margin-bottom:14px;">Saved Polygon</h3>
                                <div data-el="list" style="display:grid;gap:10px;"></div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>`;

        const el = (name) => root.querySelector(`[data-el="${name}"]`);
        const canvas = el('canvas');

        // ---------- data ----------
        const netName = (c) => c.network_name || c.ip || 'Network';

        async function init() {
            try {
                const r = await fetch(`${SVC}/polygons`);
                if (r.status === 404 && r.headers.get('X-Svc-Missing')) return block(`The ${cfg.title} service is not on this machine. Please contact your administrator.`);
                // 502 (no answer), 503 (port table unreadable) or any other failure: there but not usable yet
                if (!r.ok) return block(`The ${cfg.title} service is not answering yet. Please try again in a moment.`);
                state.cameras = await fetch(`${API}/cameras/channels?status=1`).then(r => r.json());
            } catch (err) {
                console.error(err);
                return block('Could not load cameras. Please try again.');
            }
            el('loading').classList.add('ac-hidden');
            if (!state.cameras.length) {
                return block('No cameras selected yet. Choose cameras in Camera Network > Camera Selection first.');
            }
            el('main').classList.remove('ac-hidden');
            if (cfg.renderSide) cfg.renderSide(el('side'), sideApi);
            setType(state.type);
            setColor(state.color);
            selectCamera(state.cameras[0]);
        }

        function block(msg) {
            el('loading').classList.add('ac-hidden');
            const b = el('blocked');
            b.innerHTML = `<div class="ac-empty-icon"><svg viewBox="0 0 24 24"><rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10 5-3v10l-5-3"/></svg></div><h3>Nothing to draw yet</h3><p>${esc(msg)}</p>`;
            b.classList.remove('ac-hidden');
        }

        async function selectCamera(cam) {
            if (state.edit) endEdit();
            state.camera = cam;
            state.network = netName(cam);
            el('net-val').textContent = state.network;
            el('cam-val').textContent = `Camera ${cam.channel}`;
            el('card-title').textContent = `${state.network} - Camera ${cam.channel}`;
            // The frame every service processes, so a zone drawn on it matches what they test (ADR-0002).
            el('img').src = `${window.location.protocol}//${window.location.host}${appUrl(`api/svc/camera_service/frames/cam_${String(cam.ip).replace(/\./g, '_')}_${cam.channel}`)}`;
            resetDrawing();
            await Promise.all([loadPolygons(), cfg.onCamera ? cfg.onCamera(cam, sideApi) : null]);
        }

        async function loadPolygons() {
            try {
                const all = await fetch(`${SVC}/polygons`).then(r => r.json());
                state.polygons = all.filter(p => p.ip === state.camera.ip && Number(p.channel) === Number(state.camera.channel));
            } catch (err) {
                ToastManager.error('Could not load saved polygons');
                state.polygons = [];
            }
            renderList();
            render();
        }

        // ---------- menus ----------
        function toggleMenu(btn, menu, build) {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const open = menu.hidden;
                root.querySelectorAll('.ac-menu').forEach(m => { m.hidden = true; });
                if (open) { build(); menu.hidden = false; }
            });
        }
        toggleMenu(el('net-btn'), el('net-menu'), () => {
            const nets = [...new Set(state.cameras.map(netName))];
            el('net-menu').innerHTML = nets.map(n => `<button data-n="${esc(n)}" class="${n === state.network ? 'is-selected' : ''}">${esc(n)}</button>`).join('');
            el('net-menu').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
                el('net-menu').hidden = true;
                const cam = state.cameras.find(c => netName(c) === b.dataset.n);
                if (cam) leaveEdit('switching camera').then(ok => { if (ok) selectCamera(cam); });
            }));
        });
        toggleMenu(el('cam-btn'), el('cam-menu'), () => {
            const cams = state.cameras.filter(c => netName(c) === state.network);
            el('cam-menu').innerHTML = cams.map((c, i) => `<button data-i="${i}" class="${c === state.camera ? 'is-selected' : ''}">Camera ${c.channel}</button>`).join('');
            el('cam-menu').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
                el('cam-menu').hidden = true;
                const cam = cams[Number(b.dataset.i)];
                if (cam) leaveEdit('switching camera').then(ok => { if (ok) selectCamera(cam); });
            }));
        });
        toggleMenu(el('color'), el('color-menu'), () => {
            el('color-menu').innerHTML = COLORS.map(c => `<button data-c="${c}" class="ac-swatch${c === state.color ? ' is-active' : ''}" style="background:${c};"></button>`).join('');
            el('color-menu').querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
                el('color-menu').hidden = true;
                setColor(b.dataset.c);
            }));
        });

        function setColor(c) {
            state.color = c;
            el('color').style.background = c;
            render();
        }

        function setType(t) {
            state.type = t;
            root.querySelectorAll('[data-type]').forEach(b => b.classList.toggle('is-active', b.dataset.type === t));
            if (cfg.onType) cfg.onType(t, sideApi);
            updateControls();
        }
        root.querySelectorAll('[data-type]').forEach(b => b.addEventListener('click', () => setType(b.dataset.type)));

        // ---------- drawing ----------
        function resetDrawing() {
            state.points = []; state.redo = []; state.drawing = false;
            updateControls(); render();
        }

        el('draw').addEventListener('click', () => {
            state.drawing = !state.drawing;
            updateControls();
        });
        // Fast clicks are double clicks to the browser: keep them from selecting (painting blue) the picture.
        canvas.addEventListener('mousedown', (e) => { if (state.drawing || state.edit) e.preventDefault(); });
        canvas.addEventListener('click', (e) => {
            if (!state.drawing || state.edit) return;
            const r = canvas.getBoundingClientRect();
            state.points.push({ x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height });
            state.redo = [];
            updateControls(); render();
        });
        el('undo').addEventListener('click', () => {
            if (state.edit) { editUndo(); return; }
            if (!state.points.length) return;
            state.redo.unshift(state.points.pop());
            updateControls(); render();
        });
        el('redo').addEventListener('click', () => {
            if (!state.redo.length) return;
            state.points.push(state.redo.shift());
            updateControls(); render();
        });
        el('clear').addEventListener('click', resetDrawing);

        function missingRequirement() {
            if (state.edit) return state.edit.points.length < 3 ? 'A zone needs at least 3 corners' : (cfg.validate ? cfg.validate(state, sideApi) : null);
            if (state.points.length < 3) return 'Click Draw Line and add at least 3 points';
            return cfg.validate ? cfg.validate(state, sideApi) : null;
        }

        // Save Polygon also saves side-panel settings (e.g. speed thresholds), so it is
        // usable with no polygon drawn as long as those settings changed.
        const sideDirty = () => !!(cfg.sideDirty && cfg.sideDirty(sideApi));

        function updateControls() {
            const editing = !!state.edit;
            el('undo').disabled = editing ? !state.edit.history.length : !state.points.length;
            el('redo').disabled = editing || !state.redo.length;
            el('clear').disabled = editing || !state.points.length;
            el('draw').disabled = editing;
            el('draw').classList.toggle('is-active', state.drawing && !editing);
            el('shape').classList.toggle('ac-hidden', !editing);
            root.querySelectorAll('[data-shape]').forEach(b => b.classList.toggle('is-active', editing && b.dataset.shape === (state.edit.rect ? 'square' : 'polygon')));
            el('cancel-edit').classList.toggle('ac-hidden', !editing);
            el('save').textContent = editing ? 'Save Changes' : 'Save Polygon';
            el('edit-banner').classList.toggle('ac-hidden', !editing);
            if (editing) el('edit-banner').textContent = `Editing ${polygonName(state.edit.orig, state.polygons.indexOf(state.edit.orig))}. ` + (state.edit.rect
                ? 'Square: drag a corner to resize, the zone to move; drag the turn handle around to turn it.'
                : 'Polygon: drag a corner or the zone, click to add a corner, double-click to remove one.');
            if (editing) {
                const missing = missingRequirement();
                el('save').disabled = !!missing;
                el('save').title = missing || '';
                el('hint').classList.add('ac-hidden');
                canvas.style.cursor = state.edit.rect ? 'move' : 'crosshair';
                return;
            }
            const missing = missingRequirement();
            const canSave = !missing || (sideDirty() && !state.points.length);
            el('save').disabled = !canSave;
            el('save').title = canSave ? '' : missing;
            el('hint').classList.toggle('ac-hidden', !(state.drawing && state.points.length < 3));
            canvas.style.cursor = state.drawing ? 'crosshair' : 'default';
        }

        function render() {
            const svg = el('svg');
            const r = canvas.getBoundingClientRect();
            svg.innerHTML = '';
            el('handles').innerHTML = '';
            state.polygons.forEach((p, i) => {
                if (state.edit && String(p.id) === String(state.edit.id)) return;
                svg.appendChild(shape(p.points || [], p.color, true, r, polygonName(p, i)));
            });
            if (state.edit) { renderEdit(svg, r); return; }
            if (state.points.length) {
                svg.appendChild(shape(state.points, state.color, false, r));
                state.points.forEach(pt => {
                    const c = document.createElementNS(SVG_NS, 'circle');
                    c.setAttribute('cx', pt.x * r.width); c.setAttribute('cy', pt.y * r.height);
                    c.setAttribute('r', 5); c.setAttribute('fill', '#fff');
                    c.setAttribute('stroke', state.color); c.setAttribute('stroke-width', 2);
                    svg.appendChild(c);
                });
            }
        }

        function shape(points, color, closed, r, label) {
            const g = document.createElementNS(SVG_NS, 'g');
            if (!points.length) return g;
            const pts = points.map(p => [p.x * r.width, p.y * r.height]);
            const path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', pts.map((p, i) => `${i ? 'L' : 'M'}${p[0]} ${p[1]}`).join(' ') + (closed || pts.length >= 3 ? ' Z' : ''));
            path.setAttribute('fill', `${color}55`);
            path.setAttribute('stroke', color);
            path.setAttribute('stroke-width', 2.5);
            path.setAttribute('stroke-linejoin', 'round');
            g.appendChild(path);
            if (closed && label && pts.length >= 3) {
                const cx = pts.reduce((s, p) => s + p[0], 0) / pts.length;
                const cy = pts.reduce((s, p) => s + p[1], 0) / pts.length;
                const t = document.createElementNS(SVG_NS, 'text');
                t.setAttribute('x', cx); t.setAttribute('y', cy);
                t.setAttribute('text-anchor', 'middle'); t.setAttribute('dominant-baseline', 'central');
                t.setAttribute('fill', '#fff'); t.setAttribute('font-size', 13); t.setAttribute('font-weight', 600);
                t.setAttribute('style', 'paint-order:stroke;stroke:rgba(0,0,0,.7);stroke-width:3px;');
                t.textContent = label;
                g.appendChild(t);
            }
            return g;
        }

        function polygonName(p, i) {
            return cfg.label ? cfg.label(p, i) : `Polygon ${i + 1}`;
        }

        // ---------- saved list ----------
        function renderList() {
            const list = el('list');
            // cfg.listFilter narrows the list (e.g. one zone type); i stays the polygon's own index.
            const shown = state.polygons.map((p, i) => [p, i]).filter(([p]) => !cfg.listFilter || cfg.listFilter(p));
            const editingId = state.edit ? String(state.edit.id) : null;
            if (!shown.length) {
                list.innerHTML = '<p style="font-size:14px;color:#8a8a8a;">No polygons saved for this camera yet.</p>';
                return;
            }
            list.innerHTML = shown.map(([p, i]) => `
                <div class="ze-item${editingId === String(p.id) ? ' is-editing' : ''}">
                    <div style="flex:1;min-width:0;">
                        ${cfg.tag && cfg.tag(p) ? `<span class="ze-tag">${esc(cfg.tag(p))}</span>` : ''}
                        <div class="ze-name">
                            <span class="ze-swatch" style="background:${esc(p.color)};"></span>
                            <span>${esc(polygonName(p, i))}</span>
                            ${cfg.arrow && cfg.arrow(p) ? `<svg class="ze-arrow" viewBox="0 0 24 24"><path d="${cfg.arrow(p)}"/></svg>` : ''}
                        </div>
                    </div>
                    <div class="ac-pill-wrap">
                        <button class="ac-kebab" data-menu="${esc(p.id)}"><svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg></button>
                        <div class="ac-menu right" hidden style="min-width:140px;">
                            ${cfg.editable !== false ? `<button data-edit="${esc(p.id)}"><svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>Edit</button>` : ''}
                            <button class="danger" data-delete="${esc(p.id)}"><svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>Delete</button>
                        </div>
                    </div>
                </div>`).join('');
            list.querySelectorAll('[data-menu]').forEach(b => b.addEventListener('click', (e) => {
                e.stopPropagation();
                const m = b.nextElementSibling, open = m.hidden;
                root.querySelectorAll('.ac-menu').forEach(x => { x.hidden = true; });
                m.hidden = !open;
            }));
            list.querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', () => {
                b.closest('.ac-menu').hidden = true;
                if (state.edit && String(state.edit.id) === b.dataset.edit) return;   // already editing it
                leaveEdit('editing another zone').then(ok => {
                    if (!ok) return;
                    const p = state.polygons.find(x => String(x.id) === b.dataset.edit);
                    if (p) startEdit(p);
                });
            }));
            list.querySelectorAll('[data-delete]').forEach(b => b.addEventListener('click', () => {
                b.closest('.ac-menu').hidden = true;
                const idx = state.polygons.findIndex(p => String(p.id) === b.dataset.delete);
                confirmDelete(state.polygons[idx], idx);
            }));
        }

        function confirmDelete(p, i) {
            let modal = document.getElementById('ze-delete');
            if (!modal) {
                modal = document.createElement('div');
                modal.id = 'ze-delete';
                modal.className = 'ac-modal';
                modal.hidden = true;
                modal.innerHTML = `<div class="ac-modal-box"><div class="ac-modal-head"><h2 data-t></h2></div>
                    <div class="ac-modal-body"><p class="ac-modal-text">This polygon and its detection results for this camera will stop being used. This action cannot be undone.</p>
                    <div class="ac-modal-foot"><button class="ac-btn ac-btn-lg" data-cancel>Cancel</button><button class="ac-btn ac-btn-danger ac-btn-lg" data-ok>Delete</button></div></div></div>`;
                document.body.appendChild(modal);
                modal.querySelector('[data-cancel]').addEventListener('click', () => { modal.hidden = true; });
            }
            modal.querySelector('[data-t]').textContent = `Delete ${polygonName(p, i)}?`;
            const ok = modal.querySelector('[data-ok]');
            ok.onclick = async () => {
                ok.disabled = true;
                try {
                    const res = await fetch(`${SVC}/polygons/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    modal.hidden = true;
                    ToastManager.success('Polygon deleted');
                    await loadPolygons();
                } catch (err) {
                    ToastManager.error('Could not delete polygon', err.message);
                } finally { ok.disabled = false; }
            };
            modal.hidden = false;
        }

        // ---------- save ----------
        el('save').addEventListener('click', async () => {
            if (state.edit) { saveEdit(); return; }
            const polygonReady = !missingRequirement();
            if (!polygonReady && !(sideDirty() && !state.points.length)) return;
            el('save').disabled = true;
            if (sideDirty()) {
                try {
                    await cfg.saveSide(sideApi);
                } catch (err) {
                    ToastManager.error('Could not save settings', err.message);
                    updateControls();
                    return;
                }
                if (!polygonReady) {
                    ToastManager.success('Settings saved');
                    updateControls();
                    return;
                }
            }
            const payload = {
                id: (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()),
                service_name: cfg.serviceName,
                points: state.points,
                color: state.color,
                ip: state.camera.ip,
                channel: Number(state.camera.channel),
            };
            if (state.type != null) payload.type = state.type;
            if (cfg.extendPayload) cfg.extendPayload(payload, state, sideApi);
            try {
                const res = await fetch(`${SVC}/polygons`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                ToastManager.success('Polygon saved');
                if (cfg.afterSave) cfg.afterSave(sideApi);
                setColor(COLORS[(COLORS.indexOf(state.color) + 1) % COLORS.length]);
                resetDrawing();
                await loadPolygons();
            } catch (err) {
                ToastManager.error('Could not save polygon', err.message);
                updateControls();
            }
        });

        // ---------- edit (cfg.editable) ----------
        // A saved polygon is edited in place: the page fills its side fields from it
        // (cfg.onEditStart), the corners are moved on the picture as Polygon or Square, and
        // Save Changes writes it back under the same id.
        const ST = window.ShapeTools;
        const toPx = (pts, r) => pts.map(p => ({ x: p.x * r.width, y: p.y * r.height }));
        const fromPx = (pts, r) => pts.map(p => ({ x: p.x / r.width, y: p.y / r.height }));
        const clamp01 = (v) => Math.max(0, Math.min(1, v));
        const fits = (pts) => pts.every(p => p.x >= -1e-4 && p.y >= -1e-4 && p.x <= 1 + 1e-4 && p.y <= 1 + 1e-4);
        let drag = null;   // {kind: 'corner'|'move'|'rotate'|'click', idx, startPt, startY, base, basePx, moved}

        function startEdit(p) {
            if (!ST) return;
            if (state.edit) endEdit();
            state.points = []; state.redo = []; state.drawing = false;
            const pts = (p.points || []).map(q => ({ x: Number(q.x), y: Number(q.y) }));
            const r = canvas.getBoundingClientRect();
            state.edit = { id: p.id, orig: p, points: pts, rect: r.width > 10 && ST.isRect(toPx(pts, r)), history: [], color0: state.color, type0: state.type };
            setColor(p.color || state.color);
            // A page with types (Walk In / Walk Pass, Interest / Employee) shows the zone's own; it can be changed.
            if (cfg.types && p.type != null) setType(p.type);
            if (cfg.onEditStart) cfg.onEditStart(p, sideApi);
            state.edit.saved = editSnapshot();
            renderList(); updateControls(); render();
            canvas.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
        function endEdit() {
            const e = state.edit;
            state.edit = null; drag = null;
            if (e) { setColor(e.color0); if (cfg.types) setType(e.type0); }
            if (cfg.onEditEnd) cfg.onEditEnd(sideApi);
            renderList(); updateControls(); render();
        }
        el('cancel-edit').addEventListener('click', endEdit);
        const pushEdit = () => { state.edit.history.push({ points: state.edit.points.map(p => ({ ...p })), rect: state.edit.rect }); if (state.edit.history.length > 100) state.edit.history.shift(); };
        function editUndo() {
            const snap = state.edit && state.edit.history.pop();
            if (!snap) return;
            state.edit.points = snap.points; state.edit.rect = snap.rect;
            updateControls(); render();
        }
        // What the edit would save: corners, colour and the side panel's fields (name, duration...).
        const editSnapshot = () => JSON.stringify([state.edit.points, state.color, state.type,
            [...el('side').querySelectorAll('input, select, textarea')].map(i => i.value),
            cfg.editExtra ? cfg.editExtra() : null]);
        const editDirty = () => !!state.edit && editSnapshot() !== state.edit.saved;
        // Before leaving a zone being edited (Edit on another zone, another camera): with changes
        // not saved it asks Save / go on without saving / stay. Resolves true to go on.
        async function leaveEdit(what) {
            if (!editDirty()) return true;
            const a = await ST.askSaveFirst({
                title: 'Save your changes first?',
                text: `The zone you are editing has changes that are not saved yet. Save them before ${what}?`,
                saveLabel: 'Save and continue', skipLabel: 'Continue without saving' });
            if (a === 'cancel') return false;
            if (a === 'save') return await saveEdit();
            return true;
        }
        // Polygon / Square; with changes not saved it asks first: Save (then switch), switch anyway, or stay.
        root.querySelectorAll('[data-shape]').forEach(b => b.addEventListener('click', async () => {
            if (!state.edit) return;
            const square = b.dataset.shape === 'square';
            if (state.edit.rect === square) return;
            if (editDirty()) {
                const id = state.edit.id;
                const a = await ST.askSaveFirst({
                    title: 'Save before switching?',
                    text: `This zone has changes that are not saved yet. Save them before switching to ${square ? 'Square' : 'Polygon'}?`,
                    saveLabel: 'Save and switch', skipLabel: 'Switch without saving' });
                if (a === 'cancel' || !state.edit) return;
                if (a === 'save') {
                    if (!(await saveEdit())) return;
                    const again = state.polygons.find(x => String(x.id) === String(id));
                    if (!again) return;
                    startEdit(again);
                    if (state.edit.rect === square) return;
                }
            }
            switchShape(square);
        }));
        function switchShape(square) {
            const r = canvas.getBoundingClientRect();
            pushEdit();
            if (square) {
                const px = toPx(state.edit.points, r);
                if (!ST.isRect(px)) state.edit.points = fromPx(ST.box(px), r).map(p => ({ x: clamp01(p.x), y: clamp01(p.y) }));
            }
            state.edit.rect = square;
            updateControls(); render();
        }

        function renderEdit(svg, r) {
            const e = state.edit, pts = e.points;
            svg.appendChild(shape(pts, state.color, true, r, polygonName(e.orig, state.polygons.indexOf(e.orig))));
            const layer = el('handles');
            pts.forEach((p, i) => {
                const d = document.createElement('div');
                d.className = 'ze-corner';
                d.style.left = `${p.x * r.width}px`; d.style.top = `${p.y * r.height}px`;
                d.style.borderColor = state.color;
                d.title = e.rect ? 'Drag to resize' : 'Drag to move this corner · double-click to remove it';
                d.addEventListener('pointerdown', (ev) => { ev.stopPropagation(); ev.preventDefault(); beginDrag(ev, 'corner', i); });
                d.addEventListener('click', (ev) => ev.stopPropagation());
                if (!e.rect) d.addEventListener('dblclick', (ev) => { ev.stopPropagation(); removeCorner(i); });
                layer.appendChild(d);
            });
            if (e.rect) {
                const at = ST.handleAt(toPx(pts, r), 18, r.width, r.height);
                const h = document.createElement('div');
                h.className = 'ze-rot';
                h.style.left = `${at.x}px`; h.style.top = `${at.y}px`;
                h.style.borderColor = state.color; h.style.color = state.color;
                h.title = 'Drag around to turn the zone';
                h.innerHTML = ST.ICON_ROTATE;
                h.addEventListener('pointerdown', (ev) => { ev.stopPropagation(); ev.preventDefault(); beginDrag(ev, 'rotate', -1); });
                h.addEventListener('click', (ev) => ev.stopPropagation());
                layer.appendChild(h);
            }
        }
        const ptOf = (ev, r) => ({ x: (ev.clientX - r.left) / r.width, y: (ev.clientY - r.top) / r.height });
        function beginDrag(ev, kind, idx) {
            const r = canvas.getBoundingClientRect();
            const base = state.edit.points.map(p => ({ ...p }));
            drag = { kind, idx, startPt: ptOf(ev, r), startY: ev.clientY, base, basePx: toPx(base, r), moved: false };
        }
        canvas.addEventListener('pointerdown', (ev) => {
            if (!state.edit) return;
            const r = canvas.getBoundingClientRect(), pt = ptOf(ev, r);
            if (pt.x < 0 || pt.x > 1 || pt.y < 0 || pt.y > 1) return;
            ev.preventDefault();
            if (ST.inside(toPx([pt], r)[0], toPx(state.edit.points, r))) beginDrag(ev, 'move', -1);
            else if (!state.edit.rect) drag = { kind: 'click', startPt: pt, startY: ev.clientY, moved: false };
        });
        window.addEventListener('pointermove', (ev) => {
            if (!drag || !state.edit) return;
            const r = canvas.getBoundingClientRect(), pt = ptOf(ev, r);
            const dx = pt.x - drag.startPt.x, dy = pt.y - drag.startPt.y;
            if (!drag.moved && Math.hypot(dx * r.width, dy * r.height) < 2 && Math.abs(ev.clientY - drag.startY) < 2) return;
            if (drag.kind === 'click') { drag.moved = true; return; }
            if (!drag.moved) { pushEdit(); drag.moved = true; }
            const e = state.edit, base = drag.base;
            const square = e.rect && base.length === 4;
            let next = null;
            if (drag.kind === 'rotate' && square) {
                // the rectangle turns about its middle to follow the pointer
                // ...moved back inside the picture when a corner would leave it
                const turned = ST.nudgeInside(ST.rotate(drag.basePx, ST.turnAngle(drag.basePx, toPx([drag.startPt], r)[0], toPx([pt], r)[0])), r.width, r.height);
                if (turned) next = fromPx(turned, r);
            } else if (drag.kind === 'corner' && square) {
                const q = toPx([{ x: clamp01(base[drag.idx].x + dx), y: clamp01(base[drag.idx].y + dy) }], r)[0];
                next = fromPx(ST.resize(drag.basePx, drag.idx, q), r);
            } else if (drag.kind === 'corner') {
                next = base.map((p, i) => i === drag.idx ? { x: clamp01(p.x + dx), y: clamp01(p.y + dy) } : { ...p });
            } else if (drag.kind === 'move') {
                // the whole zone stays inside the picture instead of being squashed at the edge
                const minX = Math.min(...base.map(p => p.x)), maxX = Math.max(...base.map(p => p.x));
                const minY = Math.min(...base.map(p => p.y)), maxY = Math.max(...base.map(p => p.y));
                const cx = Math.max(-minX, Math.min(1 - maxX, dx)), cy = Math.max(-minY, Math.min(1 - maxY, dy));
                next = base.map(p => ({ x: p.x + cx, y: p.y + cy }));
            }
            if (!next || (square && !fits(next))) return;   // a square leaving the picture stops instead of bending
            e.points = next;
            updateControls(); render();
        });
        window.addEventListener('pointerup', () => {
            if (!drag || !state.edit) { drag = null; return; }
            const d = drag; drag = null;
            if (d.kind === 'click' && !d.moved) addCorner(d.startPt);
        });
        function addCorner(pt) {
            const e = state.edit;
            if (!e || e.rect) return;
            if (e.points.length >= 64) { ToastManager.error('A zone has at most 64 corners'); return; }
            const r = canvas.getBoundingClientRect();
            pushEdit();
            e.points = fromPx(ST.insertAt(toPx(e.points, r), toPx([{ x: clamp01(pt.x), y: clamp01(pt.y) }], r)[0]), r);
            updateControls(); render();
        }
        function removeCorner(i) {
            const e = state.edit;
            if (!e || e.rect) return;
            if (e.points.length <= 3) { ToastManager.error('A zone needs at least 3 corners'); return; }
            pushEdit();
            e.points = e.points.filter((_, k) => k !== i);
            updateControls(); render();
        }

        async function saveEdit() {
            const e = state.edit;
            if (!e || missingRequirement()) return false;
            const orig = e.orig;
            const payload = {
                id: orig.id, service_name: cfg.serviceName,
                points: e.points.map(p => ({ x: Math.round(clamp01(p.x) * 1e6) / 1e6, y: Math.round(clamp01(p.y) * 1e6) / 1e6 })),
                color: state.color, ip: orig.ip, channel: Number(orig.channel),
            };
            if (cfg.types) payload.type = state.type;
            else if (orig.type != null) payload.type = orig.type;
            if (cfg.extendPayload) cfg.extendPayload(payload, state, sideApi);
            el('save').disabled = true;
            try {
                // the side panel's own settings (e.g. speed thresholds) changed too: saved with it
                if (sideDirty()) await cfg.saveSide(sideApi);
                await updatePolygon(orig, payload);
                ToastManager.success('Polygon updated');
                endEdit();
                await loadPolygons();
                return true;
            } catch (err) {
                ToastManager.error('Could not save the changes', err.message);
                updateControls();
                return false;
            }
        }
        // PUT /polygons/{id}; a service from before that route answers 405 or FastAPI's own
        // 404 "Not Found": then the polygon is saved again under the same id (delete, create),
        // and put back as it was if the new one is refused.
        async function updatePolygon(orig, payload) {
            const url = `${SVC}/polygons/${encodeURIComponent(orig.id)}`;
            const { id, service_name, ip, channel, ...body } = payload;
            const res = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            if (res.ok) return;
            const j = await res.json().catch(() => ({}));
            if (!(res.status === 405 || (res.status === 404 && j.detail === 'Not Found'))) {
                throw new Error(res.status === 404 ? 'This polygon is no longer saved' : (j.detail || `HTTP ${res.status}`));
            }
            const del = await fetch(url, { method: 'DELETE' });
            if (!del.ok) throw new Error(`HTTP ${del.status}`);
            const post = (b) => fetch(`${SVC}/polygons`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
            const created = await post(payload);
            if (!created.ok) {
                const back = { ...orig, service_name: cfg.serviceName };
                await post(back).catch(() => {});
                throw new Error(`HTTP ${created.status}`);
            }
        }

        // The side panel's settings live in the same DLC service (cfg.svc).
        const sideApi = { state, api: SVC, refresh: updateControls, renderList, el, editing: () => !!state.edit };
        window.addEventListener('resize', render);
        new ResizeObserver(render).observe(canvas);
        init();
    }

    window.ZoneEditor = { mount };
})();
