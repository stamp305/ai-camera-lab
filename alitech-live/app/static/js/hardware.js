// Hardware Integration: one product type at a time (dropdown) -> a row per brand -> its devices.
// The chosen type rides in ?type= so the back link from a device page lands on the same type.
(() => {
  "use strict";
  const host = document.querySelector("[data-hw-filter]");
  const cats = [...document.querySelectorAll("[data-hw-cat]")];
  if (!host || !cats.length || !window.Filters) return;
  const options = cats.map((c) => ({ value: c.dataset.hwCat, text: c.dataset.hwName }));
  let current = (cats.find((c) => !c.hidden) || cats[0]).dataset.hwCat;
  const f = Filters.create(host, { key: "type", name: "ประเภท", icon: "device" });
  const show = (key) => {
    current = key;
    cats.forEach((c) => (c.hidden = c.dataset.hwCat !== key));
    f.setValue(options.find((o) => o.value === key).text);
    const url = new URL(location.href);
    url.searchParams.set("type", key);
    history.replaceState(null, "", url);
  };
  f.build = (menu) => Filters.radioMenu(menu, { heading: "ประเภทอุปกรณ์", options, current, onPick: (v) => { show(v); f.close(); } });
  show(current);
})();
