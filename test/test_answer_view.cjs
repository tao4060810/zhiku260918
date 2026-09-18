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
console.log('Answer stream boundary and legacy display checks passed.');
