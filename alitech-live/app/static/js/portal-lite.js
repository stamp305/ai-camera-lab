// Sidebar collapse + profile menu, behaving like the Alitech portal's (portal.js), without its i18n.
(() => {
  "use strict";
  const page = document.querySelector(".portal-page");
  const toggle = document.querySelector("[data-portal-sidebar-toggle]");
  const KEY = "portal-sidebar-collapsed";
  const set = (on) => {
    page.classList.toggle("sidebar-is-collapsed", on);
    const label = on ? "Expand sidebar" : "Collapse sidebar";
    toggle?.setAttribute("aria-label", label);
    toggle?.setAttribute("title", label);
  };
  try { if (localStorage.getItem(KEY) === "true") set(true); } catch (_) {}
  toggle?.addEventListener("click", () => {
    const on = !page.classList.contains("sidebar-is-collapsed");
    set(on);
    try { localStorage.setItem(KEY, String(on)); } catch (_) {}
  });

  const wrap = document.querySelector("[data-portal-profile]");
  const btn = document.querySelector("[data-portal-profile-toggle]");
  const menu = document.querySelector("[data-portal-profile-menu]");
  const open = (on) => {
    if (!wrap) return;
    if (on) menu.hidden = false;
    requestAnimationFrame(() => wrap.classList.toggle("is-open", on));
    btn.setAttribute("aria-expanded", String(on));
    if (!on) setTimeout(() => { if (!wrap.classList.contains("is-open")) menu.hidden = true; }, 180);
  };
  btn?.addEventListener("click", (e) => { e.stopPropagation(); open(!wrap.classList.contains("is-open")); });
  document.addEventListener("click", (e) => { if (wrap && !wrap.contains(e.target)) open(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") open(false); });
})();

// Security dropdown: Alitech's Analytics menu behaviour (portal.js setAnalyticsOpen) —
// an accordion while the sidebar is open, a flyout that closes itself while it is collapsed.
(() => {
  "use strict";
  const page = document.querySelector(".portal-page");
  const nav = document.querySelector("[data-security-nav]");
  const toggle = document.querySelector("[data-security-toggle]");
  const menu = document.querySelector("[data-security-menu]");
  if (!nav || !toggle || !menu) return;
  const collapsed = () => page.classList.contains("sidebar-is-collapsed");
  const setOpen = (on) => {
    toggle.setAttribute("aria-expanded", String(on));
    if (on) {
      menu.hidden = false;
      requestAnimationFrame(() => nav.classList.add("is-open"));
    } else {
      nav.classList.remove("is-open");
      setTimeout(() => { if (!nav.classList.contains("is-open")) menu.hidden = true; }, 180);
    }
  };
  if (collapsed() && !menu.hidden) { nav.classList.remove("is-open"); menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); }
  toggle.addEventListener("click", (e) => { e.stopPropagation(); setOpen(toggle.getAttribute("aria-expanded") !== "true"); });
  document.addEventListener("click", (e) => { if (collapsed() && !nav.contains(e.target)) setOpen(false); });
  menu.addEventListener("click", (e) => { if (collapsed() && e.target.closest(".portal-subnav")) setOpen(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && collapsed() && toggle.getAttribute("aria-expanded") === "true") { setOpen(false); toggle.focus(); } });
})();
