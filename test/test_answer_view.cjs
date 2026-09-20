const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const app = fs.readFileSync(path.join(__dirname, '../web/static/js/chat.js'), 'utf8');
const context = {};
vm.runInNewContext(app.slice(app.indexOf('function streamText('), app.indexOf('function sourceTitle(')), context);
const { streamText } = context;

// 逐一检查所有可能的增量分段位置，确保内部引用标记和图片传输文本不会显示。
const wireAnswer = 'Answer[cite:12]\n\n【图片】\nhttps://example.com/diagram.png';
for (let end = 0; end <= wireAnswer.length; end++) {
  const shown = streamText(wireAnswer.slice(0, end));
  assert.ok(!/[\[【]|https?:|example\.com/.test(shown), `Leaked metadata at boundary ${end}: ${shown}`);
}
assert.equal(streamText('根据参考内容，步骤（参考内容[4]）。'), '步骤。');
assert.equal(streamText('步骤（参考[4][2]）。'), '步骤。');
assert.equal(streamText('保留 `array[1]` 和 `[cite:1]` 示例'), '保留 `array[1]` 和 `[cite:1]` 示例');
assert.equal(streamText('普通正文与链接 https://example.com'), '普通正文与链接 https://example.com');

const asset = '/assets/11111111-1111-4111-8111-111111111111';
// 每个字符边界都要隐藏尚未校验的图片语法，完成后仍保留后续文字。
const inlineAnswer = `Before\n\n![diagram](${asset})\n\nAfter`;
for (let end = 0; end <= inlineAnswer.length; end++) {
  const shown = streamText(inlineAnswer.slice(0, end));
  assert.ok(!/!|diagram|\/assets\//.test(shown), `Leaked inline image at ${end}: ${shown}`);
}
assert.equal(streamText(inlineAnswer), 'Before\n\n\n\nAfter');

// 使用项目实际的 Markdown 解析器检查插入 DOM 前的地址校验；这里不替代浏览器中的净化验证。
const marked = require('../web/static/vendor/marked.umd.js');
const escapeHTML = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const DOMPurify = {sanitize: html => html};
const renderContext = {marked, DOMPurify, window:{marked,DOMPurify}, escapeHTML, URL, location:{origin:'http://localhost:8000'}};
vm.runInNewContext(app.slice(app.indexOf('function safeImageURL('), app.indexOf('function streamText(')), renderContext);
const html = renderContext.markdown(inlineAnswer, [asset]);
assert.ok(html.indexOf('Before') < html.indexOf('<img '));
assert.ok(html.indexOf('<img ') < html.indexOf('After'));
assert.ok(html.includes(`src="http://localhost:8000${asset}"`));
assert.ok(!renderContext.markdown(inlineAnswer).includes('<img '));
for (const address of ['https://evil.test/x.png', '//evil.test/x.png', asset+'?x=1', asset+'#x', 'javascript:alert(1)', '/assets/22222222-2222-4222-8222-222222222222']) {
  assert.ok(!renderContext.markdown(`![x](${address})`, [asset]).includes('<img '), address);
}
assert.ok(!renderContext.markdown(`<img src="${asset}" srcset="https://evil.test/x">`, [asset]).includes('<img '));
assert.ok(!renderContext.markdown('```md\n'+inlineAnswer+'\n```', [asset]).includes('<img '));
assert.ok(!renderContext.markdown(`![x" onerror="alert(1)](${asset})`, [asset]).includes(' onerror="'));
console.log('Answer inline image whitelist, positioning, stream boundaries and legacy display checks passed.');
