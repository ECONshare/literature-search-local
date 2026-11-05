/* The question page: citations that show their passage on hover and open the
   highlighted PDF on click, drag-to-cite from the evidence list, saving the
   edited answer, "Copy as text" and "Read aloud". Listeners are delegated,
   so they keep working when HTMX swaps the answer block. */
(function () {
  "use strict";

  var citmap = {};
  try {
    citmap = JSON.parse(document.getElementById("citmap").textContent) || {};
  } catch (err) { /* a page without citations */ }

  var popup = document.getElementById("cit-popup");
  var LOCATED = {
    pdf: "Highlighted in the PDF.",
    text: "In the paper's text, but not highlighted in the PDF.",
    none: "Not found in the paper — check this quote before relying on it."
  };
  var VERDICT = {
    supported: "Claude checked it: the passage supports the sentence.",
    partial: "Claude checked it: the passage only partly supports the sentence.",
    unsupported: "Claude checked it: the passage does not support the sentence."
  };

  function editor() { return document.getElementById("summary-editor"); }
  function infoOf(cite) { return citmap[cite.getAttribute("data-k")] || null; }
  function viewerUrl(info) {
    return "/annotations/" + info.annotation_id + "/view?page=" + (info.page || 1);
  }

  /* Citations are single tokens in the editor: not typed into, deleted whole. */
  function prepare(root) {
    (root || document).querySelectorAll("#summary-editor cite.cit").forEach(function (el) {
      el.setAttribute("contenteditable", "false");
    });
    var speak = document.querySelector("[data-read-aloud]");
    if (speak && "speechSynthesis" in window) speak.hidden = false;
  }

  /* ------------------------------------------------------- hover popup */
  function line(tag, text, cls) {
    var el = document.createElement(tag);
    el.textContent = text;
    if (cls) el.className = cls;
    popup.appendChild(el);
    return el;
  }

  function showPopup(cite) {
    if (!popup) return;
    var info = infoOf(cite);
    popup.innerHTML = "";
    if (!info) {
      line("strong", "Citation unavailable");
      line("p", "The passage this cited is no longer among the evidence — its paper " +
                "was taken out of the question, or read again. Drag in a current " +
                "passage to replace it.");
    } else {
      line("strong", info.title || info.paper_title || "Passage");
      line("p", (info.apa || "") + " " + (info.paper_title || ""), "pop-paper");
      var quote = info.quote || "";
      line("blockquote", "“" + (quote.length > 480 ? quote.slice(0, 480) + "…" : quote) + "”");
      var where = "p. " + (info.page || 1) + (info.section ? " · " + info.section : "");
      line("p", where + (info.why ? " — " + info.why : ""));
      var located = info.located || (info.matched ? "pdf" : "none");
      line("p", LOCATED[located] || "", "pop-located located-" + located);
      var verdict = cite.getAttribute("data-verdict");
      if (verdict && VERDICT[verdict]) {
        var note = cite.getAttribute("data-note");
        line("p", VERDICT[verdict] + (note ? " " + note : ""), "pop-verdict verdict-" + verdict);
      }
      line("p", "Click to open the PDF at this page.", "pop-hint");
    }
    popup.hidden = false;
    var r = cite.getBoundingClientRect();
    popup.style.left = Math.max(8, Math.min(r.left, window.innerWidth - popup.offsetWidth - 8)) + "px";
    var top = r.bottom + 6;
    if (top + popup.offsetHeight > window.innerHeight - 8) {
      top = Math.max(8, r.top - popup.offsetHeight - 6);
    }
    popup.style.top = top + "px";
  }

  document.addEventListener("mouseover", function (e) {
    var cite = e.target.closest && e.target.closest("cite.cit");
    if (cite) showPopup(cite);
  });
  document.addEventListener("mouseout", function (e) {
    if (popup && e.target.closest && e.target.closest("cite.cit")) popup.hidden = true;
  });

  /* ---------------------------------- click a citation or a passage */
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var cite = e.target.closest("cite.cit");
    if (cite) {
      var info = infoOf(cite);
      if (info) { e.preventDefault(); window.open(viewerUrl(info), "_blank"); }
      return;
    }
    var card = e.target.closest(".ann-card");
    if (card) {
      window.open("/annotations/" + card.getAttribute("data-ann") + "/view?page=" +
                  card.getAttribute("data-page"), "_blank");
    }
  });
  document.addEventListener("keydown", function (e) {
    var card = e.key === "Enter" && e.target.closest && e.target.closest(".ann-card");
    if (card) card.click();
  });

  /* ----------------------------------------------------- drag to cite */
  document.addEventListener("dragstart", function (e) {
    var card = e.target.closest && e.target.closest(".ann-card");
    if (card) {
      e.dataTransfer.setData("text/litreview-cit", card.getAttribute("data-k"));
      e.dataTransfer.effectAllowed = "copy";
    }
  });
  document.addEventListener("dragover", function (e) {
    var target = e.target.closest && e.target.closest("#summary-editor[contenteditable=true]");
    if (target && Array.prototype.indexOf.call(e.dataTransfer.types, "text/litreview-cit") !== -1) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  });
  document.addEventListener("drop", function (e) {
    var box = e.target.closest && e.target.closest("#summary-editor[contenteditable=true]");
    if (!box) return;
    var key = e.dataTransfer.getData("text/litreview-cit");
    var info = citmap[key];
    if (!info) return;
    e.preventDefault();
    var range = null;
    if (document.caretRangeFromPoint) {
      range = document.caretRangeFromPoint(e.clientX, e.clientY);
    } else if (document.caretPositionFromPoint) {
      var pos = document.caretPositionFromPoint(e.clientX, e.clientY);
      if (pos) { range = document.createRange(); range.setStart(pos.offsetNode, pos.offset); }
    }
    if (!range || !box.contains(range.startContainer)) {
      range = document.createRange();
      range.selectNodeContents(box);
      range.collapse(false);
    }
    var cite = document.createElement("cite");
    cite.className = "cit";
    cite.setAttribute("data-k", key);
    cite.setAttribute("contenteditable", "false");
    cite.textContent = info.apa;
    range.collapse(true);
    range.insertNode(document.createTextNode(" "));
    range.insertNode(cite);
    box.focus();
    markDirty(true);
  });

  /* ------------------------------------------- saving, and unsaved edits */
  var dirty = false;
  function markDirty(on) {
    dirty = on;
    var note = document.querySelector("[data-dirty]");
    if (note) note.hidden = !on;
  }
  document.addEventListener("input", function (e) {
    if (e.target && e.target.id === "summary-editor") markDirty(true);
  });
  document.body.addEventListener("htmx:configRequest", function (e) {
    if (e.detail.elt && e.detail.elt.id === "summary-save-form") {
      var box = editor();
      e.detail.parameters.summary_html = box ? box.innerHTML : "";
    }
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    prepare(e.target);
    if (e.target && e.target.id === "summary-block" && e.detail.xhr &&
        e.detail.xhr.status < 400) {
      markDirty(false);
    }
  });
  window.addEventListener("beforeunload", function (e) {
    if (dirty) { e.preventDefault(); e.returnValue = ""; }
  });
  /* Claude's check of the citations has arrived: reload to show it on the
     citations - unless that would throw away an edit. */
  document.body.addEventListener("litreview:verified", function () {
    if (!dirty) { window.location.hash = "verification"; window.location.reload(); }
  });

  /* ----------------------------------------------- copy, and read aloud */
  document.addEventListener("click", function (e) {
    var button = e.target.closest && e.target.closest("[data-answer-copy]");
    if (!button) return;
    var box = editor();
    if (box && window.LitReviewText && window.LitReview) {
      window.LitReview.copyFrom(button, window.LitReviewText.answerText(box));
    }
  });

  var speaking = false;
  function stopReading() {
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    speaking = false;
    var speak = document.querySelector("[data-read-aloud]");
    var stop = document.querySelector("[data-read-stop]");
    if (speak) speak.hidden = false;
    if (stop) stop.hidden = true;
  }
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    if (e.target.closest("[data-read-stop]")) { stopReading(); return; }
    var speak = e.target.closest("[data-read-aloud]");
    var box = editor();
    if (!speak || !box || !("speechSynthesis" in window) || !window.LitReviewText) return;
    window.speechSynthesis.cancel();
    var chunks = window.LitReviewText.speechChunks(window.LitReviewText.spokenText(box));
    if (!chunks.length) return;
    speaking = true;
    speak.hidden = true;
    var stop = document.querySelector("[data-read-stop]");
    if (stop) stop.hidden = false;
    chunks.forEach(function (chunk, i) {
      var utterance = new SpeechSynthesisUtterance(chunk);
      utterance.lang = document.documentElement.lang || "en";
      if (i === chunks.length - 1) utterance.onend = function () { if (speaking) stopReading(); };
      window.speechSynthesis.speak(utterance);
    });
  });
  window.addEventListener("pagehide", function () {
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  });

  prepare(document);
})();
