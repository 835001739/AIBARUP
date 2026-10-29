/* 本地 GPT 连接入口的接线测试

   运行：node --test tests/js/ai-provider-wiring.test.js
*/

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const INDEX = read('static/index.html');
const STUDIO = read('static/js/studio.js');
const CSS = read('static/css/pages.css');

test('提示词工作台提供本地 GPT 状态胶囊', () => {
  assert.match(INDEX, /id="btn-ai-provider"/);
  assert.match(INDEX, /id="ai-provider-dot"/);
  assert.match(INDEX, /id="ai-provider-label"/);
  assert.match(INDEX, /本地 GPT/);
});

test('studio.js 绑定状态读取、弹窗与一键自检', () => {
  assert.match(STUDIO, /getElementById\('btn-ai-provider'\)/);
  assert.match(STUDIO, /bindAIProvider\(\)/);
  assert.match(STUDIO, /\/api\/prompts\/ai-provider\/status/);
  assert.match(STUDIO, /\/api\/prompts\/ai-provider\/test/);
  assert.match(STUDIO, /function openAIProviderModal/);
  assert.match(STUDIO, /function testAIProvider/);
  assert.match(STUDIO, /AI_PROVIDER_ENDPOINT=http:\/\/127\.0\.0\.1:11434\/v1/);
});

test('连接弹窗展示诊断信息与配置示例', () => {
  ['端点', '模型', 'API Key', '超时', '在 .env 中配置', '修改 .env 后需要重启 AIBAR'].forEach((text) => {
    assert.ok(STUDIO.includes(text), '缺少诊断文案：' + text);
  });
});

test('状态胶囊与弹窗样式齐全且使用暗色设计令牌', () => {
  ['.ai-provider-chip {', '.ai-provider-dot {', '.ai-provider-panel {', '.ai-provider-alert {', '.ai-provider-code {'].forEach((selector) => {
    assert.ok(CSS.includes(selector), '缺少样式：' + selector);
  });
  assert.match(CSS, /var\(--color-state-success\)/);
  assert.match(CSS, /var\(--color-state-warning-soft\)/);
});

test('高级设置提供扩写引擎选择并接入 expand 请求', () => {
  assert.match(INDEX, /id="opt-provider"/);
  assert.match(STUDIO, /getElementById\('opt-provider'\)/);
  assert.match(STUDIO, /function fillProviderOptions/);
  assert.match(STUDIO, /openai_compatible/);
  assert.match(STUDIO, /provider:\s*refs\.provider \? refs\.provider\.value : 'rules'/);
  assert.ok(!STUDIO.includes("provider: 'rules'"), '扩写请求不应再硬编码 rules');
});
