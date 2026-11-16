// Invitation / password-reset landing page.
// Supabase puts the access token in the URL fragment, which never reaches the server on its
// own. This removes it from the address bar and history, then posts it once (with the CSRF
// token already in the form) to the same origin, where the server verifies it.
(function () {
  "use strict";
  var status = document.getElementById("auth-callback-status");
  var form = document.getElementById("auth-callback-form");
  if (!status || !form) { return; }
  var params = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  if (window.history && window.history.replaceState) {
    window.history.replaceState(null, "", window.location.pathname);
  }
  var token = params.get("access_token");
  var type = params.get("type");
  var allowed = { invite: true, recovery: true, signup: true, magiclink: true };
  if (params.get("error") || !token || !allowed[type]) {
    status.textContent = "This link is invalid or has expired. Ask for a new invitation or password reset.";
    return;
  }
  form.elements.access_token.value = token;
  form.submit();
})();
