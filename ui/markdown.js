/*
 * 极简 Markdown 渲染，零依赖、纯离线。
 * 支持：标题、粗体/斜体/删除线、行内代码与围栏代码块（带复制按钮）、
 *       有序/无序列表、引用、分隔线、链接、表格。
 * 所有文本先转义再插入，避免 XSS。
 */
(function (global) {
  "use strict";

  function escapeHtml(s) {
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function inline(text) {
    let s = escapeHtml(text);
    // 行内代码（先处理，避免里面的 * _ 被后续规则吃掉）
    s = s.replace(/`([^`]+)`/g, function (_m, c) {
      return "<code>" + c + "</code>";
    });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    s = s.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>'
    );
    return s;
  }

  function render(md) {
    const lines = md.replace(/\r\n/g, "\n").split("\n");
    let html = "";
    let i = 0;
    let para = [];

    function flush() {
      if (para.length) {
        html += "<p>" + inline(para.join(" ")) + "</p>";
        para = [];
      }
    }

    while (i < lines.length) {
      const line = lines[i];

      // 围栏代码块
      if (/^```/.test(line)) {
        flush();
        const lang = line.slice(3).trim() || "text";
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        i++; // 跳过结束的 ```
        const raw = buf.join("\n");
        html +=
          '<div class="code-block"><div class="code-head"><span class="code-lang">' +
          escapeHtml(lang) +
          '</span><button class="copy-btn" data-code="' +
          encodeURIComponent(raw) +
          '">复制</button></div><pre><code>' +
          highlight(raw) +
          "</code></pre></div>";
        continue;
      }

      if (/^\s*$/.test(line)) {
        flush();
        i++;
        continue;
      }

      // 分隔线
      if (/^(---+|\*\*\*+)$/.test(line.trim())) {
        flush();
        html += "<hr>";
        i++;
        continue;
      }

      // 标题
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        flush();
        const lvl = h[1].length;
        html += "<h" + lvl + ">" + inline(h[2]) + "</h" + lvl + ">";
        i++;
        continue;
      }

      // 引用
      if (/^>\s?/.test(line)) {
        flush();
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^>\s?/, ""));
          i++;
        }
        html += "<blockquote>" + render(buf.join("\n")) + "</blockquote>";
        continue;
      }

      // 无序列表
      if (/^\s*[-*+]\s+/.test(line)) {
        flush();
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
          i++;
        }
        html +=
          "<ul>" + items.map((it) => "<li>" + inline(it) + "</li>").join("") + "</ul>";
        continue;
      }

      // 有序列表
      if (/^\s*\d+\.\s+/.test(line)) {
        flush();
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
          i++;
        }
        html +=
          "<ol>" + items.map((it) => "<li>" + inline(it) + "</li>").join("") + "</ol>";
        continue;
      }

      // 表格
      if (/^\|.*\|$/.test(line.trim())) {
        flush();
        const header = line
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((s) => s.trim());
        i++;
        if (i < lines.length && /^\|[\s:|-]+\|$/.test(lines[i].trim())) i++;
        const rows = [];
        while (i < lines.length && /^\|.*\|$/.test(lines[i].trim())) {
          rows.push(
            lines[i]
              .trim()
              .replace(/^\||\|$/g, "")
              .split("|")
              .map((s) => s.trim())
          );
          i++;
        }
        html +=
          "<table><thead><tr>" +
          header.map((c) => "<th>" + inline(c) + "</th>").join("") +
          "</tr></thead><tbody>" +
          rows
            .map(
              (r) =>
                "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>"
            )
            .join("") +
          "</tbody></table>";
        continue;
      }

      // 普通段落
      para.push(line);
      i++;
    }
    flush();
    return html;
  }

  // 极轻量语法高亮（正则匹配，零依赖）：注释 / 字符串 / 数字 / 关键字
  const KEYWORDS = new Set(
    ("if else elif for while do break continue return function def class import from " +
      "as in of new const let var void public private protected static async await try " +
      "catch finally throw switch case default pass yield with lambda nullptr true false " +
      "null None True False and or not is None True False int float str bool list dict " +
      "set tuple map struct enum interface type package func go defer chan select " +
      "then fi echo export source git npm pip python print console log require module " +
      "extends implements super this self public private int double string boolean long " +
      "short byte char unsigned sizeof typedef struct union enum include define ifdef")
      .split(/\s+/)
      .filter(Boolean)
  );

  function highlight(code) {
    const pat =
      /(\/\/[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_$][A-Za-z0-9_$]*)/g;
    let out = "";
    let last = 0;
    let m;
    while ((m = pat.exec(code))) {
      out += escapeHtml(code.slice(last, m.index));
      const txt = m[0];
      if (m[1]) out += '<span class="tk-comment">' + escapeHtml(txt) + "</span>";
      else if (m[2]) out += '<span class="tk-string">' + escapeHtml(txt) + "</span>";
      else if (m[3]) out += '<span class="tk-num">' + escapeHtml(txt) + "</span>";
      else if (m[4] && KEYWORDS.has(txt))
        out += '<span class="tk-kw">' + escapeHtml(txt) + "</span>";
      else out += escapeHtml(txt);
      last = m.index + txt.length;
    }
    out += escapeHtml(code.slice(last));
    return out;
  }

  global.MD = { render: render, escapeHtml: escapeHtml, highlight: highlight };
})(window);
