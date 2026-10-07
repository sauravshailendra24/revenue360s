(function () {
  const root = document.querySelector("[data-journey]");
  if (!root) return;
  const steps = [...root.querySelectorAll("[data-stage]")];
  const scene = root.querySelector("[data-scene]");
  const bar = root.querySelector(".journey-bar i");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const scenes = {
    find: ["Find", "A prospect enters the workspace.", "Acme Technologies", "SaaS · India · 500 employees"],
    understand: ["Understand", "The reason to talk is visible before the first message.", "Hiring sales roles", "Expanding · Active site · Recent launch"],
    reach: ["Reach", "The next channel is chosen from what is already connected.", "WhatsApp + Email", "LinkedIn only if that account is connected"],
    follow: ["Follow up", "The sequence waits, then continues, and stops on reply.", "Day 1 email", "Day 3 WhatsApp · Day 6 follow-up"],
    convert: ["Convert", "The outcome stays on the work list until it moves.", "Interested", "Meeting booked · Opportunity"]
  };
  let index = 0;
  function show(next) {
    index = Math.max(0, Math.min(steps.length - 1, next));
    steps.forEach((step, i) => step.classList.toggle("active", i === index));
    const key = steps[index].dataset.stage;
    const [title, copy, a, b] = scenes[key];
    if (scene) {
      scene.innerHTML = `<h3>${title}</h3><p>${copy}</p><div class="evidence"><div>${a}</div><div>${b}</div></div>`;
    }
    if (bar) bar.style.width = ((index + 1) / steps.length * 100) + "%";
    if (window.R360Analytics) window.R360Analytics.trackEvent("journey_step_view", { section: "journey", step: key });
  }
  steps.forEach((step, i) => step.onclick = () => show(i));
  let startX = 0;
  root.addEventListener("touchstart", (event) => { startX = event.changedTouches[0].clientX; }, { passive: true });
  root.addEventListener("touchend", (event) => {
    const delta = event.changedTouches[0].clientX - startX;
    if (Math.abs(delta) < 36) return;
    show(index + (delta < 0 ? 1 : -1));
  });
  if (!reduce && "IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting) && window.R360Analytics) {
        window.R360Analytics.trackEvent("journey_start", { section: "journey" });
        observer.disconnect();
      }
    }, { threshold: 0.4 });
    observer.observe(root);
    const timers = steps.map((_, i) => setTimeout(() => show(i), 400 + i * 1800));
    setTimeout(() => {
      if (window.R360Analytics) window.R360Analytics.trackEvent("journey_complete", { section: "journey" });
    }, 400 + steps.length * 1800);
    root.addEventListener("click", () => timers.forEach(clearTimeout), { once: true });
  }
  show(0);
})();
