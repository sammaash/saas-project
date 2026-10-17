// WhatsApp AI configuration tab.
//
// Two progressive enhancements, both of which degrade to a working page without JavaScript:
//
//   1. Mark a section as having unsaved changes as soon as one of its fields is edited, so
//      "Saved" and "pending change" are visibly different.
//   2. Let "Save every section at once" submit all section forms as one onboarding payload
//      (the same request the original single-form onboarding page posted).
(function () {
  "use strict";

  var forms = Array.prototype.slice.call(document.querySelectorAll("form.config-form"));
  if (forms.length === 0) {
    return;
  }

  function markDirty(form, dirty) {
    form.classList.toggle("dirty", dirty);
    var state = form.querySelector(".section-state");
    if (state) {
      state.textContent = dirty ? "Unsaved changes" : "Saved";
      state.classList.toggle("dirty", dirty);
    }
  }

  forms.forEach(function (form) {
    ["input", "change"].forEach(function (event) {
      form.addEventListener(event, function () {
        markDirty(form, true);
      });
    });
    // A save navigates away; if the browser restores the page, start from "Saved" again.
    window.addEventListener("pageshow", function () {
      markDirty(form, false);
    });
  });

  var saveAll = document.getElementById("save-all-sections");
  if (!saveAll) {
    return;
  }

  // Fields that belong to the section protocol, not to the configuration itself.
  var skip = { csrf_token: true, config_version: true, focus: true, form_kind: true };

  saveAll.addEventListener("submit", function (event) {
    event.preventDefault();
    forms.forEach(function (form) {
      Array.prototype.slice.call(form.elements).forEach(function (element) {
        if (!element.name || element.tagName === "BUTTON" || skip[element.name]) {
          return;
        }
        if (
          (element.type === "checkbox" || element.type === "radio") &&
          !element.checked
        ) {
          return;
        }
        var copy = document.createElement("input");
        copy.type = "hidden";
        copy.name = element.name;
        copy.value = element.value;
        saveAll.appendChild(copy);
      });
    });
    saveAll.submit();
  });
})();
