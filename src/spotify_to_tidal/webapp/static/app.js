if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/static/service-worker.js"));
}

if (document.body.dataset.tidalPending === "true") {
  const poll = async () => {
    try {
      const response = await fetch("/auth/tidal/status", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      if (!response.ok) return;
      const result = await response.json();
      if (result.status === "connected" || result.status === "error") {
        window.location.reload();
        return;
      }
    } catch (_) {
      // A transient network failure is safe to ignore; the next poll retries.
    }
    window.setTimeout(poll, 2500);
  };
  window.setTimeout(poll, 1200);
}
