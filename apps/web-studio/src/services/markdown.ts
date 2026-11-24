/**
 * Minimal, safe markdown renderer for assistant replies.
 *
 * The platform ships no markdown dependency, and model output is untrusted, so
 * everything is HTML-escaped first and only a small, explicit subset is turned
 * back into markup: fenced code blocks, inline code, bold, italic, links,
 * bullet and numbered lists, and paragraph breaks.
 */
function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderInline(value: string): string {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
}

export function renderMarkdown(source: string): string {
  const text = source.replace(/\r\n/g, "\n");
  const blocks: string[] = [];
  const withPlaceholders = text.replace(/```([\w-]*)\n([\s\S]*?)```/g, (_match, language: string, code: string) => {
    const index = blocks.length;
    const label = language ? `<span class="code-lang">${escapeHtml(language)}</span>` : "";
    blocks.push(`<pre class="code-block">${label}<code>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${index}\u0000`;
  });

  const lines = withPlaceholders.split("\n");
  const html: string[] = [];
  let list: "ul" | "ol" | null = null;
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    html.push(`<p>${paragraph.map(renderInline).join("<br />")}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (list) {
      html.push(`</${list}>`);
      list = null;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const placeholder = /^\u0000(\d+)\u0000$/.exec(line.trim());
    if (placeholder) {
      flushParagraph();
      closeList();
      html.push(blocks[Number(placeholder[1])] ?? "");
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      closeList();
      continue;
    }
    const bullet = /^[-*]\s+(.*)$/.exec(line);
    if (bullet) {
      flushParagraph();
      if (list !== "ul") {
        closeList();
        html.push("<ul>");
        list = "ul";
      }
      html.push(`<li>${renderInline(bullet[1])}</li>`);
      continue;
    }
    const ordered = /^\d+\.\s+(.*)$/.exec(line);
    if (ordered) {
      flushParagraph();
      if (list !== "ol") {
        closeList();
        html.push("<ol>");
        list = "ol";
      }
      html.push(`<li>${renderInline(ordered[1])}</li>`);
      continue;
    }
    closeList();
    paragraph.push(line);
  }

  flushParagraph();
  closeList();
  return html.join("");
}
