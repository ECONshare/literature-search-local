/* What every page of LitReview shares: copy buttons, confirmations, file
   pickers that send at once, messages for refusals, the eye that shows a
   hidden key, and the Quit button.
   Listeners are delegated from the document, so they work for content that
   HTMX swaps in later. */
(function () {
  "use strict";

  /* ------------------------------------------------------------ copying */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        if (document.execCommand("copy")) resolve(); else reject(new Error("copy"));
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  function confirmButton(button, words) {
    if (!button.hasAttribute("data-label")) {
      button.setAttribute("data-label", button.textContent);
    }
    button.textContent = words;
    clearTimeout(button._litreviewTimer);
    button._litreviewTimer = setTimeout(function () {
      button.textContent = button.getAttribute("data-label");
    }, 1800);
  }

  function copyFrom(button, text) {
    copyText(text).then(function () {
      confirmButton(button, "Copied");
    }, function () {
      confirmButton(button, "Select it and copy");
    });
  }

  /* Copy an element as formatted text (italic journal names survive a paste
     into a word processor) with plain text beside it for everything else. */
  function copyRich(button, el) {
    var plain = textOf(el);
    var items = el.querySelectorAll("li");
    var markup = items.length
      ? Array.prototype.map.call(items, function (li) { return "<p>" + li.innerHTML + "</p>"; }).join("")
      : el.innerHTML;
    if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write &&
        window.isSecureContext) {
      var item = new window.ClipboardItem({
        "text/html": new Blob([markup], {type: "text/html"}),
        "text/plain": new Blob([plain], {type: "text/plain"})
      });
      navigator.clipboard.write([item]).then(function () {
        confirmButton(button, "Copied");
      }, function () { copyFrom(button, plain); });
      return;
    }
    copyFrom(button, plain);
  }

  /* The text of an element: a list as one line per item. */
  function textOf(el) {
    var items = el.querySelectorAll("li");
    if (items.length) {
      return Array.prototype.map.call(items, function (li) {
        return li.textContent.replace(/\s+/g, " ").trim();
      }).join("\n");
    }
    return el.textContent.trim();
  }

  window.LitReview = {copyText: copyText, copyFrom: copyFrom, toast: toast};

  /* ------------------------------------------------------------ messages */
  function toast(message) {
    var box = document.getElementById("toast");
    if (!box) { window.alert(message); return; }
    box.textContent = message;
    box.hidden = false;
    clearTimeout(box._litreviewTimer);
    box._litreviewTimer = setTimeout(function () { box.hidden = true; }, 9000);
  }

  document.addEventListener("click", function (e) {
    var target = e.target;
    if (!target || !target.closest) return;

    var copy = target.closest("[data-copy]");
    if (copy) { copyFrom(copy, copy.getAttribute("data-copy")); return; }

    var copyEl = target.closest("[data-copy-from]");
    if (copyEl) {
      var source = document.querySelector(copyEl.getAttribute("data-copy-from"));
      if (source) copyFrom(copyEl, textOf(source));
      return;
    }

    var richEl = target.closest("[data-copy-rich]");
    if (richEl) {
      var rich = document.querySelector(richEl.getAttribute("data-copy-rich"));
      if (rich) copyRich(richEl, rich);
      return;
    }

    var remove = target.closest("[data-remove]");
    if (remove) {
      var gone = document.getElementById(remove.getAttribute("data-remove"));
      if (gone) gone.remove();
      return;
    }

    /* The "i" of a hover-over: a tap shows its text, as hovering does
       (Safari does not give a tapped button the focus by itself). */
    var tip = target.closest(".tip-button");
    if (tip) { tip.focus(); return; }

    /* The eye beside a hidden key shows it, and hides it again. */
    var reveal = target.closest("[data-reveal]");
    if (reveal) {
      var secret = document.getElementById(reveal.getAttribute("data-reveal"));
      if (secret) {
        var show = secret.type === "password";
        secret.type = show ? "text" : "password";
        reveal.classList.toggle("is-shown", show);
        var words = reveal.getAttribute(show ? "data-hide" : "data-show");
        if (words) {
          reveal.setAttribute("aria-label", words);
          reveal.setAttribute("title", words);
        }
      }
      return;
    }

    if (target.id === "toast") { target.hidden = true; return; }

    /* The highlighted-PDF viewer: jump to a passage's page. Changing only
       the #page of an iframe does not move the browser's PDF viewer, so a
       throwaway query parameter makes it load again. */
    var finding = target.closest(".finding[data-page]");
    var frame = document.getElementById("pdf-frame");
    if (finding && frame) {
      frame._litreviewBump = (frame._litreviewBump || 0) + 1;
      frame.src = frame.getAttribute("data-src") + "?v=" + frame._litreviewBump +
                  "#page=" + finding.getAttribute("data-page");
      return;
    }

    var quit = target.closest("[data-quit]");
    if (quit) {
      if (!window.confirm("Quit LitReview? It stops running until you start it again.")) return;
      quit.disabled = true;
      // The header stops asking a server that is going away (htmx drops a
      // poll whose element has left the page).
      var indicator = document.getElementById("claude-indicator");
      if (indicator) {
        var stopped = document.createElement("span");
        stopped.className = "claude-indicator";
        stopped.textContent = "LitReview has stopped";
        indicator.replaceWith(stopped);
      }
      var result = document.querySelector("[data-quit-result]");
      fetch("/quit", {method: "POST"}).then(function (r) { return r.json(); })
        .then(function (data) {
          if (result) {
            result.hidden = false;
            result.textContent = data.stopping
              ? "LitReview has stopped. You can close this tab; start LitReview again with its launcher."
              : (data.note || "Close the LitReview window to stop it.");
          }
        }, function () {
          if (result) {
            result.hidden = false;
            result.textContent = "LitReview has stopped. You can close this tab.";
          }
        });
    }
  });

  /* A form with data-confirm asks first. (Forms sent through HTMX use
     hx-confirm instead.) */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form && form.getAttribute && form.getAttribute("data-confirm") &&
        !window.confirm(form.getAttribute("data-confirm"))) {
      e.preventDefault();
      e.stopImmediatePropagation();
    }
  }, true);

  /* Choosing a file sends the form it is in. */
  document.addEventListener("change", function (e) {
    var input = e.target;
    if (input && input.matches && input.matches("input[type=file][data-autosubmit]") &&
        input.files && input.files.length && input.form) {
      if (input.form.requestSubmit) input.form.requestSubmit(); else input.form.submit();
    }
  });

  /* ------------------------------------------------------------ HTMX */
  /* A refusal with a body is a panel to show (the answer's save conflict,
     which hands the unsaved text back). The others come as a toast. */
  document.addEventListener("htmx:beforeSwap", function (e) {
    var xhr = e.detail.xhr;
    if (xhr.status >= 400 && xhr.status < 500 && xhr.responseText &&
        xhr.getResponseHeader("HX-Reswap") !== "none") {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });
  document.addEventListener("litreview:error", function (e) {
    toast((e.detail && (e.detail.message || e.detail.value)) || "That did not work.");
  });
  document.addEventListener("htmx:sendError", function () {
    toast("LitReview is not answering. Is it still running? Start it again with its launcher.");
  });
  document.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr.status >= 500) {
      toast("Something went wrong inside LitReview. Its log, in the logs folder of your " +
            "library, says what.");
    }
  });
})();
