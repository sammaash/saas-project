// Samma Inbox: keep the open page current without websockets.
// Polls a change marker (no message content) every 10 s while the tab is visible.
// When something changed it reloads the page - unless the user is typing a reply or
// confirming "Resume AI", in which case it only shows a "New activity" link.
(function () {
  "use strict";
  var root = document.getElementById("inbox-root");
  if (!root) return;
  var url = root.getAttribute("data-updates-url");
  var signature = root.getAttribute("data-signature");
  var banner = document.getElementById("inbox-refresh");
  var INTERVAL = 10000;
  var stopped = false;

  // Open on the newest message (the stream renders oldest-first).
  var stream = document.getElementById("msg-stream");
  var params = new URLSearchParams(window.location.search);
  if (stream && !params.get("before")) stream.scrollTop = stream.scrollHeight;

  function busy() {
    var composer = document.getElementById("composer-input");
    if (composer && !composer.disabled && composer.value.trim() !== "") return true;
    var resume = document.querySelector("details.resume-ai[open]");
    if (resume) return true;
    return false;
  }

  function changed() {
    if (busy()) {
      if (banner) banner.hidden = false;
      return;
    }
    window.location.reload();
  }

  function poll() {
    if (stopped || document.hidden) return;
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (response.status === 401 || response.status === 403) {
          stopped = true; // session ended: stop polling, the next click re-authenticates
          return null;
        }
        return response.ok ? response.json() : null;
      })
      .then(function (data) {
        if (data && data.signature && data.signature !== signature) changed();
      })
      .catch(function () { /* transient network error: try again next tick */ });
  }

  window.setInterval(poll, INTERVAL);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) poll();
  });
})();
