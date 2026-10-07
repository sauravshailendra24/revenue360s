(function () {
  const store = (() => { try { return JSON.parse(localStorage.getItem("r360_landing") || "{}"); } catch { return {}; } })();
  const token = localStorage.getItem("r360_token") || "";
  if (token && store.visitor_id) {
    fetch("/api/public/attach", {
      method: "POST",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: JSON.stringify({ visitor_id: store.visitor_id, session_id: store.session_id || "" })
    }).catch(() => {});
  }
  const orig = window.fetch;
  window.fetch = async function (url, options) {
    const response = await orig.apply(this, arguments);
    const path = String(url || "");
    if (response.ok && path.includes("/api/auth/signup") && window.R360Analytics) window.R360Analytics.trackEvent("signup_complete", { section: "app" });
    if (response.ok && path.includes("/api/auth/login") && window.R360Analytics) window.R360Analytics.trackEvent("login_click", { section: "app", result: "success" });
    return response;
  };
  const params = new URLSearchParams(location.search);
  window.addEventListener("load", () => {
    if (params.get("auth") === "signup") document.querySelector("#tab-signup") && document.querySelector("#tab-signup").click();
  });
})();
