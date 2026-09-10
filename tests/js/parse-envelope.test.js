/* 统一响应信封解析 / 错误归一化 / 查询串拼接（docs/API_CONTRACT.md） */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { parseEnvelope, normalizeError, buildQuery } = require('../../static/js/api.js');

test('parseEnvelope：标准成功信封解包出 data', () => {
  const result = parseEnvelope({ ok: true, data: { id: 7, title: '逆光' } });
  assert.strictEqual(result.ok, true);
  assert.deepStrictEqual(result.data, { id: 7, title: '逆光' });
  assert.strictEqual(result.error, null);
});

test('parseEnvelope：没有 data 字段时整个响应体作为 data', () => {
  const payload = { items: [1, 2], total: 2 };
  const result = parseEnvelope(payload);
  assert.strictEqual(result.ok, true);
  assert.deepStrictEqual(result.data, payload);
});

test('parseEnvelope：data 为 null 也算成功', () => {
  const result = parseEnvelope({ ok: true, data: null });
  assert.strictEqual(result.ok, true);
  assert.strictEqual(result.data, null);
});

test('parseEnvelope：ok:false 抽出结构化错误', () => {
  const result = parseEnvelope({ ok: false, error: { code: 'not_found', message: '没有该资源' } });
  assert.strictEqual(result.ok, false);
  assert.strictEqual(result.data, null);
  assert.deepStrictEqual(result.error, { code: 'not_found', message: '没有该资源' });
});

test('parseEnvelope：ok:false 缺 error 时兜底未知错误', () => {
  const result = parseEnvelope({ ok: false });
  assert.strictEqual(result.ok, false);
  assert.deepStrictEqual(result.error, { code: 'unknown_error', message: '请求失败' });
});

test('parseEnvelope：error 为字符串时也归一成对象', () => {
  assert.deepStrictEqual(parseEnvelope({ ok: false, error: '服务繁忙' }).error,
    { code: 'error', message: '服务繁忙' });
});

test('parseEnvelope：非对象响应一律判为格式错误', () => {
  [null, undefined, 0, '', 'not json'].forEach((payload) => {
    const result = parseEnvelope(payload);
    assert.strictEqual(result.ok, false, '非对象响应必须判为失败');
    assert.strictEqual(result.data, null);
    assert.strictEqual(result.error.code, 'bad_response');
  });
});

test('normalizeError：补齐缺失的 code 或 message', () => {
  assert.deepStrictEqual(normalizeError({ code: 'offline' }), { code: 'offline', message: '请求失败' });
  assert.deepStrictEqual(normalizeError({ message: '炸了' }), { code: 'error', message: '炸了' });
  assert.deepStrictEqual(normalizeError({ code: 500, message: 500 }), { code: '500', message: '500' });
});

test('normalizeError：空值与非对象退回 unknown_error', () => {
  [null, undefined, '', 0, 42].forEach((value) => {
    assert.deepStrictEqual(normalizeError(value), { code: 'unknown_error', message: '请求失败' });
  });
});

test('buildQuery：跳过空值但保留 0 与 false', () => {
  assert.strictEqual(buildQuery(null), '');
  assert.strictEqual(buildQuery(undefined), '');
  assert.strictEqual(buildQuery({}), '');
  assert.strictEqual(buildQuery({ page: 1, page_size: 24 }), '?page=1&page_size=24');
  assert.strictEqual(buildQuery({ q: '', page: 1 }), '?page=1');
  assert.strictEqual(buildQuery({ page: 0, favorite: false }), '?page=0&favorite=false');
});

test('buildQuery：键与值都做 URL 编码', () => {
  assert.strictEqual(buildQuery({ 'q': '逆光 边缘' }), '?q=%E9%80%86%E5%85%89%20%E8%BE%B9%E7%BC%98');
  assert.strictEqual(buildQuery({ 'a b': 'c&d' }), '?a%20b=c%26d');
});
