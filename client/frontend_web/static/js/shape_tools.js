// Shape editing shared by Calibrate Hot Zone (floor plan) and Zone Area (camera picture):
// Polygon (free corners) and Square (a rectangle, any angle). Every function works on
// points in screen pixels ({x, y}), so a rectangle stays square-cornered on screen
// whatever the picture's proportions; each page converts its own units in and out.
(function () {
    const MIN_SIDE = 4;   // px: a rectangle never collapses (its sides keep their direction)

    const centroid = (pts) => ({
        x: pts.reduce((s, p) => s + p.x, 0) / pts.length,
        y: pts.reduce((s, p) => s + p.y, 0) / pts.length,
    });

    function inside(pt, poly) {
        let hit = false;
        for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
            const a = poly[i], b = poly[j];
            if (((a.y > pt.y) !== (b.y > pt.y)) && (pt.x < (b.x - a.x) * (pt.y - a.y) / (b.y - a.y) + a.x)) hit = !hit;
        }
        return hit;
    }

    // The upright rectangle around the shape: top-left, top-right, bottom-right, bottom-left.
    function box(pts) {
        const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
        let x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
        if (x1 - x0 < MIN_SIDE) x1 = x0 + MIN_SIDE;
        if (y1 - y0 < MIN_SIDE) y1 = y0 + MIN_SIDE;
        return [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }];
    }

    // Four corners with square corners (any angle), within tolDeg.
    function isRect(pts, tolDeg = 2) {
        if (!pts || pts.length !== 4) return false;
        const cos = Math.cos((90 - tolDeg) * Math.PI / 180);
        for (let i = 0; i < 4; i++) {
            const a = pts[(i + 3) % 4], b = pts[i], c = pts[(i + 1) % 4];
            const u = { x: a.x - b.x, y: a.y - b.y }, v = { x: c.x - b.x, y: c.y - b.y };
            const lu = Math.hypot(u.x, u.y), lv = Math.hypot(v.x, v.y);
            if (lu < 1 || lv < 1) return false;
            if (Math.abs((u.x * v.x + u.y * v.y) / (lu * lv)) > cos) return false;
        }
        return true;
    }

    const unit = (v) => { const l = Math.hypot(v.x, v.y); return l > 1e-6 ? { x: v.x / l, y: v.y / l } : null; };

    // Square: corner i goes to q; the corner across stays, the two beside it follow so the
    // shape stays a rectangle at the same angle.
    function resize(base, i, q) {
        const o = base[(i + 2) % 4], n1 = base[(i + 1) % 4], n3 = base[(i + 3) % 4];
        let A = unit({ x: n1.x - o.x, y: n1.y - o.y }), B = unit({ x: n3.x - o.x, y: n3.y - o.y });
        if (!A && !B) { A = { x: 1, y: 0 }; B = { x: 0, y: 1 }; }
        else if (!A) A = { x: B.y, y: -B.x };
        else if (!B) B = { x: -A.y, y: A.x };
        const d = { x: q.x - o.x, y: q.y - o.y };
        let a = d.x * A.x + d.y * A.y, b = d.x * B.x + d.y * B.y;
        if (Math.abs(a) < MIN_SIDE) a = a < 0 ? -MIN_SIDE : MIN_SIDE;
        if (Math.abs(b) < MIN_SIDE) b = b < 0 ? -MIN_SIDE : MIN_SIDE;
        const out = base.map(p => ({ x: p.x, y: p.y }));
        out[(i + 1) % 4] = { x: o.x + A.x * a, y: o.y + A.y * a };
        out[(i + 3) % 4] = { x: o.x + B.x * b, y: o.y + B.y * b };
        out[i] = { x: o.x + A.x * a + B.x * b, y: o.y + A.y * a + B.y * b };
        return out;
    }

    function rotate(pts, rad, about) {
        const c = about || centroid(pts), s = Math.sin(rad), k = Math.cos(rad);
        return pts.map(p => ({ x: c.x + (p.x - c.x) * k - (p.y - c.y) * s, y: c.y + (p.x - c.x) * s + (p.y - c.y) * k }));
    }

    // Where a new corner at p goes: after the corner whose side is nearest.
    function insertAt(pts, p) {
        let best = 0, bestD = Infinity;
        pts.forEach((a, i) => {
            const b = pts[(i + 1) % pts.length];
            const dx = b.x - a.x, dy = b.y - a.y, L = dx * dx + dy * dy;
            const t = L ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / L)) : 0;
            const d = Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
            if (d < bestD) { bestD = d; best = i; }
        });
        return [...pts.slice(0, best + 1), { x: p.x, y: p.y }, ...pts.slice(best + 1)];
    }

    // Where the turn handle sits: beyond the middle of the rectangle's right side (its side
    // from corner 0 to 1 points "right"), so it turns with the rectangle; on the other side
    // when that would be off a picture W x H pixels.
    function handleAt(pts, gap = 18, W = Infinity, H = Infinity) {
        const c = centroid(pts);
        let u = pts.length >= 2 ? unit({ x: pts[1].x - pts[0].x, y: pts[1].y - pts[0].y }) : null;
        if (!u) u = { x: 1, y: 0 };
        const half = pts.length >= 2 ? Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y) / 2 : 0;
        const at = (k) => ({ x: c.x + k * u.x * (half + gap), y: c.y + k * u.y * (half + gap) });
        const ok = (q) => q.x >= 13 && q.y >= 13 && q.x <= W - 13 && q.y <= H - 13;
        const right = at(1);
        return ok(right) || !ok(at(-1)) ? right : at(-1);
    }

    // The shape moved just enough to be inside a W x H picture, or null when it is too big.
    function nudgeInside(pts, W, H) {
        const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
        const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
        if (x1 - x0 > W + 1e-6 || y1 - y0 > H + 1e-6) return null;
        const dx = x0 < 0 ? -x0 : x1 > W ? W - x1 : 0, dy = y0 < 0 ? -y0 : y1 > H ? H - y1 : 0;
        return pts.map(p => ({ x: p.x + dx, y: p.y + dy }));
    }

    const within = (pts, W, H) => pts.every(p => p.x >= -0.5 && p.y >= -0.5 && p.x <= W + 0.5 && p.y <= H + 0.5);

    // The turn the handle makes about the rectangle's middle, from where it was pressed (start)
    // to where the pointer is now (now), in radians: the rectangle follows the pointer around.
    function turnAngle(pts, start, now) {
        const c = centroid(pts);
        return Math.atan2(now.y - c.y, now.x - c.x) - Math.atan2(start.y - c.y, start.x - c.x);
    }

    // "Save first?" before something that would change what is being edited (e.g. switching
    // Polygon / Square). Resolves 'save', 'skip' (go on without saving) or 'cancel'.
    function askSaveFirst({ title = 'Save your changes first?', text, saveLabel = 'Save', skipLabel = 'Continue without saving' } = {}) {
        let m = document.getElementById('st-save-first');
        if (!m) {
            m = document.createElement('div');
            m.id = 'st-save-first'; m.className = 'ac-modal'; m.hidden = true;
            m.innerHTML = `<div class="ac-modal-box"><div class="ac-modal-head"><h2 data-t></h2></div>
                <div class="ac-modal-body"><p class="ac-modal-text" data-x></p>
                <div class="ac-modal-foot"><button type="button" class="ac-btn ac-btn-lg" data-a="cancel">Cancel</button>
                <button type="button" class="ac-btn ac-btn-lg" data-a="skip"></button>
                <button type="button" class="ac-btn ac-btn-dark ac-btn-lg" data-a="save"></button></div></div></div>`;
            document.body.appendChild(m);
        }
        m.querySelector('[data-t]').textContent = title;
        m.querySelector('[data-x]').textContent = text || 'There are changes that are not saved yet.';
        m.querySelector('[data-a="save"]').textContent = saveLabel;
        m.querySelector('[data-a="skip"]').textContent = skipLabel;
        return new Promise((resolve) => {
            const done = (v) => { m.hidden = true; m.removeEventListener('click', onClick); obs.disconnect(); resolve(v); };
            const onClick = (e) => { const b = e.target.closest('[data-a]'); if (b) done(b.dataset.a); };
            m.addEventListener('click', onClick);
            // closed some other way (Esc, a click outside): as Cancel
            const obs = new MutationObserver(() => { if (m.hidden) done('cancel'); });
            m.hidden = false;
            obs.observe(m, { attributes: true, attributeFilter: ['hidden'] });
        });
    }

    const ICON_ROTATE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>';

    window.ShapeTools = { centroid, inside, box, isRect, resize, rotate, insertAt, handleAt, within, nudgeInside, turnAngle, askSaveFirst, ICON_ROTATE, MIN_SIDE };
})();
