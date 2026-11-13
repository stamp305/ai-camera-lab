/**
 * app.js - Core application logic for the Python frontend
 * Handles: API config, toast notifications, sidebar toggle, auth guards
 */

// ============================================================
// API Configuration
// ============================================================
const API_BASE_URL = (() => {
    // Always derive the API URL from the browser's current hostname.
    // This ensures that when a branch machine accesses the frontend via its
    // server IP (e.g. 192.168.2.240:3000), API calls also go to that same IP
    // on port 8000, regardless of what the .env / meta tag says.
    // The meta tag may contain a stale IP from a different subnet.
    const meta = document.querySelector('meta[name="api-base-url"]');
    const metaUrl = meta ? meta.content : '';

    // Extract port from meta tag if available (default 8000)
    let apiPort = '8000';
    if (metaUrl) {
        try {
            const parsed = new URL(metaUrl);
            if (parsed.port) apiPort = parsed.port;
        } catch (_) { /* ignore parse errors */ }
    }

    // หมายเหตุ: ค่านี้เป็น absolute + พอร์ต 8000 ใช้ผ่านรีโมตไม่ได้ (ท่อยอมให้ถึงแค่
    // 127.0.0.1:3000) ตอนนี้ไม่มีที่ไหนเรียกใช้ ถ้าจะใช้ต้องเปลี่ยนไปทาง getApiUrl()
    return `${window.location.protocol}//${window.location.hostname}:${apiPort}`;
})();

// ============================================================
// Remote browser support
// ============================================================
// เวลาแอดมินเปิดหน้านี้ผ่านรีโมต หน้าเว็บจะอยู่ใต้ /ai-camera/remote/<activateID>/
// ทุก path ที่เขียนเป็น /xxx ต้องถูกดึงเข้ามาอยู่ใต้ prefix นั้น ไม่งั้นเบราว์เซอร์
// จะไปเรียกตัวแอดมินเองแทนที่จะเป็นเครื่องสาขา ตัว proxy แปลง href/src ใน HTML ให้
// แล้ว เหลือที่ JS สร้าง URL เองตอนรัน ต้องใช้ appUrl() ตรงนี้
const REMOTE_PREFIX = (() => {
    const m = window.location.pathname.match(/^\/ai-camera\/remote\/[^/]+\//);
    return m ? m[0] : '/';
})();

function appUrl(path) {
    return REMOTE_PREFIX + String(path == null ? '' : path).replace(/^\//, '');
}

// ดัก fetch ให้เติม prefix เองทั้งไฟล์ จะได้ไม่ต้องไล่แก้ทุกจุดเรียก
(() => {
    if (REMOTE_PREFIX === '/') return;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
        // ห้ามเติมซ้ำ ถ้า URL ถูกเติม prefix มาแล้ว (เช่นที่ fsUrl/appUrl สร้างเอง)
        // ไม่งั้นได้ /ai-camera/remote/<id>/ai-camera/remote/<id>/... = 404
        if (typeof input === 'string' && input.startsWith('/') && !input.startsWith('//')
            && !input.startsWith(REMOTE_PREFIX)) {
            input = appUrl(input);
        }
        return nativeFetch(input, init);
    };
})();

function getApiUrl() {
    return `${window.location.protocol}//${window.location.host}${appUrl('api/proxy')}`;
}

// ============================================================
// แถบเตือน "กำลังถูกรีโมต" สำหรับคนหน้าร้าน
// ============================================================
// ขึ้นเฉพาะตอนเปิดหน้านี้บนเครื่องสาขาเอง ไม่ต้องขึ้นในจอของแอดมินที่รีโมตเข้ามา
(() => {
    if (REMOTE_PREFIX !== '/') return;

    const STATUS_URL = 'http://127.0.0.1:8210/remote-status';
    let banner = null;

    function ensureBanner() {
        if (banner) return banner;
        banner = document.createElement('div');
        banner.id = 'remote-banner';
        banner.textContent = '🔴 ผู้ดูแลระบบกำลังเข้าดูหน้าจอเครื่องนี้อยู่';
        banner.style.cssText = [
            'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:9999',
            'background:#dc2626', 'color:#fff', 'text-align:center',
            'padding:6px 12px', 'font-size:13px', 'font-weight:600',
            'box-shadow:0 2px 6px rgba(0,0,0,.2)',
        ].join(';');
        document.body.appendChild(banner);
        return banner;
    }

    function markSidebar(active) {
        const nav = document.querySelector('.sidebar, aside, nav');
        if (!nav) return;
        let tag = document.getElementById('remote-sidebar-tag');
        if (active && !tag) {
            tag = document.createElement('div');
            tag.id = 'remote-sidebar-tag';
            tag.textContent = '🔴 กำลังถูกรีโมต';
            tag.style.cssText = 'margin:8px; padding:6px 8px; border-radius:6px;'
                + 'background:#fee2e2; color:#b91c1c; font-size:12px; font-weight:600;';
            nav.prepend(tag);
        } else if (!active && tag) {
            tag.remove();
        }
    }

    async function poll() {
        try {
            const res = await fetch(STATUS_URL, { cache: 'no-store' });
            const data = await res.json();
            const active = !!data.active;
            ensureBanner().style.display = active ? 'block' : 'none';
            document.body.style.paddingTop = active ? '30px' : '';
            markSidebar(active);
        } catch (e) {
            // ตัวแทนสถานะไม่ทำงาน (client_logic ยังไม่ขึ้น) — ไม่ต้องรบกวนผู้ใช้
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        poll();
        setInterval(poll, 3000);
    });
})();

// ============================================================
// เลือกไฟล์จาก "เครื่องสาขา" ตอนใช้งานผ่านรีโมต
// ============================================================
// ปกติ <input type="file"> จะเปิด dialog อ่านดิสก์ของเครื่องที่เปิดเบราว์เซอร์อยู่
// ซึ่งตอนรีโมตคือเครื่องแอดมิน ไม่ใช่เครื่องสาขา — ผิดความตั้งใจของงานเกือบทุกกรณี
// (เช่นอัปโหลดรูปยูนิฟอร์มที่ถ่ายไว้ที่ร้าน) จึงดักคลิกแล้วเปิดตัวเลือกไฟล์ของ
// เครื่องสาขาผ่านท่อ /__fs/ แทน
(() => {
    if (REMOTE_PREFIX === '/') return;

    let targetInput = null;
    let overlay = null;

    function fsUrl(op, path) {
        const q = path ? `?path=${encodeURIComponent(path)}` : '';
        return appUrl(`__fs/${op}${q}`);
    }

    function buildOverlay() {
        overlay = document.createElement('div');
        overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.45);'
            + 'display:flex;align-items:center;justify-content:center;';
        overlay.innerHTML = `
            <div style="background:#fff;border-radius:10px;width:min(560px,92vw);max-height:76vh;
                        display:flex;flex-direction:column;overflow:hidden;font-size:13px;">
              <div style="padding:10px 14px;border-bottom:1px solid #eee;display:flex;gap:8px;align-items:center;">
                <strong>เลือกไฟล์จากเครื่องสาขา</strong>
                <span id="rfs-path" style="color:#6b7280;font-size:11px;flex:1;"></span>
                <button id="rfs-up" style="border:1px solid #e5e7eb;background:#fff;border-radius:6px;padding:2px 8px;cursor:pointer;">ขึ้นบน</button>
                <button id="rfs-close" style="border:none;background:#f3f4f6;border-radius:6px;padding:2px 8px;cursor:pointer;">ปิด</button>
              </div>
              <div id="rfs-list" style="padding:10px 14px;overflow:auto;"></div>
            </div>`;
        document.body.appendChild(overlay);
        overlay.querySelector('#rfs-close').addEventListener('click', close);
        overlay.querySelector('#rfs-up').addEventListener('click', () => {
            const cur = overlay.dataset.path;
            if (cur) browse(cur.replace(/\/[^/]+$/, '') || null);
        });
        return overlay;
    }

    function close() {
        if (overlay) { overlay.remove(); overlay = null; }
        targetInput = null;
    }

    async function browse(path) {
        const box = overlay.querySelector('#rfs-list');
        box.textContent = 'กำลังโหลด...';

        let data;
        try {
            const res = await fetch(path ? fsUrl('list', path) : fsUrl('roots'));
            if (!res.ok) throw new Error(`เครื่องสาขาตอบ ${res.status}`);
            data = await res.json();
        } catch (err) {
            // ไม่งั้นค้างอยู่ที่ "กำลังโหลด..." โดยไม่บอกว่าเกิดอะไรขึ้น
            box.innerHTML = `<div style="color:#dc2626;">เปิดโฟลเดอร์ไม่สำเร็จ: ${err.message || err}</div>`;
            return;
        }

        if (data.error) { box.innerHTML = `<div style="color:#dc2626;">${data.error}</div>`; return; }

        const roots = data.media_roots || data.roots;
        const rows = roots
            ? roots.map(r => ({ name: r.split('/').pop(), path: r, is_dir: true, size: 0 }))
            : data.items;
        overlay.dataset.path = data.path || '';
        overlay.querySelector('#rfs-path').textContent = data.path || 'เลือกโฟลเดอร์';
        // อยู่รากแล้ว ไม่มีอะไรให้ขึ้นไปอีก
        overlay.querySelector('#rfs-up').hidden = !data.path;

        if (!rows.length) {
            box.innerHTML = '<div style="color:#9ca3af;">โฟลเดอร์ว่าง</div>';
            return;
        }

        // รูปโชว์เป็นตัวอย่างภาพเลย จะได้ไม่ต้องเดาจากชื่อไฟล์ว่าอันไหนคืออันไหน
        // (โหลดแบบ lazy ผ่านท่อเดิม เลื่อนถึงค่อยดึง)
        const isImage = (n) => /\.(jpe?g|png|gif|webp|bmp|heic)$/i.test(n);
        const dirs = rows.filter(i => i.is_dir);
        const images = rows.filter(i => !i.is_dir && isImage(i.name));
        const others = rows.filter(i => !i.is_dir && !isImage(i.name));

        const dirHtml = dirs.map(i => `<div style="padding:3px 0;">
                 <button data-path="${i.path}" data-dir="true"
                   style="background:none;border:none;cursor:pointer;color:#2563eb;">
                   📁 ${i.name}</button></div>`).join('');

        const imgHtml = images.length ? `<div style="display:grid;
                 grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:8px;margin:8px 0;">`
            + images.map(i => `<button data-path="${i.path}" data-dir="false" title="${i.name}"
                   style="background:none;border:1px solid #e5e7eb;border-radius:8px;padding:4px;
                          cursor:pointer;display:flex;flex-direction:column;gap:4px;align-items:center;">
                   <img loading="lazy" src="${fsUrl('get', i.path)}" alt="${i.name}"
                        style="width:100%;height:78px;object-fit:cover;border-radius:5px;background:#f3f4f6;">
                   <span style="font-size:11px;word-break:break-all;line-height:1.2;">${i.name}</span>
                 </button>`).join('') + '</div>' : '';

        const otherHtml = others.map(i => `<div style="padding:3px 0;">
                 <button data-path="${i.path}" data-dir="false"
                   style="background:none;border:none;cursor:pointer;color:#374151;">
                   📄 ${i.name}</button>
                 <span style="color:#9ca3af;">(${i.size.toLocaleString()} ไบต์)</span></div>`).join('');

        box.innerHTML = dirHtml + imgHtml + otherHtml;

        box.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
            if (b.dataset.dir === 'true') browse(b.dataset.path);
            else pick(b.dataset.path, (b.title || b.textContent).replace(/^📄\s*/, '').trim());
        }));
    }

    async function pick(path, name) {
        const input = targetInput;
        const box = overlay.querySelector('#rfs-list');
        box.textContent = 'กำลังดึงไฟล์จากเครื่องสาขา...';
        try {
            const res = await fetch(fsUrl('get', path));
            if (!res.ok) throw new Error(await res.text());
            const blob = await res.blob();
            // ยัดไฟล์เข้า input เดิม ฟอร์มเดิมจึงส่งได้โดยไม่ต้องแก้หน้าเว็บแต่ละหน้า
            const dt = new DataTransfer();
            dt.items.add(new File([blob], name, { type: blob.type }));
            input.files = dt.files;
            input.dispatchEvent(new Event('change', { bubbles: true }));
            close();
        } catch (err) {
            box.innerHTML = `<div style="color:#dc2626;">ดึงไฟล์ไม่สำเร็จ: ${err}</div>`;
        }
    }

    document.addEventListener('click', (e) => {
        const input = e.target.closest('input[type="file"]');
        if (!input) return;
        e.preventDefault();
        e.stopPropagation();
        targetInput = input;
        buildOverlay();
        browse(null);
    }, true);
})();

// ============================================================
// กันกดรัว / กดซ้ำ
// ============================================================
// ปุ่มบันทึกทั้งหลาย (ล็อกอิน, บันทึกข้อมูลร้าน, บันทึกโซน, เพิ่มกล้อง) แต่ละหน้า
// ป้องกันการกดซ้ำกันเองคนละแบบ บางหน้าไม่ป้องกันเลย กดรัว ๆ จึงยิง request ซ้ำ
// ได้หลายครั้ง เกิดข้อมูลซ้ำหรือชนกันเอง โดยเฉพาะตอนใช้ผ่านรีโมตที่หน่วงกว่าปกติ
// คนกดจึงเผลอกดซ้ำเพราะคิดว่าไม่ติด
//
// ตรงนี้ดักที่ระดับกลาง: กดซ้ำระหว่างที่ยังทำงานอยู่ = ไม่ส่งซ้ำ แต่ไม่ได้ห้ามกด
// ปุ่มอื่น และไม่แตะตรรกะของแต่ละหน้า
(() => {
    const COOLDOWN_MS = 1200;      // กดซ้ำเร็วกว่านี้ถือว่ากดรัว
    const MAX_HOLD_MS = 15000;     // กันปุ่มค้างถาวรถ้า request ไม่จบ
    const busy = new WeakSet();

    let inFlight = 0;
    const waiters = [];
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (...args) => {
        inFlight += 1;
        return nativeFetch(...args).finally(() => {
            inFlight -= 1;
            if (inFlight === 0) {
                while (waiters.length) waiters.shift()();
            }
        });
    };

    function release(btn) {
        busy.delete(btn);
        btn.removeAttribute('aria-busy');
        btn.style.removeProperty('cursor');
    }

    document.addEventListener('click', (e) => {
        const btn = e.target.closest('button, input[type="submit"], .btn');
        if (!btn || btn.disabled || btn.dataset.allowRapid === 'true') return;

        if (busy.has(btn)) {
            // ยังทำงานค้างอยู่ — กลืนคลิกนี้ทิ้ง ไม่ให้ handler ของหน้าทำงานซ้ำ
            e.preventDefault();
            e.stopImmediatePropagation();
            return;
        }

        busy.add(btn);
        btn.setAttribute('aria-busy', 'true');
        btn.style.cursor = 'progress';

        // ปลดล็อกเมื่อ request ที่เกิดจากคลิกนี้จบ หรืออย่างช้าที่สุดตาม COOLDOWN
        const started = inFlight;
        setTimeout(() => {
            if (inFlight > started || inFlight > 0) {
                // คลิกนี้ยิง request จริง — ล็อกไว้จนกว่าจะได้คำตอบ
                waiters.push(() => release(btn));
                setTimeout(() => release(btn), MAX_HOLD_MS);
            } else {
                // ปุ่มที่ไม่ได้ยิง request (เปิดเมนู, สลับแท็บ, เลือกสี) ปล่อยทันที
                // ไม่งั้นกดรัว ๆ ตามปกติจะสะดุด
                release(btn);
            }
        }, 50);
    }, true);

    // ฟอร์ม (หน้าล็อกอินใช้ submit ไม่ใช่ click) กัน Enter รัว ๆ ด้วย
    document.addEventListener('submit', (e) => {
        const form = e.target;
        if (busy.has(form)) {
            e.preventDefault();
            e.stopImmediatePropagation();
            return;
        }
        busy.add(form);
        setTimeout(() => busy.delete(form), COOLDOWN_MS);
    }, true);
})();

// ============================================================
// Toast Notification System
// ============================================================
const ToastManager = (() => {
    let container = null;

    function ensureContainer() {
        if (!container) {
            container = document.createElement('div');
            container.className = 'toast-container';
            container.id = 'toast-container';
            document.body.appendChild(container);
        }
        return container;
    }

    function show(message, description, type = 'info', duration = 3000) {
        const c = ensureContainer();
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;

        let html = `<strong>${message}</strong>`;
        if (description) {
            html += `<span style="margin-left:0.25rem;opacity:0.8;font-size:0.8125rem;">${description}</span>`;
        }
        toast.innerHTML = html;

        c.appendChild(toast);

        setTimeout(() => {
            toast.style.animation = 'toast-out 0.3s ease forwards';
            setTimeout(() => toast.remove(), 300);
        }, duration);

        return toast;
    }

    return {
        success: (msg, desc, dur) => show(msg, desc, 'success', dur),
        error: (msg, desc, dur) => show(msg, desc, 'error', dur),
        info: (msg, desc, dur) => show(msg, desc, 'info', dur),
    };
})();

// ============================================================
// Sidebar Logic
// ============================================================
function initSidebar() {
    // Toggle nav groups (expand/collapse subitems)
    document.querySelectorAll('.nav-group-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const group = btn.closest('.nav-group');
            const subitems = group.querySelector('.nav-subitems');
            const isOpen = btn.classList.toggle('open');
            if (subitems) {
                subitems.classList.toggle('open', isOpen);
            }
        });
    });

    // Highlight current page in sidebar
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-subitems a, .nav-item-link').forEach(link => {
        if (link.getAttribute('href') === currentPath) {
            link.classList.add('active');
            // Also open parent group
            const group = link.closest('.nav-group');
            if (group) {
                const btn = group.querySelector('.nav-group-btn');
                const subitems = group.querySelector('.nav-subitems');
                if (btn) btn.classList.add('open');
                if (subitems) subitems.classList.add('open');
            }
        }
    });
}

// ============================================================
// Auth Guard
// ============================================================
const SESSION_DURATION_MS = 24 * 60 * 60 * 1000; // 24 hours

function requireAuth() {
    const rawData = sessionStorage.getItem('userData');
    if (!rawData) {
        window.location.href = appUrl('/login');
        return null;
    }
    
    try {
        const userData = JSON.parse(rawData);

        // Session expiry: auto-logout after 24 hours
        if (userData.loginAt) {
            const elapsed = Date.now() - userData.loginAt;
            if (elapsed > SESSION_DURATION_MS) {
                sessionStorage.removeItem('userData');
                window.location.href = appUrl('/login');
                return null;
            }
        }

        if (!userData.credentials_changed) {
            window.location.href = appUrl('/setup');
            return null;
        }
        if (!userData.is_activated) {
            window.location.href = appUrl('/activation');
            return null;
        }
        watchSession(userData);
        return userData;
    } catch (e) {
        sessionStorage.removeItem('userData');
        window.location.href = appUrl('/login');
        return null;
    }
}

// An open page used to stay up after its session ended (24 h passed, or the app
// restarted or updated and the cookie no longer verifies): nothing happened
// until the next click, which then landed on /login. Now the page itself goes
// to /login as soon as it finds out, by whichever comes first:
//   - the 24 hours since login running out (a timer);
//   - the server no longer accepting the cookie (/api/session, every 30 s and
//     whenever the tab comes back into view);
//   - any request answered "session expired" (X-Session-Expired on a 401 from
//     the frontend server; a data_service 401 such as a wrong password is not it).
const SESSION_CHECK_MS = 30 * 1000;
let _sessionWatched = false;

function endSession() {
    if (endSession.done) return;
    endSession.done = true;
    sessionStorage.removeItem('userData');
    window.location.replace(appUrl('/login'));
}

function watchSession(userData) {
    if (_sessionWatched) return;
    _sessionWatched = true;

    if (userData.loginAt) {
        setTimeout(endSession, Math.max(0, userData.loginAt + SESSION_DURATION_MS - Date.now()));
    }

    const nativeFetch = window.fetch.bind(window);
    const check = async () => {
        try {
            const res = await nativeFetch('/api/session', { cache: 'no-store' });
            if (res.ok && !(await res.json()).authenticated) endSession();
        } catch (_) { /* app restarting or offline: ask again next time */ }
    };
    setInterval(check, SESSION_CHECK_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
    window.addEventListener('focus', check);

    window.fetch = async (...args) => {
        const res = await nativeFetch(...args);
        if (res.status === 401 && res.headers.get('X-Session-Expired')) endSession();
        return res;
    };
}

// ============================================================
// Server Health Check
// ============================================================
async function checkServerStatus() {
    const healthUrl = '/frontend-health';
    console.log('[HealthCheck] Pinging:', healthUrl);
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 3000);

    try {
        const response = await fetch(healthUrl, {
            signal: controller.signal,
        });
        clearTimeout(timeoutId);

        if (response.ok) {
            const data = await response.json();
            console.log('[HealthCheck] Response:', data);
            return data.status === 'ok';
        }
        console.warn('[HealthCheck] Not ok:', response.status, response.statusText);
        return false;
    } catch (error) {
        console.error('[HealthCheck] Failed:', healthUrl, error.message || error);
        return false;
    }
}


// ============================================================
// Update notice (shared by the manual and the admin-pushed path)
// ============================================================
/**
 * Replace the page with the full-screen "updating" notice.
 * Idempotent: once it is up, later calls do nothing, so a poll that keeps
 * firing cannot wipe out a notice the user is already reading.
 */
window.showUpdatingOverlay = function showUpdatingOverlay() {
    if (window.__updatingOverlayShown) return;
    window.__updatingOverlayShown = true;

    // End the session before the app goes down. An update replaces the code
    // that is running, so whoever comes back should sign in again — and the
    // browser must not be left holding a cookie that outlives the version it
    // was issued for. keepalive so it still goes out while the page is being
    // torn down.
    try {
        fetch('/api/logout', { method: 'POST', keepalive: true }).catch(() => {});
    } catch (err) { /* service already going down */ }
    // The browser keeps its own copy of the signed-in user; leaving it behind
    // while the server session ends is what made /login bounce to /landing and
    // back again.
    try {
        sessionStorage.removeItem('userData');
    } catch (err) { /* storage blocked */ }

    // Point this tab at /login. Its URL is still the page it was on
    // (/store-info), so pressing reload on the update notice asked for that
    // page again — and once anyone signed in from the window the app opens
    // after restarting, that reload walked straight back into the app. The
    // notice has no business reloading into the app either way.
    try {
        history.replaceState(null, '', '/login');
    } catch (err) { /* history blocked — the logout above still applies */ }

    showBusyNotice('Updating the system. This may take a few moments...',
                   'The update window on this computer shows its progress. This page goes back to the login page by itself when the system is ready.');
    window.returnToLoginWhenBack();
};

/**
 * The busy notice a restart and an update share: pages with the sidebar swap only
 * their content, so it stays clear which screen is busy; the others (login, setup)
 * the whole body, styled inline since they load no ui.css.
 */
function showBusyNotice(title, sub) {
    const page = document.querySelector('.ac-main .ac-page');
    if (page) {
        page.innerHTML = `
            <div class="ac-busy" role="status" aria-live="polite">
                <div class="ac-spinner ac-spinner-lg"></div>
                <h1 class="ac-busy-title">${title}</h1>
                <p class="ac-busy-sub">${sub}</p>
            </div>`;
    } else {
        document.body.innerHTML = `
            <div role="status" aria-live="polite" style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;text-align:center;padding:24px;font-family:sans-serif;">
                <h1 style="margin:0 0 6px;font-size:16px;font-weight:600;color:#202020;">${title}</h1>
                <p style="margin:0;color:#6b7280;font-size:14px;">${sub}</p>
            </div>`;
    }
    document.querySelectorAll('.ac-modal:not([hidden])').forEach(m => { m.hidden = true; });
}

/**
 * The notice Restart System on the Service Port Settings page shows, for a restart
 * started somewhere else (the menu bar icon), whichever page this tab is on.
 * Pages with the sidebar swap only their content; the others (login, setup) the
 * whole body. Then back to the login page once the app is up again.
 */
window.showRestartingNotice = function showRestartingNotice() {
    if (window.__restartingNoticeShown || window.__updatingOverlayShown) return;
    window.__restartingNoticeShown = true;
    // The server-side session ends with the restart; a stale copy here loops /login <-> /landing.
    try { sessionStorage.removeItem('userData'); } catch (err) { /* storage blocked */ }
    showBusyNotice('Restarting port. This may take a few moments...',
                   'Please keep this page open until the process is complete.');
    window.returnToLoginWhenBack();
};

/**
 * Take this tab to the login page once the app is back from a restart or update.
 *
 * The window the app opens by itself afterwards comes from menubar_app, which a
 * branch installed by git clone does not run, so there the notice used to stay
 * up until someone pressed reload. Waits for the app to have gone down (or 90 s,
 * for a restart quicker than one poll), for no update to be in progress and for the
 * startup window to have closed.
 */
window.returnToLoginWhenBack = function returnToLoginWhenBack() {
    if (window.__returnToLoginWatch) return;
    window.__returnToLoginWatch = true;
    const startedAt = Date.now();
    let wentDown = false, backAt = 0;
    setInterval(async () => {
        try {
            const health = await fetch(appUrl('frontend-health'), { cache: 'no-store' });
            if (!health.ok) { wentDown = true; return; }
            const update = await fetch(appUrl('api/updates/in-progress'), { cache: 'no-store' });
            const busy = update.ok ? await update.json() : {};
            if (busy.updating) return;
            if (!wentDown && Date.now() - startedAt <= 90000) return;
            // Back up. Let the startup window say Ready and close first; 60 s at most, so
            // a window left open on an error message cannot hold this tab forever.
            backAt = backAt || Date.now();
            if (busy.starting && Date.now() - backAt < 60000) return;
            window.location.replace(appUrl('login'));
        } catch (err) {
            wentDown = true;   // app down mid-restart
        }
    }, 3000);
};

/**
 * Watch for an update the admin pushed, which this page is never told about
 * otherwise — the files arrive on the client supervisor's socket, not here.
 *
 * Failures are ignored on purpose: the frontend service is itself part of the
 * payload being replaced, so this request starts failing partway through every
 * update. Treating that as "no update" would tear the notice back down at the
 * worst possible moment, which is why showUpdatingOverlay() never reverts.
 */
function watchForPushedUpdate() {
    const POLL_MS = 3000;
    setInterval(async () => {
        if (window.__updatingOverlayShown) return;
        try {
            const res = await fetch('/api/updates/in-progress', { cache: 'no-store' });
            if (!res.ok) return;
            const data = await res.json();
            if (data.updating) window.showUpdatingOverlay();
            else if (data.restarting) window.showRestartingNotice();
        } catch (err) {
            /* service going down mid-update — keep whatever is on screen */
        }
    }, POLL_MS);
}

// ============================================================
// DOM Ready
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    initSidebar();
    watchForPushedUpdate();
});
