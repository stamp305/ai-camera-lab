// frontend/static/js/version_manager.js
/* Version Manager - register branches, pull Bitbucket commits as versions,
   assign Git managed services versions to branches and force updates. */
(function () {
    "use strict";

    const BASE = "/ai-camera";
    let branches = [];
    let versions = [];
    let commits = [];

    const $ = (id) => document.getElementById(id);

    function showToast(message, isError = false, isInfo = false) {
        const container = $("toast-container");
        const toast = document.createElement("div");
        toast.textContent = message;
        toast.style.cssText =
            "background:" + (isError ? "#DC2626" : isInfo ? "#64748B" : "#16A34A") +
            ";color:#fff;padding:10px 16px;border-radius:8px;font-size:13px;" +
            "box-shadow:0 4px 12px rgba(0,0,0,.15);";
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 3500);
    }

    async function fetchJSON(url, options) {
        const resp = await fetch(url, options);
        if (!resp.ok) {
            let detail = "";
            try {
                const data = await resp.json();
                detail = data.error || data.detail || "";
            } catch (_) { /* ignore */ }
            throw new Error(detail || ("HTTP " + resp.status));
        }
        return resp.json();
    }

    async function loadData() {
        try {
            const [b, v] = await Promise.all([
                fetchJSON(BASE + "/api/branches"),
                fetchJSON(BASE + "/api/managed-versions"),
            ]);
            branches = Array.isArray(b) ? b : [];
            versions = Array.isArray(v) ? v : [];
            render();
            renderStoredVersions();
            renderActiveBuilds();
        } catch (e) {
            showToast("โหลดข้อมูลไม่สำเร็จ: " + e.message, true);
            $("loading-state").textContent = "โหลดข้อมูลไม่สำเร็จ: " + e.message;
            $("loading-state").classList.remove("loading-state");
        }
    }

    function escapeHtml(text) {
        return String(text == null ? "" : text)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function formatDate(iso) {
        if (!iso) return "-";
        const d = new Date(iso);
        if (isNaN(d.getTime())) return iso;
        return d.toLocaleString("th-TH", {
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
        });
    }

    // Auto version numbering (mirrors backend: V.major.minor.bug)
    function parseVersion(id) {
        const m = /^V\.(\d+)\.(\d+)(?:\.(\d+))?$/.exec(String(id || ""));
        if (!m) return null;
        return [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3] || 0, 10)];
    }

    function maxStoredVersion() {
        let best = [0, 0, 0];
        (versions || []).forEach((v) => {
            if (v.type !== "stored") return;
            const p = parseVersion(v.id);
            if (p && (p[0] > best[0] || (p[0] === best[0] && (p[1] > best[1] || (p[1] === best[1] && p[2] > best[2]))))) {
                best = p;
            }
        });
        return best;
    }

    function bumpVersion(bump, base) {
        if (bump === "major") return "V." + (base[0] + 1) + ".0.0";
        if (bump === "minor") return "V." + base[0] + "." + (base[1] + 1) + ".0";
        return "V." + base[0] + "." + base[1] + "." + (base[2] + 1);
    }

    // Dropdown order: newest version number first, oldest last. The backend
    // returns stored versions in registry insertion order, which is the order
    // they were pulled from Bitbucket — not version order.
    function versionsNewestFirst() {
        return (versions || []).slice().sort((a, b) => {
            const pa = parseVersion(a.id), pb = parseVersion(b.id);
            if (pa && pb) {
                for (let i = 0; i < 3; i++) {
                    if (pa[i] !== pb[i]) return pb[i] - pa[i];
                }
            } else if (pa) {
                return -1;   // numbered versions above tags/hashes
            } else if (pb) {
                return 1;
            }
            const da = new Date(a.date || a.stored_at || 0).getTime();
            const db = new Date(b.date || b.stored_at || 0).getTime();
            return (isNaN(db) ? 0 : db) - (isNaN(da) ? 0 : da);
        });
    }

    // เลขเวอร์ชันล้วน ๆ: id คือค่าที่ถูก assign จริง ส่วน label เป็นบันทึก
    // ช่วยจำที่ใครตั้งเป็นอะไรก็ได้ ต่อท้ายเข้าไปแล้วรายการยาวจนอ่านยาก
    // โดยไม่ได้ช่วยให้เลือกถูกขึ้น
    function versionOptionText(v) {
        return v.id;
    }

    // A stored version is only usable once a signed build artifact has landed
    // for it (backend flag `built`). Offering an unbuilt one just moves the
    // refusal to push time, where it reads as a mystery failure instead of a
    // version that was never compiled. Tags carry no flag and stay as they were.
    // The one line of a failed build worth putting on a table row. Cython's
    // report ends with "path/file.py:3:22: Expected ':', found 'NEWLINE'" —
    // that names the file, the line and the problem, which is everything the
    // operator needs to decide whether to fix the commit or pick another one.
    function failureHeadline(msg) {
        var lines = String(msg || "").split("\n").filter(function (l) { return l.trim(); });
        if (!lines.length) return "";
        for (var i = lines.length - 1; i >= 0; i--) {
            if (/:\d+:\d+:/.test(lines[i])) return lines[i].trim();
        }
        return lines[lines.length - 1].trim();
    }

    function isUsable(v) {
        return v.type !== "stored" || v.built !== false;
    }

    function versionOptions(selected) {
        let opts = '<option value="">-- เลือกเวอร์ชั่น --</option>';
        versionsNewestFirst().forEach((v) => {
            if (v.build_stage && v.build_stage !== "done" && v.build_stage !== "none") return;
            if (!isUsable(v)) return;
            opts += '<option value="' + escapeHtml(v.id) + '"' + (v.id === selected ? " selected" : "") + ">" +
                escapeHtml(versionOptionText(v)) + "</option>";
        });
        return opts;
    }

    function hasOption(sel, value) {
        return Array.prototype.some.call(sel.options, function (o) { return o.value === value; });
    }

    // A <select> that is replaced while open closes in the user's hand. While a
    // build runs the status poll calls render() twice a second, so a dropdown in
    // the branch table could not stay open long enough to pick from. Hold the
    // render while one of them has focus, and catch up when it lets go.
    let renderHeld = false;
    function pickingVersion() {
        const el = document.activeElement;
        return !!el && el.tagName === "SELECT" &&
            (el.classList.contains("version-select") || el.id === "assign-all-version-select");
    }
    document.addEventListener("focusout", function (e) {
        if (!renderHeld || !e.target || e.target.tagName !== "SELECT") return;
        setTimeout(function () { if (renderHeld && !pickingVersion()) render(); }, 0);
    });

    function render() {
        if (pickingVersion()) { renderHeld = true; return; }
        renderHeld = false;
        $("version-summary").textContent =
            "สาขาทั้งหมด " + branches.length + " รายการ | เวอร์ชั่นในระบบ " + versions.length + " รายการ";

        // Hold on to any version picked but not yet assigned. render() runs on
        // every status poll — every 15s idle, twice a second while a build is
        // on — and rebuilds each row's <select> from assigned_version, so a
        // choice sitting in the dropdown snapped back to the assigned version
        // a moment after it was made, before anyone could press Assign.
        const pendingPick = {};
        document.querySelectorAll(".version-select").forEach(function (sel) {
            if (sel.value) pendingPick[sel.dataset.code] = sel.value;
        });

        const assignAllSel = $("assign-all-version-select");
        const pendingAll = assignAllSel.value;
        assignAllSel.innerHTML = '<option value="">-- เลือกเวอร์ชั่น --</option>';
        versionsNewestFirst().forEach((v) => {
            if (v.build_stage && v.build_stage !== "done" && v.build_stage !== "none") return;
            if (!isUsable(v)) return;
            assignAllSel.insertAdjacentHTML("beforeend",
                '<option value="' + escapeHtml(v.id) + '">' + escapeHtml(versionOptionText(v)) + "</option>");
        });
        if (pendingAll && hasOption(assignAllSel, pendingAll)) assignAllSel.value = pendingAll;
        $("assign-all-btn").disabled = branches.length === 0;

        const tbody = $("branch-tbody");
        tbody.innerHTML = "";

        if (branches.length === 0) {
            $("empty-state").style.display = "block";
            $("version-table-wrap").style.display = "none";
            $("loading-state").style.display = "none";
            return;
        }
        $("empty-state").style.display = "none";
        $("version-table-wrap").style.display = "block";
        $("loading-state").style.display = "none";

        branches.forEach((branch) => {
            const code = branch.activation_code;
            const tr = document.createElement("tr");
            tr.style.borderBottom = "1px solid #f1f5f9";

            const online = branch.status === "active";
            const statusClass = online ? "status-online" : "status-offline";
            const statusText = online ? "ออนไลน์" : "ออฟไลน์";

            tr.innerHTML =
                '<td style="padding:10px 8px; font-family:monospace; font-weight:600;">' + escapeHtml(code) + "</td>" +
                '<td style="padding:10px 8px;">' + escapeHtml(branch.branch_name || "-") + "</td>" +
                '<td style="padding:10px 8px;"><span class="' + statusClass + '">' + statusText + "</span></td>" +
                '<td style="padding:10px 8px;">' + escapeHtml(branch.last_version || "-") + "</td>" +
                '<td style="padding:10px 8px;"><span id="assigned-' + code + '" style="font-family:monospace; font-weight:600; color:#2563EB;">' +
                escapeHtml(branch.assigned_version || "-") + "</span></td>" +
                '<td style="padding:10px 8px;"><select class="form-control version-select" data-code="' + code +
                '" style="min-width:200px; padding:6px; border-radius:6px; border:1px solid #d1d5db;">' +
                versionOptions(branch.assigned_version) + "</select></td>" +
                '<td style="padding:10px 8px; text-align:center; white-space:nowrap;">' +
                '<button class="btn btn-primary btn-assign" data-code="' + code +
                '" style="padding:6px 12px; font-size:12px; margin-right:6px;">Assign</button>' +
                '<button class="btn btn-secondary btn-force" data-code="' + code +
                '" style="padding:6px 12px; font-size:12px; margin-right:6px;">อัพเดตทันที</button>' +
                '<button class="btn btn-danger btn-del-branch" data-code="' + code +
                '" style="padding:6px 12px; font-size:12px; background:#DC2626; color:#fff; border:none; border-radius:6px; cursor:pointer;">ลบ</button></td>';

            tbody.appendChild(tr);
        });

        // Put the unsaved picks back, but only if that version is still on
        // offer — a version deleted or still building must not stay selected.
        Object.keys(pendingPick).forEach(function (code) {
            const sel = document.querySelector('.version-select[data-code="' + code + '"]');
            if (sel && hasOption(sel, pendingPick[code])) sel.value = pendingPick[code];
        });
    }

    // -----------------------------------------------------------------
    // Stored versions management (rename / delete)
    // -----------------------------------------------------------------
    let versionFilterText = "";
    let sortCol = "date";
    let sortDir = -1;

    function getVersionFilter() { return versionFilterText; }

    function sortVersions(list) {
        if (!sortCol) return list;
        return list.slice().sort((a, b) => {
            let va, vb;
            switch (sortCol) {
                case "id": va = String(a.id || ""); vb = String(b.id || ""); break;
                case "author": va = String(a.author || ""); vb = String(b.author || ""); break;
                case "date": va = new Date(a.date || a.stored_at).getTime(); vb = new Date(b.date || b.stored_at).getTime(); break;
                default: return 0;
            }
            if (va < vb) return -1 * sortDir;
            if (va > vb) return 1 * sortDir;
            return 0;
        });
    }

    function setSort(col) {
        if (col !== "id" && col !== "author" && col !== "date") return;
        if (sortCol === col) { sortDir *= -1; }
        else { sortCol = col; sortDir = 1; }
        renderStoredVersions();
    }
    window.setSort = setSort;

    function sortIndicator(col) {
        if (sortCol !== col) return "";
        return sortDir === 1 ? " ▲" : " ▼";
    }

    function renderStoredVersions() {
        const tbody = $("sv-tbody");
        if (!tbody) return;
        const stored = versions.filter((v) => v.type === "stored");
        const built = stored.filter((v) => {
            const s = v.build_stage || "none";
            return s === "done" || s === "none" || s === "failed" || s === "no_builder";
        });

        let filtered = built;
        const q = versionFilterText.trim().toLowerCase();
        if (q) {
            filtered = built.filter((v) => {
                const row = [
                    v.id || "", v.ref || v.commit || "", v.author || "",
                    v.date || v.stored_at || "", v.message || "",
                ].join(" ").toLowerCase();
                return row.indexOf(q) !== -1;
            });
        }
        filtered = sortVersions(filtered);

        // Update sort indicators
        ["id","author","date"].forEach(function(c) {
            var el = document.getElementById("sort-" + c);
            if (el) el.textContent = sortIndicator(c);
        });

        $("sv-loading").style.display = "none";
        $("sv-table-wrap").style.display = built.length ? "block" : "none";
        $("sv-empty").style.display = built.length ? "none" : "block";

        let countInfo = 'ทั้งหมด ' + built.length + ' รายการ';
        if (stored.length > built.length) countInfo += ' (' + (stored.length - built.length) + ' building)';
        if (q) countInfo += ' (พบ ' + filtered.length + ' รายการ)';
        const countEl = $("sv-count") || (function() {
            const el = document.createElement("div");
            el.id = "sv-count";
            el.style.cssText = "font-size:12px; color:#6b7280; margin-bottom:8px;";
            $("sv-search").parentNode.insertBefore(el, $("sv-search").nextSibling);
            return el;
        })();
        countEl.textContent = countInfo;

        tbody.innerHTML = "";
        filtered.forEach((v) => {
            const tr = document.createElement("tr");
            tr.style.borderBottom = "1px solid #f1f5f9";
            var unbuilt = !isUsable(v);
            var failed = unbuilt && v.build_stage === "failed";
            var headline = failed ? failureHeadline(v.build_message) : "";
            // Mock admin: how this version reaches a branch.
            var isSource = v.mode === "source";
            var modeBadge = '<span title="' + (isSource ? 'ส่งเป็นซอร์สโค้ด .py ตรงๆ' : 'คอมไพล์เป็น .so ด้วย build farm ก่อนส่ง') +
                '" style="display:inline-block; margin-left:6px; padding:1px 6px; border-radius:4px; font-family:system-ui; font-size:10px; font-weight:600; vertical-align:middle; ' +
                (isSource ? 'background:#fef3c7; color:#92400e;' : 'background:#dbeafe; color:#1e40af;') + '">' +
                (isSource ? 'SOURCE' : '.so') + '</span>';
            var idCell = escapeHtml(v.id) + modeBadge + (unbuilt
                ? '<div style="font-family:system-ui; font-weight:400; font-size:11px; color:#b45309; margin-top:2px;">'
                  + (failed ? 'บิลด์ล้มเหลว' : 'ยังไม่ได้บิลด์')
                  + ' — assign ให้สาขาไม่ได้</div>'
                : '');
            // Without this the page said a version was unbuilt and nothing
            // else, so the only way to learn why was to read the builder's log
            // on the build machine.
            if (headline) {
                idCell += '<div title="' + escapeHtml(v.build_message || '') +
                    '" style="font-family:ui-monospace,monospace; font-weight:400; font-size:11px;' +
                    ' color:#b91c1c; margin-top:3px; max-width:340px; overflow:hidden;' +
                    ' text-overflow:ellipsis; white-space:nowrap; cursor:help;">' +
                    escapeHtml(headline) + '</div>';
            }
            tr.innerHTML =
                '<td style="padding:10px 8px; font-family:monospace; font-weight:600; color:' +
                (unbuilt ? '#b45309' : '#2563EB') + ';">' + idCell + "</td>" +
                '<td style="padding:10px 8px; font-family:monospace; font-size:12px;">' +
                escapeHtml((v.ref || v.commit || "").slice(0, 10) || "-") + "</td>" +
                '<td style="padding:10px 8px;">' + escapeHtml(v.author || "-") + "</td>" +
                '<td style="padding:10px 8px;">' + formatDate(v.date || v.stored_at) + "</td>" +
                '<td style="padding:10px 8px; max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' +
                escapeHtml(v.message || "-") + "</td>" +
                '<td style="padding:10px 8px; text-align:center; white-space:nowrap;">' +
                (unbuilt
                    ? '<button class="btn btn-build-ver" data-id="' + escapeHtml(v.id) +
                      '" style="padding:6px 12px; font-size:12px; background:#2563EB; color:#fff; border:none; border-radius:6px; cursor:pointer; margin-right:6px;">'
                      + (failed ? 'บิลด์ใหม่' : 'บิลด์') + '</button>'
                    : '') +
                '<button class="btn btn-mode-ver" data-id="' + escapeHtml(v.id) + '" data-mode="' + (isSource ? 'so' : 'source') +
                '" style="padding:6px 12px; font-size:12px; background:' + (isSource ? '#2563EB' : '#d97706') +
                '; color:#fff; border:none; border-radius:6px; cursor:pointer; margin-right:6px;">' +
                (isSource ? 'บิลด์เป็น .so' : 'กลับไปเป็น Source') + '</button>' +
                '<button class="btn btn-danger btn-del-ver" data-id="' + escapeHtml(v.id) +
                '" style="padding:6px 12px; font-size:12px; background:#DC2626; color:#fff; border:none; border-radius:6px; cursor:pointer;">ลบ</button></td>';
            tbody.appendChild(tr);
        });
    }

    function renderActiveBuilds() {
        const card = $("active-builds-card");
        const list = $("active-builds-list");
        if (!card || !list) return;
        var activeStages = ["building", "compiling", "downloading", "installing", "uploading", "waiting_download"];
        const active = versions.filter(function (v) {
            var s = v.build_stage || "none";
            return activeStages.indexOf(s) !== -1;
        });
        if (active.length === 0) {
            card.style.display = "none";
            return;
        }
        card.style.display = "block";

        var stageCfg = {
            building:         { label: "Compiling...",    color: "#3b82f6" },
            compiling:        { label: "Compiling...",    color: "#3b82f6" },
            downloading:      { label: "Downloading...",  color: "#f97316" },
            installing:       { label: "Installing...",   color: "#8b5cf6" },
            uploading:        { label: "Uploading...",    color: "#f97316" },
            waiting_download: { label: "Uploading...",    color: "#f97316" },
        };

        // Update existing rows in place (keyed by version id) instead of
        // wiping innerHTML each poll — recreating the bar element every
        // cycle restarted its CSS animation from frame 0, causing the jerk.
        var seen = {};
        active.forEach(function (v) {
            var stage = v.build_stage || "building";
            var cfg = stageCfg[stage] || stageCfg.building;
            var rowId = "build-row-" + escapeHtml(v.id).replace(/[^a-zA-Z0-9_-]/g, "_");
            seen[rowId] = true;
            var row = document.getElementById(rowId);
            if (!row) {
                row = document.createElement("div");
                row.id = rowId;
                row.style.cssText = "margin-bottom:12px; padding:10px 14px; background:#f8fafc; border-radius:8px; border:1px solid #e5e7eb;";
                row.innerHTML =
                    '<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">' +
                        '<span style="font-family:monospace; font-weight:600; color:#2563EB; font-size:13px;">' + escapeHtml(v.id) + '</span>' +
                        '<span class="build-row-label" style="font-size:11px; font-weight:500;"></span>' +
                    '</div>' +
                    '<div style="height:8px; background:#e5e7eb; border-radius:4px; overflow:hidden;">' +
                        '<div class="build-bar build-bar-indeterminate" style="height:100%; width:40%; border-radius:4px; transition:width .35s ease;"></div>' +
                    '</div>' +
                    '<div class="build-row-detail" style="font-size:11px; color:#374151; margin-top:6px; font-family:monospace; min-height:14px;"></div>' +
                    '<div style="font-size:11px; color:#6b7280; margin-top:4px;">' +
                        escapeHtml(v.author || "") + (v.date ? ' · ' + escapeHtml(formatDate(v.date)) : '') +
                    '</div>';
                list.appendChild(row);
            }
            var labelEl = row.querySelector(".build-row-label");
            var barEl = row.querySelector(".build-bar");
            var detailEl = row.querySelector(".build-row-detail");
            var pct = (typeof v.build_percent === "number") ? v.build_percent : null;
            labelEl.textContent = pct === null ? cfg.label : cfg.label + "  " + pct + "%";
            labelEl.style.color = cfg.color;
            barEl.style.background = cfg.color;
            // A builder that reports a percent gets a bar that actually fills;
            // one that doesn't (older worker) keeps the sliding placeholder,
            // so the two never mix into a bar that looks stuck at 40%.
            if (pct === null) {
                barEl.classList.add("build-bar-indeterminate");
                barEl.style.width = "40%";
            } else {
                barEl.classList.remove("build-bar-indeterminate");
                barEl.style.marginLeft = "0";
                barEl.style.width = pct + "%";
            }
            detailEl.textContent = v.build_message || "";
        });

        // Remove rows for builds no longer active
        Array.prototype.slice.call(list.children).forEach(function (el) {
            if (!seen[el.id]) el.remove();
        });
    }


    async function cancelActiveBuilds() {
        var activeStages = ["building", "compiling", "downloading", "installing", "uploading", "waiting_download"];
        var active = versions.filter(function (v) {
            return activeStages.indexOf(v.build_stage || "none") !== -1;
        });
        if (!active.length) return;
        if (!confirm("ยกเลิกการบิวด์ที่กำลังทำงานอยู่ " + active.length + " รายการ? (เครื่องบิวเดอร์อาจคอมไพล์ต่อจนจบ แต่ผลลัพธ์จะถูกละทิ้ง)")) return;
        for (const v of active) {
            try {
                await fetchJSON(BASE + "/api/bitbucket/version/" + encodeURIComponent(v.id) + "/cancel-build", { method: "POST" });
            } catch (e) {
                console.error("cancel failed for", v.id, e);
            }
        }
        $("active-builds-card").style.display = "none";
        showToast("ยกเลิกการบิวด์แล้ว");
        loadData();
    }

    async function rebuildVersion(id) {
        try {
            const data = await fetchJSON(BASE + "/api/bitbucket/version/" + encodeURIComponent(id) + "/rebuild", {
                method: "POST",
            });
            showToast(data.message || ("สั่งบิลด์ " + id + " แล้ว"), !data.notified);
            if (window.__kickStatusPoll) window.__kickStatusPoll();
        } catch (e) {
            showToast("สั่งบิลด์ล้มเหลว: " + e.message, true);
        }
    }

    async function setVersionMode(id, mode) {
        const what = mode === "source"
            ? ("ส่ง " + id + " เป็นซอร์สโค้ด .py ?\nสาขาที่รับเวอร์ชันนี้จะได้ซอร์สที่อ่านได้ และไฟล์ .so เดิมจะถูกลบ")
            : ("สั่งบิลด์ " + id + " เป็น .so ?\nระหว่างรอบิลด์ จะ assign เวอร์ชันนี้ให้สาขาไม่ได้");
        if (!confirm(what)) return;
        try {
            const data = await fetchJSON(BASE + "/api/bitbucket/version/" + encodeURIComponent(id) + "/mode", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ mode: mode }),
            });
            showToast(mode === "source"
                ? (id + " กลับไปเป็น Source แล้ว")
                : (data.notified ? ("สั่งบิลด์ " + id + " เป็น .so แล้ว")
                                 : ("ตั้ง " + id + " เป็น .so แล้ว แต่ยังไม่มีบิลเดอร์ต่ออยู่ — ยังไม่มีอะไรบิลด์")),
                mode === "so" && !data.notified);
            await loadData();
            if (window.__kickStatusPoll) window.__kickStatusPoll();
        } catch (e) {
            showToast("เปลี่ยนวิธีส่งไม่สำเร็จ: " + e.message, true);
        }
    }

    async function deleteVersion(id) {
        if (!confirm("ลบเวอร์ชัน '" + id + "' ? (จะลบทั้ง registry และโฟลเดอร์ cache)")) return;
        try {
            const data = await fetchJSON(BASE + "/api/bitbucket/version/" + encodeURIComponent(id), {
                method: "DELETE",
            });
            showToast(data.message || ("ลบเวอร์ชัน " + id + " สำเร็จ"));
            await loadData();
        } catch (e) {
            showToast("ลบเวอร์ชันล้มเหลว: " + e.message, true);
        }
    }

    async function assignVersion(code, version) {
        const btn = document.querySelector('.btn-assign[data-code="' + code + '"]');
        if (btn) { btn.disabled = true; }
        try {
            const data = await fetchJSON(BASE + "/api/branches/" + encodeURIComponent(code) + "/assign-version", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ version: version }),
            });
            const assignedEl = $("assigned-" + code);
            if (assignedEl) { assignedEl.textContent = version; }
            showToast(data.message || ("Assign " + version + " ให้ " + code + " สำเร็จ"));
        } catch (e) {
            showToast("Assign " + code + " ล้มเหลว: " + e.message, true);
        } finally {
            if (btn) { btn.disabled = false; }
        }
    }

    async function forceUpdate(code) {
        const btn = document.querySelector('.btn-force[data-code="' + code + '"]');
        if (btn) { btn.disabled = true; }
        try {
            const data = await fetchJSON(BASE + "/api/branches/" + encodeURIComponent(code) + "/force-update", {
                method: "POST",
            });
            if (data.success) {
                if (data.skipped) {
                    showToast(data.message || ("เวอร์ชันของ " + code + " ตรงกันอยู่แล้ว"), false, true);
                } else {
                    showToast(data.message || ("อัพเดต " + code + " สำเร็จ"));
                }
            } else {
                showToast(data.message || ("อัพเดต " + code + " ไม่สำเร็จ"), true);
            }
        } catch (e) {
            showToast("อัพเดต " + code + " ล้มเหลว: " + e.message, true);
        } finally {
            if (btn) { btn.disabled = false; }
        }
    }

    async function assignAll(version) {
        $("assign-all-btn").disabled = true;
        let ok = 0, fail = 0;
        for (const branch of branches) {
            try {
                await fetchJSON(BASE + "/api/branches/" + encodeURIComponent(branch.activation_code) + "/assign-version", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ version: version }),
                });
                const el = $("assigned-" + branch.activation_code);
                if (el) { el.textContent = version; }
                ok++;
            } catch (_) {
                fail++;
            }
        }
        showToast("Assign " + version + " ให้ " + ok + " สาขา" + (fail ? " (ล้มเหลว " + fail + ")" : ""));
        $("assign-all-btn").disabled = false;
    }

    async function forceAll() {
        $("force-all-btn").disabled = true;
        try {
            for (const branch of branches) {
                await forceUpdate(branch.activation_code);
            }
        } finally {
            $("force-all-btn").disabled = false;
        }
    }

    // -----------------------------------------------------------------
    // Branch deletion
    // -----------------------------------------------------------------
    async function deleteBranch(code) {
        try {
            const data = await fetchJSON(BASE + "/api/branches/" + encodeURIComponent(code), {
                method: "DELETE",
            });
            showToast(data.message || ("ลบสาขา " + code + " สำเร็จ"));
            await loadData();
        } catch (e) {
            showToast("ลบสาขา " + code + " ล้มเหลว: " + e.message, true);
        }
    }

    // -----------------------------------------------------------------
    // Bitbucket commits
    // -----------------------------------------------------------------
    async function loadBitbucketBranches() {
        try {
            const data = await fetchJSON(BASE + "/api/bitbucket/branches");
            const list = (data && data.branches) || [];
            const sel = $("bb-branch-select");
            sel.innerHTML = "";
            list.forEach((b) => {
                sel.insertAdjacentHTML("beforeend", '<option value="' + escapeHtml(b) + '">' + escapeHtml(b) + "</option>");
            });
            if (list.length) {
                const last = localStorage.getItem("bb_branch") || list[0];
                sel.value = list.includes(last) ? last : list[0];
            }
        } catch (e) {
            $("bb-status").textContent = "ดึง branch ไม่สำเร็จ: " + e.message;
        }
    }

    async function fetchCommits() {
        const branch = $("bb-branch-select").value;
        if (!branch) {
            showToast("กรุณาเลือก branch ก่อน", true);
            return;
        }
        localStorage.setItem("bb_branch", branch);

        const btn = $("fetch-commits-btn");
        btn.disabled = true;
        $("bb-status").textContent = "Fetching commits from Bitbucket...";
        try {
            const limit = $("fetch-commits-limit").value;
            let url = BASE + "/api/bitbucket/commits?branch=" + encodeURIComponent(branch);
            if (limit) url += "&limit=" + encodeURIComponent(limit);
            const data = await fetchJSON(url);
            commits = Array.isArray(data) ? data : [];
            renderCommits();
            const limitLabel = $("fetch-commits-limit").selectedOptions[0].textContent;
            $("bb-status").textContent =
                commits.length
                    ? "มีคอมมิตใหม่ " + commits.length + " รายการ (" + limitLabel + ") จาก branch '" + branch + "'"
                    : "ไม่มีคอมมิตใหม่ใน branch '" + branch + "' (" + limitLabel + ")";
        } catch (e) {
            $("bb-status").textContent = "ดึง commits ไม่สำเร็จ";
            showToast("ดึง commits ล้มเหลว: " + e.message, true);
        } finally {
            btn.disabled = false;
        }
    }

    function selectedCommits() {
        return commits.filter((c) => {
            const cb = document.querySelector('.commit-check[data-id="' + c.short + '"]');
            return cb && cb.checked;
        });
    }

    function renderCommits() {
        const tbody = $("commits-tbody");
        tbody.innerHTML = "";

        if (commits.length === 0) {
            $("commits-table-wrap").style.display = "none";
            $("commits-empty").style.display = "block";
            // "New Ver" shows only commits not fetched before, and the fetch
            // itself moves that mark forward — so a commit appears exactly
            // once and is gone on the next press. Saying only "no new commits"
            // reads as "the button is broken" to whoever just watched their
            // commit vanish; the way back to it is the N-back option.
            var newOnly = !$("fetch-commits-limit").value;
            $("commits-empty").innerHTML = newOnly
                ? 'ไม่มี commit ใหม่ตั้งแต่ครั้งที่กดดึงล่าสุด' +
                  '<div style="color:#92400e; margin-top:6px;">' +
                  'โหมด <b>New Ver</b> แสดงเฉพาะ commit ที่ยังไม่เคยดึง และการกดดึงจะนับว่า' +
                  'ดึงไปแล้ว — commit ที่เคยขึ้นรอบก่อนจะไม่ขึ้นซ้ำ ถ้าจะเก็บตัวนั้น ให้เปลี่ยนเป็น ' +
                  '<b>5 ตัวย้อนหลัง</b> หรือ <b>10 ตัวย้อนหลัง</b> แล้วกดดึงใหม่</div>'
                : 'ไม่เจอ commit ใน branch นี้ตามจำนวนที่เลือก';
            $("store-versions-btn").disabled = true;
            $("store-versions-btn").textContent = "เก็บเวอร์ชันที่เลือก (0)";
            return;
        }
        $("commits-empty").style.display = "none";
        $("commits-table-wrap").style.display = "block";

        commits.forEach((c) => {
            const tr = document.createElement("tr");
            tr.style.borderBottom = "1px solid #f1f5f9";

            const badge = c.stored
                ? '<span style="font-size:11px; background:#E0E7FF; color:#3730A3; border-radius:99px; padding:2px 8px; margin-left:6px;">เก็บแล้ว</span>'
                : '<span style="font-size:11px; background:#FEF3C7; color:#92400E; border-radius:99px; padding:2px 8px; margin-left:6px;">ใหม่</span>';

            tr.innerHTML =
                '<td style="padding:10px 8px;"><input type="checkbox" class="commit-check" data-id="' + c.short + '"' + (c.stored ? " disabled" : "") + " /></td>" +
                '<td style="padding:10px 8px; font-family:monospace; font-weight:600;">' + escapeHtml(c.short) + badge + "</td>" +
                '<td style="padding:10px 8px;">' + escapeHtml(c.message) + "</td>" +
                '<td style="padding:10px 8px;">' + escapeHtml(c.author) + "</td>" +
                '<td style="padding:10px 8px;">' + escapeHtml(formatDate(c.date)) + "</td>";

            tbody.appendChild(tr);
        });

        updateStoreButton();
    }

    function updateStoreButton() {
        const count = selectedCommits().length;
        $("store-versions-btn").disabled = count === 0;
        $("store-versions-btn").textContent = "เก็บเวอร์ชันที่เลือก (" + count + ")";
    }

    // -----------------------------------------------------------------
    // Store selected commits as versions
    // -----------------------------------------------------------------
    function openStoreModal() {
        // Numbering follows commit time (oldest first), matching the backend.
        const items = selectedCommits()
            .slice()
            .sort((a, b) => String(a.date).localeCompare(String(b.date)));
        if (items.length === 0) return;
        const wrap = $("store-items");
        wrap.innerHTML = "";

        let running = maxStoredVersion();
        items.forEach((c) => {
            const preview = bumpVersion("bug", running);
            running = parseVersion(preview);
            const row = document.createElement("div");
            row.style.cssText = "border:1px solid #e5e7eb; border-radius:8px; padding:10px 12px; margin-bottom:8px;";
            row.innerHTML =
                '<div style="display:flex; justify-content:space-between; gap:8px; align-items:center;">' +
                '<div style="min-width:0;">' +
                '<div style="font-family:monospace; font-weight:600; font-size:12px;">' + escapeHtml(c.short) + "</div>" +
                '<div style="font-size:12px; color:#374151; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">' +
                escapeHtml(c.message) + "</div>" +
                '<div style="font-size:11px; color:#6b7280;">' + escapeHtml(c.author) + " · " + escapeHtml(formatDate(c.date)) + "</div>" +
                "</div>" +
                '<div style="flex-shrink:0; text-align:right;">' +
                '<label style="font-size:11px; color:#6b7280; display:block;">ประเภทการแก้</label>' +
                '<select class="store-bump" data-commit="' + c.short + '" style="width:140px; padding:6px 8px; border-radius:6px; border:1px solid #d1d5db; font-size:12px;">' +
                '<option value="bug" selected>Bug — แก้บั๊ก</option>' +
                '<option value="minor">Minor — ฟีเจอร์เล็ก</option>' +
                '<option value="major">Major — ใหญ่/ของเดิมพัง</option>' +
                "</select>" +
                '<div class="store-preview" style="font-size:12px; color:#2563EB; font-family:monospace; font-weight:600; margin-top:4px;">→ ' + preview + "</div>" +
                "</div></div>";
            wrap.appendChild(row);
        });

        wrap.addEventListener("change", (e) => {
            if (e.target.classList.contains("store-bump")) updateStorePreviews();
        });

        $("store-modal").style.display = "flex";
    }

    function updateStorePreviews() {
        let running = maxStoredVersion();
        Array.from(document.querySelectorAll("#store-items > div")).forEach((row) => {
            const sel = row.querySelector(".store-bump");
            if (!sel) return;
            const preview = bumpVersion(sel.value, running);
            running = parseVersion(preview);
            const el = row.querySelector(".store-preview");
            if (el) el.textContent = "→ " + preview;
        });
    }

    function closeStoreModal() {
        $("store-modal").style.display = "none";
    }

    async function confirmStore() {
        const selects = Array.from(document.querySelectorAll(".store-bump"));
        // Every version is stored as .so; the table's button switches one to source.
        const items = selects.map((sel) => ({
            commit: sel.dataset.commit,
            bump: sel.value,
        }));
        if (items.length === 0) return;

        const btn = $("store-confirm-btn");
        btn.disabled = true;
            $("bb-status").textContent = "Storing version...";
        try {
            const data = await fetchJSON(BASE + "/api/bitbucket/store", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ items: items }),
            });
            const stored = (data && data.stored) || [];
            const builders = (data && data.builders) || 0;
            closeStoreModal();
            // With no builder connected nothing compiles at all. Saying
            // "building automatically" then sent people off to wait for a bar
            // that was never going to appear.
            showToast(builders
                ? ("เก็บเวอร์ชันแล้ว " + stored.length + " รายการ — กำลังบิลด์ (บิลเดอร์ " + builders + " เครื่อง)")
                : ("เก็บเวอร์ชันแล้ว " + stored.length + " รายการ แต่ยังไม่มีบิลเดอร์ต่ออยู่ — ยังไม่มีอะไรบิลด์ กดปุ่ม \"บิลด์\" ในตารางเวอร์ชันเมื่อบิลเดอร์ขึ้นแล้ว"),
                !builders);
            await Promise.all([loadData(), fetchCommits()]);
            if (window.__kickStatusPoll) window.__kickStatusPoll();
        } catch (e) {
            $("bb-status").textContent = "Store failed";
            showToast("เก็บเวอร์ชันล้มเหลว: " + e.message, true);
        } finally {
            btn.disabled = false;
        }
    }

    // -----------------------------------------------------------------
    // Realtime build status polling
    // -----------------------------------------------------------------
    function startStatusPolling() {
        var timer = null;
        var wasBuilding = false;
        var activeStages = ["building", "compiling", "downloading", "installing", "uploading", "waiting_download"];
        function isActive(v) {
            var s = v.build_stage || "none";
            return activeStages.indexOf(s) !== -1;
        }
        async function tick() {
            try {
                const res = await fetchJSON(BASE + "/api/managed-versions");
                if (res && res.length) {
                    var isBuilding = false;
                    res.forEach(function (v) {
                        var idx = versions.findIndex(function (vv) { return vv.id === v.id; });
                        if (idx >= 0) {
                            Object.keys(v).forEach(function (k) { versions[idx][k] = v[k]; });
                        }
                        if (isActive(v)) {
                            isBuilding = true;
                        }
                    });
                    renderStoredVersions();
                    renderActiveBuilds();
                    render();
                    if (wasBuilding && !isBuilding) {
                        showToast("Build completed");
                        loadData();
                    }
                    wasBuilding = isBuilding;
                }
            } catch (e) { /* silently retry */ }
            var active = versions.some(isActive);
            timer = setTimeout(tick, active ? 500 : 15000);
        }
        window.__kickStatusPoll = function () {
            wasBuilding = true;
            if (timer) { clearTimeout(timer); timer = null; }
            tick();
        };
        tick();
    }

    // -----------------------------------------------------------------
    // Event wiring
    // -----------------------------------------------------------------
    document.addEventListener("DOMContentLoaded", () => {
        $("refresh-btn").addEventListener("click", loadData);

        // Re-check on its own every 30 minutes: last_version only moves when a
        // branch reconnects, so the page would otherwise keep showing whatever
        // was true when it was opened.
        setInterval(loadData, 30 * 60 * 1000);

        $("sv-search") && $("sv-search").addEventListener("input", (e) => {
            versionFilterText = e.target.value;
            renderStoredVersions();
        });

        $("fetch-commits-btn").addEventListener("click", fetchCommits);
        $("store-versions-btn").addEventListener("click", openStoreModal);
        $("store-cancel-btn").addEventListener("click", closeStoreModal);
        $("store-confirm-btn").addEventListener("click", confirmStore);

        $("commits-select-all").addEventListener("change", (e) => {
            document.querySelectorAll(".commit-check").forEach((cb) => {
                if (!cb.disabled) cb.checked = e.target.checked;
            });
            updateStoreButton();
        });

        $("commits-tbody").addEventListener("change", (e) => {
            if (e.target.classList.contains("commit-check")) updateStoreButton();
        });

        $("assign-all-btn").addEventListener("click", () => {
            const version = $("assign-all-version-select").value;
            if (!version) {
                showToast("กรุณาเลือกเวอร์ชั่นก่อน", true);
                return;
            }
            if (confirm("Assign " + version + " ให้ทุกสาขา (" + branches.length + " รายการ) ใช่หรือไม่?")) {
                assignAll(version);
            }
        });

        $("force-all-btn").addEventListener("click", () => {
            if (confirm("บังคับอัพเดตทันทีทุกสาขาที่เชื่อมต่ออยู่ ใช่หรือไม่?")) {
                forceAll();
            }
        });

        document.addEventListener("click", (e) => {
            const assignBtn = e.target.closest(".btn-assign");
            if (assignBtn) {
                const code = assignBtn.dataset.code;
                const sel = document.querySelector('.version-select[data-code="' + code + '"]');
                const version = sel ? sel.value : "";
                if (!version) {
                    showToast("กรุณาเลือกเวอร์ชั่นสำหรับ " + code, true);
                    return;
                }
                assignVersion(code, version);
                return;
            }
            const forceBtn = e.target.closest(".btn-force");
            if (forceBtn) {
                const code = forceBtn.dataset.code;
                if (confirm("บังคับอัพเดต " + code + " ทันที?")) {
                    forceUpdate(code);
                }
                return;
            }
            const delBtn = e.target.closest(".btn-del-branch");
            if (delBtn) {
                const code = delBtn.dataset.code;
                if (confirm("ลบสาขา " + code + " ออกจากระบบ?")) {
                    deleteBranch(code);
                }
                return;
            }
            const delVer = e.target.closest(".btn-del-ver");
            if (delVer) {
                deleteVersion(delVer.dataset.id);
                return;
            }
            const modeVer = e.target.closest(".btn-mode-ver");
            if (modeVer) {
                setVersionMode(modeVer.dataset.id, modeVer.dataset.mode);
                return;
            }
            const buildVer = e.target.closest(".btn-build-ver");
            if (buildVer) {
                rebuildVersion(buildVer.dataset.id);
                return;
            }
        });

        $("active-builds-cancel") && $("active-builds-cancel").addEventListener("click", cancelActiveBuilds);

        loadData();
        startStatusPolling();
        loadBitbucketBranches();
    });
})();
