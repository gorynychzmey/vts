// A small markdown renderer for prompt results (summaries, memos) in the MCP
// summary view. It builds DOM nodes and sets text with textContent — never
// innerHTML — so a result is shown, not executed: LLM output is data like a
// transcript is.
//
// Covers what the summary prompts produce: headings, paragraphs, bullet and
// numbered lists (one level of nesting by indentation), block quotes, rules,
// fenced code, and inline **bold**, *italic* / _italic_, `code`. A link shows
// its text; the sandboxed view does not navigate.
var VtsMarkdown = (function() {
  // Inline spans, appended to `parent`.
  function inline(parent, text) {
    var re = /(\*\*([^*]+)\*\*|__([^_]+)__|`([^`]+)`|\*([^*\s][^*]*)\*|_([^_\s][^_]*)_|\[([^\]]+)\]\(([^)\s]+)\))/;
    var rest = String(text);
    while (rest) {
      var m = re.exec(rest);
      if (!m) { parent.appendChild(document.createTextNode(rest)); break; }
      if (m.index) parent.appendChild(document.createTextNode(rest.slice(0, m.index)));
      var el;
      if (m[2] !== undefined || m[3] !== undefined) {
        el = document.createElement("strong"); inline(el, m[2] !== undefined ? m[2] : m[3]);
      } else if (m[4] !== undefined) {
        el = document.createElement("code"); el.textContent = m[4];
      } else if (m[5] !== undefined || m[6] !== undefined) {
        el = document.createElement("em"); inline(el, m[5] !== undefined ? m[5] : m[6]);
      } else {
        el = document.createElement("span"); el.className = "md-link"; el.title = m[8]; inline(el, m[7]);
      }
      parent.appendChild(el);
      rest = rest.slice(m.index + m[0].length);
    }
  }

  function render(container, source) {
    container.innerHTML = "";
    var lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
    var para = null;
    var lists = []; // stack of {el, indent, ordered}
    function closePara() { para = null; }
    function closeLists() { lists = []; }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (/^\s*```/.test(line)) {
        closePara(); closeLists();
        var pre = document.createElement("pre");
        var code = document.createElement("code");
        var body = [];
        for (i++; i < lines.length && !/^\s*```/.test(lines[i]); i++) body.push(lines[i]);
        code.textContent = body.join("\n");
        pre.appendChild(code);
        container.appendChild(pre);
        continue;
      }
      if (!line.trim()) { closePara(); closeLists(); continue; }
      var h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        closePara(); closeLists();
        // Shifted down one level: the view's own title is the h1.
        var hd = document.createElement("h" + Math.min(6, h[1].length + 1));
        inline(hd, h[2].replace(/\s*#+\s*$/, ""));
        container.appendChild(hd);
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        closePara(); closeLists();
        container.appendChild(document.createElement("hr"));
        continue;
      }
      var q = /^\s*>\s?(.*)$/.exec(line);
      if (q) {
        closePara(); closeLists();
        var bq = container.lastChild && container.lastChild.tagName === "BLOCKQUOTE"
          ? container.lastChild : container.appendChild(document.createElement("blockquote"));
        var qp = document.createElement("p");
        inline(qp, q[1]);
        bq.appendChild(qp);
        continue;
      }
      var li = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (li) {
        closePara();
        var indent = li[1].replace(/\t/g, "  ").length;
        var ordered = /\d/.test(li[2]);
        while (lists.length && indent < lists[lists.length - 1].indent) lists.pop();
        var top = lists[lists.length - 1];
        if (!top || indent > top.indent || top.ordered !== ordered) {
          var list = document.createElement(ordered ? "ol" : "ul");
          var parent = top && indent > top.indent && top.el.lastChild ? top.el.lastChild : container;
          if (top && indent <= top.indent) { lists.pop(); parent = lists.length ? lists[lists.length - 1].el.lastChild : container; }
          parent.appendChild(list);
          lists.push({ el: list, indent: indent, ordered: ordered });
        }
        var item = document.createElement("li");
        inline(item, li[3]);
        lists[lists.length - 1].el.appendChild(item);
        continue;
      }
      // A continuation line of a list item, or ordinary paragraph text.
      if (lists.length && /^\s+\S/.test(line)) {
        var last = lists[lists.length - 1].el.lastChild;
        last.appendChild(document.createTextNode(" "));
        inline(last, line.trim());
        continue;
      }
      closeLists();
      if (!para) { para = document.createElement("p"); container.appendChild(para); }
      else para.appendChild(document.createTextNode(" "));
      inline(para, line.trim());
    }
  }

  return { render: render };
})();
