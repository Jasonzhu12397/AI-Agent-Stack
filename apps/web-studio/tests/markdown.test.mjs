import test from "node:test";
import assert from "node:assert/strict";

import { renderMarkdown } from "../src/services/markdown.ts";

test("escapes model output before adding markup", () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)"> & <script>bad()</script>');
  assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;/);
  assert.match(html, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img"), false);
});

test("fenced code keeps its body verbatim and is not re-parsed", () => {
  const html = renderMarkdown('```yaml\napiVersion: v1\n**not bold**\n```');
  assert.match(html, /<pre class="code-block">/);
  assert.match(html, /<span class="code-lang">yaml<\/span>/);
  assert.match(html, /apiVersion: v1\n\*\*not bold\*\*/);
});

test("inline code, bold and links render", () => {
  const html = renderMarkdown("Run `kubectl get bmh` and **recheck** [docs](https://example.com/x)");
  assert.match(html, /<code>kubectl get bmh<\/code>/);
  assert.match(html, /<strong>recheck<\/strong>/);
  assert.match(html, /<a href="https:\/\/example\.com\/x" target="_blank" rel="noopener noreferrer">docs<\/a>/);
});

test("javascript: links never become anchors", () => {
  const html = renderMarkdown("[click](javascript:alert(1))");
  assert.equal(html.includes('href="javascript:'), false);
});

test("bullet and numbered lists group per block", () => {
  const html = renderMarkdown("- one\n- two\n\n1. first\n2. second");
  assert.match(html, /<ul><li>one<\/li><li>two<\/li><\/ul>/);
  assert.match(html, /<ol><li>first<\/li><li>second<\/li><\/ol>/);
});

test("single newlines become line breaks inside one paragraph", () => {
  const html = renderMarkdown("line one\nline two");
  assert.equal(html, "<p>line one<br />line two</p>");
});
