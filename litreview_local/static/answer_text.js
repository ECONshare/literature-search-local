/* The answer as plain text, for "Copy as text" and "Read aloud".

   A manuscript wants citations before the sentence's full stop, and a run of
   them as one parenthesis without repeats:

       "... the variable. (A, 2021)(A, 2021) (B, 2022)"
    -> "... the variable (A, 2021; B, 2022)."

   The server places citations that way when Claude writes an answer, but an
   answer edited by hand can have them anywhere, so the copy does it again.
   A citation followed by a sentence that carries on in lower case ("Smith
   et al. (A) found ...") is left where it is: that full stop is not the end
   of the sentence.

   Plain functions, loadable outside a browser: tests/test_web.py runs them
   under node when node is installed. */
(function (root) {
  "use strict";

  var INLINE_SPACE = /^[ \t\xA0]*$/;
  // Sentence-final punctuation before the citation, with any closing quote
  // or bracket after it (the quote stays, the full stop moves).
  var BEFORE = /([.!?;:]+)([”"’')\]]*)[ \t\xA0]*$/;

  function label(text) {
    var t = String(text || "").replace(/\s+/g, " ").trim();
    var m = t.match(/^\((.*)\)$/);
    return (m ? m[1] : t).trim();
  }

  function unique(list) {
    var out = [];
    list.forEach(function (x) {
      if (x && out.indexOf(x) === -1) out.push(x);
    });
    return out;
  }

  function tidy(text) {
    return String(text)
      .replace(/\xA0/g, " ")
      .replace(/[ \t]+/g, " ")
      .replace(/ +([.,;:!?])/g, "$1")
      .replace(/ *\n */g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  /* parts: [{text: "..."} or {cite: "(Smith, 2020)"}], in reading order. */
  function formatCited(parts) {
    var items = [];
    parts.forEach(function (part) {
      var last = items[items.length - 1];
      if (part.cite !== undefined) {
        if (last && last.cites) { last.cites.push(part.cite); return; }
        // Two citations with only spaces between them are one run.
        if (last && last.text !== undefined && INLINE_SPACE.test(last.text) &&
            items.length > 1 && items[items.length - 2].cites) {
          items.pop();
          items[items.length - 1].cites.push(part.cite);
          return;
        }
        items.push({cites: [part.cite]});
        return;
      }
      var text = String(part.text || "");
      if (last && last.text !== undefined) last.text += text;
      else items.push({text: text});
    });

    var out = "";
    items.forEach(function (item, i) {
      if (item.text !== undefined) { out += item.text; return; }
      var labels = unique(item.cites.map(label));
      if (!labels.length) return;
      var next = items[i + 1];
      var after = next && next.text !== undefined ? next.text : "";
      var carriesOn = /^[ \t\xA0]*[a-z]/.test(after);
      var punct = "";
      var m = carriesOn ? null : out.match(BEFORE);
      if (m) {
        out = out.slice(0, out.length - m[0].length) + m[2];
        punct = m[1];
      }
      out = out.replace(/[ \t\xA0]+$/, "");
      if (out && !/[\s(\[“"'‘]$/.test(out)) out += " ";
      out += "(" + labels.join("; ") + ")";
      // Unless the text after already ends the sentence.
      if (punct && !/^[ \t\xA0]*[.!?;:]/.test(after)) out += punct;
    });
    return tidy(out);
  }

  var BLOCK = /^(P|DIV|UL|OL|H[1-6]|BLOCKQUOTE|SECTION)$/;

  /* The editor's content as parts; citations included or left out. */
  function partsOf(rootNode, withCites) {
    var parts = [];
    function walk(node) {
      if (node.nodeType === 3) { parts.push({text: node.nodeValue}); return; }
      if (node.nodeType !== 1) return;
      var tag = node.tagName.toUpperCase();
      if (tag === "CITE") {
        if (withCites) parts.push({cite: node.textContent});
        return;
      }
      if (tag === "BR") { parts.push({text: "\n"}); return; }
      if (tag === "LI") {
        parts.push({text: "\n- "});
        for (var li = node.firstChild; li; li = li.nextSibling) walk(li);
        parts.push({text: "\n"});
        return;
      }
      var block = BLOCK.test(tag);
      if (block) parts.push({text: "\n\n"});
      for (var c = node.firstChild; c; c = c.nextSibling) walk(c);
      if (block) parts.push({text: "\n\n"});
    }
    for (var c = rootNode.firstChild; c; c = c.nextSibling) walk(c);
    return parts;
  }

  /* Text to speak, in pieces short enough for every browser's voice. */
  function speechChunks(text) {
    var chunks = [];
    String(text || "").split(/\n+/).forEach(function (para) {
      var sentences = para.match(/[^.!?]+[.!?]+[”"’)\]]*\s*|[^.!?]+$/g) || [para];
      var buf = "";
      sentences.forEach(function (s) {
        if (buf && (buf + s).length > 220) { chunks.push(buf.trim()); buf = ""; }
        buf += s;
      });
      if (buf.trim()) chunks.push(buf.trim());
    });
    return chunks;
  }

  var api = {
    formatCited: formatCited,
    tidy: tidy,
    label: label,
    speechChunks: speechChunks,
    answerText: function (node) { return formatCited(partsOf(node, true)); },
    spokenText: function (node) { return formatCited(partsOf(node, false)); }
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LitReviewText = api;
})(typeof window !== "undefined" ? window : this);
