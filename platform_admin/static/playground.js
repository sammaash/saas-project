(function () {
  "use strict";

  var app = document.getElementById("playground-app");
  if (!app) {
    return;
  }

  var apiUrl = app.dataset.apiUrl;
  var pending = false;

  function messagesElement() {
    return document.getElementById("playground-messages");
  }

  function scrollMessages() {
    var messages = messagesElement();
    if (messages) {
      messages.scrollTop = messages.scrollHeight;
    }
  }

  function setTyping(visible) {
    var messages = messagesElement();
    var indicator = messages && messages.querySelector(".typing-indicator");
    if (indicator) {
      indicator.hidden = !visible;
    }
    scrollMessages();
  }

  function appendBubble(role, text, isError) {
    var messages = messagesElement();
    if (!messages) {
      return;
    }
    var bubble = document.createElement("div");
    bubble.className = "bubble " +
      (role === "user" ? "customer-bubble" : "business-bubble") +
      (isError ? " error-bubble" : "");
    var paragraph = document.createElement("p");
    paragraph.textContent = text;
    var metadata = document.createElement("span");
    metadata.className = "message-meta";
    metadata.textContent = new Date().toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit"
    });
    if (role === "user") {
      metadata.textContent += " \u2713\u2713";
    }
    var indicator = messages.querySelector(".typing-indicator");
    bubble.appendChild(paragraph);
    bubble.appendChild(metadata);
    messages.insertBefore(bubble, indicator || null);
    scrollMessages();
  }

  function setPending(value) {
    pending = value;
    app.classList.toggle("is-pending", value);
    app.querySelectorAll("button[type='submit']").forEach(function (button) {
      button.disabled = value;
    });
  }

  function showStatus(message, isError) {
    var status = document.getElementById("playground-status");
    if (!status) {
      return;
    }
    status.textContent = message || "";
    status.classList.toggle("error", Boolean(isError));
    status.hidden = !message;
  }

  function updateCartButton(cart) {
    var button = app.querySelector("[data-cart-button]");
    if (!button || !cart) {
      return;
    }
    button.textContent = "View cart" +
      (cart.item_count ? " (" + cart.item_count + ")" : "");
  }

  async function submitForm(form, submitter) {
    if (pending) {
      return;
    }

    var data = new FormData(form);
    if (submitter && submitter.name) {
      data.set(submitter.name, submitter.value);
    }
    var action = data.get("start_new") === "1"
      ? "reset"
      : (data.get("action") || "message");
    var messageInput = form.querySelector("[name='customer_message']");
    var optimisticMessage = action === "message" && messageInput
      ? messageInput.value.trim()
      : "";
    if (action === "message" && !optimisticMessage) {
      messageInput.reportValidity();
      return;
    }

    showStatus("", false);
    if (optimisticMessage) {
      appendBubble("user", optimisticMessage, false);
      messageInput.value = "";
      setTyping(true);
    }
    setPending(true);

    try {
      var csrfToken = data.get("csrf_token");
      var response = await fetch(apiUrl, {
        method: "POST",
        body: data,
        credentials: "same-origin",
        headers: {
          "Accept": "application/json",
          "X-CSRF-Token": csrfToken || ""
        }
      });
      var contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) {
        throw new Error("Your session may have expired. Reload the page and try again.");
      }
      var payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "The simulator could not complete that request.");
      }

      var messages = messagesElement();
      var panel = document.getElementById("playground-panel");
      if (messages && typeof payload.chat_html === "string") {
        messages.innerHTML = payload.chat_html;
      }
      if (panel && typeof payload.panel_html === "string") {
        panel.innerHTML = payload.panel_html;
      }
      updateCartButton(payload.cart);
      showStatus(payload.notice || "", false);
      scrollMessages();
    } catch (error) {
      setTyping(false);
      var safeMessage = error instanceof Error
        ? error.message
        : "The simulator could not complete that request.";
      appendBubble("assistant", safeMessage, true);
      showStatus(safeMessage, true);
      if (messageInput && optimisticMessage) {
        messageInput.value = optimisticMessage;
      }
    } finally {
      setPending(false);
      var currentInput = document.getElementById("customer-message");
      if (currentInput) {
        currentInput.focus();
      }
    }
  }

  app.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement) ||
        form.method.toLowerCase() !== "post") {
      return;
    }
    event.preventDefault();
    submitForm(form, event.submitter);
  });

  app.addEventListener("keydown", function (event) {
    if (event.target.id !== "customer-message" ||
        event.key !== "Enter" ||
        event.shiftKey) {
      return;
    }
    event.preventDefault();
    if (!pending) {
      event.target.form.requestSubmit();
    }
  });

  scrollMessages();
}());
