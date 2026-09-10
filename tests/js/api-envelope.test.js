/* API 封装纯逻辑测试
   运行：node --test tests/js/
   覆盖统一 envelope 解析（{ok,data|error}）与查询串构造。 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const api = require('../../static/js/api.js');

/* ---------------------------------------------------------- 双环境导出 */

test('解析函数同时导出到 module.exports 与 AIBAR.api', () => {
  assert.strictEqual(typeof api.parseEnvelope, 'function');
  assert.strictEqual(globalThis.AIBAR.api.parseEnvelope, api.parseEnvelope);
  // buildQuery 一直是通过 AIBAR.api 暴露的（本文件下面第 80 行起就用它）；
  // 这里曾经断言它是 undefined，与同文件的用法直接打架。
  assert.strictEqual(typeof globalThis.AIBAR.api.buildQuery, 'function');
  assert.strictEqual(globalThis.AIBAR.api.buildQuery, api.buildQuery);
});

/* ---------------------------------------------------------- parseEnvelope */

test('成功响应取 data 字段', () => {
  const result = api.parseEnvelope({ ok: true, data: { total: 3, items: [] } });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.error, null);
  assert.strictEqual(result.data.total, 3);
});

test('失败响应提取 error.code 与 error.message', () => {
  const result = api.parseEnvelope({
    ok: false,
    error: { code: 'comfyui_offline', message: 'ComfyUI 未运行' }
  });
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.data, null);
  assert.strictEqual(result.error.code, 'comfyui_offline');
  assert.strictEqual(result.error.message, 'ComfyUI 未运行');
});

test('error 为字符串时补齐默认 code', () => {
  const result = api.parseEnvelope({ ok: false, error: '图片上传失败' });
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.error.code, 'error');
  assert.strictEqual(result.error.message, '图片上传失败');
});

test('ok:false 且缺少 error 时给出可读兜底', () => {
  const result = api.parseEnvelope({ ok: false });
  assert.strictEqual(result.ok, false);
  // 走的是 normalizeError(undefined)，与下面 normalizeError(null) 同一条分支，
  // 因此是 unknown_error 而不是 error（本用例原先写成 error，与第 73 行自相矛盾）。
  assert.strictEqual(result.error.code, 'unknown_error');
  assert.strictEqual(result.error.message, '请求失败');
  // error 是字符串时才会补成 code='error'
  assert.strictEqual(api.parseEnvelope({ ok: false, error: '' }).error.code, 'unknown_error');
  assert.strictEqual(api.parseEnvelope({ ok: false, error: '坏了' }).error.code, 'error');
});

test('非对象响应标记为 bad_response', () => {
  assert.strictEqual(api.parseEnvelope(null).error.code, 'bad_response');
  assert.strictEqual(api.parseEnvelope(undefined).error.code, 'bad_response');
  assert.strictEqual(api.parseEnvelope('oops').error.code, 'bad_response');
  assert.strictEqual(api.parseEnvelope(42).error.code, 'bad_response');
});

test('无 ok 字段的历史接口响应整体作为 data', () => {
  const result = api.parseEnvelope({ items: [], total: 0 });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.data.total, 0);
});

test('ok:true 但无 data 字段时不丢内容', () => {
  const result = api.parseEnvelope({ ok: true });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.data.ok, true);
});

test('normalizeError 对未知 error 形态给出默认文案', () => {
  assert.deepStrictEqual(api.normalizeError(null), { code: 'unknown_error', message: '请求失败' });
  assert.deepStrictEqual(api.normalizeError({ code: 'x' }), { code: 'x', message: '请求失败' });
});

/* ---------------------------------------------------------- buildQuery */

test('查询串跳过空值并对参数编码', () => {
  assert.strictEqual(api.buildQuery({ media_type: 'image', q: '', page: 1 }), '?media_type=image&page=1');
  assert.strictEqual(api.buildQuery({ q: 'a b&c' }), '?q=a%20b%26c');
  assert.strictEqual(api.buildQuery({ q: '少女' }), '?q=%E5%B0%91%E5%A5%B3');
});

test('无有效参数时返回空串', () => {
  assert.strictEqual(api.buildQuery({}), '');
  assert.strictEqual(api.buildQuery(null), '');
  assert.strictEqual(api.buildQuery(undefined), '');
  assert.strictEqual(api.buildQuery({ q: null, page: undefined, size: '' }), '');
});

test('page_size / page 等数值参数按顺序拼接', () => {
  assert.strictEqual(api.buildQuery({ page: 2, page_size: 24 }), '?page=2&page_size=24');
});
