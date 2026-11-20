// Filter dropdowns with Alitech's analytics markup (.analytics-filter / -button / -menu /
// .analytics-options / .analytics-menu-search). One menu open at a time; outside click and
// Escape close it. Text always goes in with textContent, never as HTML.
(() => {
  "use strict";
  const ICON = {
    region: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2a7 7 0 0 1 7 7c0 5-7 13-7 13S5 14 5 9a7 7 0 0 1 7-7Zm0 4.5a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5Z"/></svg>',
    branch: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 21V9l8-6 8 6v12h-6v-6h-4v6H4Z"/></svg>',
    camera: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 7a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v2.5l5-3v11l-5-3V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/></svg>',
    device: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm5 14.5a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3ZM8 5v8h8V5H8Z"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
    caret: '<svg class="analytics-filter-caret" viewBox="0 0 24 24"><path d="m7 10 5 5 5-5"/></svg>',
  };
  const all = [];

  function create(host, { key, name, icon }) {
    const root = document.createElement("div");
    root.className = "analytics-filter";
    root.dataset.filter = key;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "analytics-filter-button";
    button.setAttribute("aria-haspopup", "true");
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = (ICON[icon] || "") + '<span class="analytics-filter-name"></span><span class="analytics-filter-value"></span>' + ICON.caret;
    button.querySelector(".analytics-filter-name").textContent = name;
    const menu = document.createElement("div");
    menu.className = "analytics-filter-menu";
    menu.hidden = true;
    root.append(button, menu);
    host.append(root);
    const f = {
      root, button, menu, build: null,
      setValue(text) { button.querySelector(".analytics-filter-value").textContent = text; },
      setDisabled(on) { button.disabled = on; root.classList.toggle("is-disabled", on); },
      open() {
        all.forEach((o) => o !== f && o.close());
        menu.replaceChildren();
        if (f.build) f.build(menu);
        menu.hidden = false;
        requestAnimationFrame(() => root.classList.add("is-open"));
        button.setAttribute("aria-expanded", "true");
        const s = menu.querySelector("input[type=search]");
        if (s) s.focus();
      },
      close() {
        if (menu.hidden) return;
        root.classList.remove("is-open");
        button.setAttribute("aria-expanded", "false");
        setTimeout(() => { if (!root.classList.contains("is-open")) menu.hidden = true; }, 180);
      },
    };
    button.addEventListener("click", (e) => { e.stopPropagation(); menu.hidden ? f.open() : f.close(); });
    menu.addEventListener("click", (e) => e.stopPropagation());
    all.push(f);
    return f;
  }

  function title(text) {
    const p = document.createElement("p");
    p.className = "analytics-menu-title";
    p.textContent = text;
    return p;
  }

  function searchBox(placeholder, onInput) {
    const label = document.createElement("label");
    label.className = "analytics-menu-search";
    label.innerHTML = ICON.search;
    const input = Object.assign(document.createElement("input"), { type: "search", placeholder });
    input.addEventListener("input", () => onInput(input.value.trim().toLowerCase()));
    label.append(input);
    return label;
  }

  function option(type, name, value, text, small, checked, searchText) {
    const label = document.createElement("label");
    label.className = "analytics-option";
    const input = Object.assign(document.createElement("input"), { type, name, value, checked: !!checked });
    const span = document.createElement("span");
    span.textContent = text;
    label.append(input, span);
    if (small) { const s = document.createElement("small"); s.textContent = small; label.append(s); }
    label.dataset.searchText = (searchText || text).toLowerCase();
    return label;
  }

  // Single choice; picks apply at once. options: [{value, text, small, search}]
  function radioMenu(menu, { heading, options, current, onPick, search }) {
    menu.append(title(heading));
    const box = document.createElement("div");
    box.className = "analytics-options";
    const name = "r" + Math.random().toString(36).slice(2);
    for (const o of options) box.append(option("radio", name, o.value, o.text, o.small, o.value === current, o.search));
    if (search) menu.append(searchBox(search, (q) => box.querySelectorAll(".analytics-option").forEach((l) => (l.hidden = !!q && !l.dataset.searchText.includes(q)))));
    if (!options.length) { const p = document.createElement("p"); p.className = "analytics-menu-note"; p.textContent = "ไม่มีรายการ"; box.append(p); }
    box.addEventListener("change", (e) => onPick(e.target.value));
    menu.append(box);
  }

  // Many choices with "All", Clear / Apply. options: [{value, text, small}]
  function checkMenu(menu, { heading, allText, options, selected, onApply, search }) {
    menu.append(title(heading));
    const box = document.createElement("div");
    box.className = "analytics-options";
    const allBox = option("checkbox", "", "", allText, "", false);
    allBox.classList.add("is-all");
    box.append(allBox);
    for (const o of options) box.append(option("checkbox", "", o.value, o.text, o.small, selected.has(o.value), o.search));
    if (search) menu.append(searchBox(search, (q) => {
      allBox.hidden = !!q;      // like Alitech: "All" hides while a search is typed
      box.querySelectorAll(".analytics-option:not(.is-all)").forEach((l) => (l.hidden = !!q && !l.dataset.searchText.includes(q)));
    }));
    const items = () => [...box.querySelectorAll(".analytics-option:not(.is-all) input")];
    const syncAll = () => {
      const n = items().filter((i) => i.checked).length;
      const a = allBox.querySelector("input");
      a.checked = n === items().length && n > 0;
      a.indeterminate = n > 0 && n < items().length;
    };
    allBox.querySelector("input").addEventListener("change", (e) => { items().forEach((i) => (i.checked = e.target.checked)); });
    box.addEventListener("change", (e) => { if (!e.target.closest(".is-all")) syncAll(); });
    syncAll();
    const err = document.createElement("p");
    err.className = "analytics-menu-error";
    const actions = document.createElement("div");
    actions.className = "analytics-menu-actions";
    const clear = Object.assign(document.createElement("button"), { type: "button", textContent: "ล้าง" });
    const apply = Object.assign(document.createElement("button"), { type: "button", textContent: "ใช้", className: "is-primary" });
    clear.addEventListener("click", () => { items().forEach((i) => (i.checked = false)); syncAll(); });
    apply.addEventListener("click", () => {
      const picked = items().filter((i) => i.checked).map((i) => i.value);
      if (!picked.length) { err.textContent = "เลือกอย่างน้อย 1 รายการ"; return; }
      onApply(picked);
    });
    actions.append(clear, apply);
    menu.append(box, err, actions);
  }

  document.addEventListener("click", () => all.forEach((f) => f.close()));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") all.forEach((f) => f.close()); });
  window.Filters = { create, radioMenu, checkMenu, closeAll: () => all.forEach((f) => f.close()) };
})();
