// Hardware Integration: category banner (closed on arrival) -> device cards -> one device's details.
(() => {
  "use strict";
  const toggle = (btn, open) => {
    btn.setAttribute("aria-expanded", String(open));
    document.getElementById(btn.getAttribute("aria-controls")).hidden = !open;
  };
  document.querySelectorAll("[data-hw-toggle]").forEach((btn) => {
    const card = btn.closest("[data-hw]");
    btn.addEventListener("click", () => {
      const open = btn.getAttribute("aria-expanded") !== "true";
      toggle(btn, open);
      card.classList.toggle("is-collapsed", !open);
    });
  });
  document.querySelectorAll("[data-hw-device]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const open = btn.getAttribute("aria-expanded") !== "true";
      // one device's details at a time
      btn.closest("[data-hw]").querySelectorAll("[data-hw-device]").forEach((b) => { toggle(b, false); b.classList.remove("is-on"); });
      toggle(btn, open);
      btn.classList.toggle("is-on", open);
    });
  });
})();
