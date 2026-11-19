// Loading spinner: a ring of 8 dots, big and dark at the head fading to small and light,
// stepping round (CSS only: .dot-spin in live.css). Spinner.make("sm" | "lg").
(() => {
  "use strict";
  window.Spinner = {
    make(size = "lg") {
      const s = document.createElement("span");
      s.className = `dot-spin dot-spin-${size}`;
      s.setAttribute("aria-hidden", "true");
      for (let i = 0; i < 8; i++) s.append(document.createElement("i"));
      return s;
    },
  };
})();
