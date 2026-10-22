(function () {
  "use strict";

  var section = document.getElementById("config-menu");
  if (!section) {
    return;
  }

  var form = section.querySelector("form.config-form");
  var preview = document.getElementById("menu-builder-preview");
  if (!form || !preview) {
    return;
  }

  function rows() {
    return Array.prototype.slice.call(section.querySelectorAll("[data-menu-option]"));
  }

  function value(row, name) {
    var input = row.querySelector("[name='" + name + "']");
    return input ? String(input.value || "").trim() : "";
  }

  function action(row, index) {
    var select = row.querySelector("[name='option_action_" + index + "']");
    return select ? select.value : "";
  }

  function refresh() {
    var current = [];
    rows().forEach(function (row, index) {
      var id = value(row, "option_id_" + index);
      var label = value(row, "option_label_" + index);
      var selectedAction = action(row, index);
      var enabledInput = row.querySelector("[name='option_enabled_" + index + "']");
      var position = parseInt(value(row, "option_position_" + index), 10);
      if (!id && !label) {
        return;
      }
      current.push({
        label: label || "(label required)",
        action: selectedAction,
        enabled: enabledInput ? enabledInput.checked : false,
        position: Number.isFinite(position) ? position : 999
      });
    });
    current.sort(function (left, right) {
      return left.position - right.position;
    });

    var welcome = form.querySelector("[name='menu_welcome_message']");
    var welcomeText = welcome && welcome.value.trim() ? welcome.value.trim() : "Main menu:";
    var list = document.createElement("ol");
    var enabled = current.filter(function (item) { return item.enabled; });
    if (!enabled.length) {
      var empty = document.createElement("li");
      empty.className = "muted";
      empty.textContent = "No enabled options will be shown.";
      list.appendChild(empty);
    } else {
      enabled.forEach(function (item) {
        var entry = document.createElement("li");
        var strong = document.createElement("strong");
        strong.textContent = item.label;
        var description = document.createElement("span");
        description.className = "muted";
        description.textContent = " " + item.action.replace(/_/g, " ");
        entry.appendChild(strong);
        entry.appendChild(description);
        list.appendChild(entry);
      });
    }
    preview.innerHTML = "";
    var eyebrow = document.createElement("span");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = "Preview before saving";
    var heading = document.createElement("strong");
    heading.textContent = welcomeText;
    preview.appendChild(eyebrow);
    preview.appendChild(heading);
    preview.appendChild(list);
  }

  form.addEventListener("input", refresh);
  form.addEventListener("change", refresh);

  section.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    if (target.matches("[data-menu-clear]")) {
      var row = target.closest("[data-menu-option]");
      if (!row) {
        return;
      }
      row.querySelectorAll("input").forEach(function (input) {
        if (input.type === "checkbox") {
          input.checked = false;
        } else {
          input.value = "";
        }
      });
      row.querySelectorAll("select").forEach(function (select) {
        select.value = "";
      });
      refresh();
      return;
    }
    if (target.matches("[data-menu-add]")) {
      var emptyRow = rows().find(function (row) {
        var index = rows().indexOf(row);
        return !value(row, "option_id_" + index) && !value(row, "option_label_" + index);
      });
      if (!emptyRow) {
        window.alert("All 10 menu rows are in use.");
        return;
      }
      var emptyIndex = rows().indexOf(emptyRow);
      var idInput = emptyRow.querySelector("[name='option_id_" + emptyIndex + "']");
      if (idInput) {
        idInput.focus();
      }
      refresh();
    }
  });
}());
