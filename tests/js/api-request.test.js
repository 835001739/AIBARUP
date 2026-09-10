/* api.request(path, {method, body, timeout})：统一解包 {ok,data,error}，失败不抛异常 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

require('../../static/js/api.js');

/*
 * api.js 在没有 window 的环境里把命名空间挂到 globalThis，
 * request 依赖全局 fetch 与 AbortController，这里用桩函数替换后再还原。
 */
const request = globalThis.AIBAR.api.request;

function restoreFetch(original) {
  if (typeof original === 'undefined') delete globalThis.fetch;
  else globalThis.fetch = original;
}

/** 用桩 fetch 跑一段断言，结束或失败都还原全局 fetch */
function withFetch(impl, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = impl;
  return Promise.resolve().then(fn).then(function (value) {
    restoreFetch(original);
    return value;
  }, function (err) {
    restoreFetch(original);
    throw err;
  });
}

/*
 * 响应桩**必须返回 Promise**：api.js 里是 fetch(...).then(...)，
 * 返回裸对象会在 .then 处抛 TypeError，8 个用例集体失败——那是桩写错了，
 * 不是产品代码的 bug（详见 docs/OPTIMIZATION_REVIEW.md「已核实不是问题的项」）。
 */
function jsonResponse(body, status) {
  const code = status === undefined ? 200 : status;
  return Promise.resolve({
    ok: code >= 200 && code < 300,
    status: code,
    text: () => Promise.resolve(JSON.stringify(body))
  });
}

function textResponse(raw, status) {
  const code = status === undefined ? 200 : status;
  return Promise.resolve({
    ok: code >= 200 && code < 300,
    status: code,
    text: () => Promise.resolve(raw)
  });
}

test('request：成功时解包出 data', async () => {
  await withFetch(() => jsonResponse({ ok: true, data: { id: 1 } }), async () => {
    const res = await request('/api/ping');
    assert.deepStrictEqual(res, { ok: true, data: { id: 1 }, error: null });
  });
});

test('request：缺省为 GET，且每个请求都带超时 signal', async () => {
  let captured = null;
  await withFetch((url, init) => {
    captured = { url: url, init: init };
    return jsonResponse({ ok: true, data: {} });
  }, async () => {
    await request('/api/status');
  });
  assert.strictEqual(captured.url, '/api/status');
  assert.strictEqual(captured.init.method, 'GET');
  assert.strictEqual(captured.init.body, undefined);
  assert.ok(captured.init.signal, '必须带 AbortController signal 才能超时中断');
});

test('request：method / body / params 正确传入 fetch', async () => {
  let captured = null;
  await withFetch((url, init) => {
    captured = { url: url, init: init };
    return jsonResponse({ ok: true, data: {} });
  }, async () => {
    await request('/api/prompts/expand', {
      method: 'POST',
      body: { original_prompt: '一只猫' },
      params: { page: 2 }
    });
  });
  assert.strictEqual(captured.url, '/api/prompts/expand?page=2');
  assert.strictEqual(captured.init.method, 'POST');
  assert.strictEqual(captured.init.headers['Content-Type'], 'application/json');
  assert.strictEqual(captured.init.body, JSON.stringify({ original_prompt: '一只猫' }));
});

test('request：ok:false 不抛异常，返回结构化错误', async () => {
  await withFetch(() => jsonResponse({ ok: false, error: { code: 'not_found', message: '没有该资源' } }), async () => {
    const res = await request('/api/missing');
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.data, null);
    assert.deepStrictEqual(res.error, { code: 'not_found', message: '没有该资源' });
  });
});

test('request：HTTP 错误优先使用信封里的错误', async () => {
  await withFetch(() => jsonResponse({ ok: false, error: { code: 'server_error', message: '炸了' } }, 500), async () => {
    const res = await request('/api/boom');
    assert.strictEqual(res.ok, false);
    assert.deepStrictEqual(res.error, { code: 'server_error', message: '炸了' });
  });
});

test('request：HTTP 错误没有信封时按状态码兜底', async () => {
  await withFetch(() => jsonResponse({ message: 'Internal' }, 500), async () => {
    const res = await request('/api/boom');
    assert.strictEqual(res.ok, false);
    assert.deepStrictEqual(res.error, { code: 'http_500', message: 'Internal' });
  });
});

test('request：网络失败归为 network_error，不抛异常', async () => {
  await withFetch(() => Promise.reject(new Error('boom')), async () => {
    const res = await request('/api/ping');
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error.code, 'network_error');
    assert.ok(res.error.message);
  });
});

test('request：超时归为 timeout，不抛异常', async () => {
  await withFetch((url, init) => new Promise((resolve, reject) => {
    const signal = init && init.signal;
    if (!signal) return resolve(jsonResponse({ ok: true, data: {} }));
    signal.addEventListener('abort', () => {
      const err = new Error('aborted');
      err.name = 'AbortError';
      reject(err);
    });
    return undefined;
  }), async () => {
    const res = await request('/api/slow', { timeout: 20 });
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error.code, 'timeout');
    assert.ok(res.error.message);
  });
});

test('request：非 JSON 或空响应判为 bad_response', async () => {
  await withFetch(() => textResponse('not json'), async () => {
    const res = await request('/api/html');
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error.code, 'bad_response');
  });

  await withFetch(() => textResponse(''), async () => {
    const res = await request('/api/empty');
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error.code, 'bad_response');
  });
});

test('request：服务端无 data 字段时整体作为 data', async () => {
  await withFetch(() => jsonResponse({ items: [1, 2], total: 2 }), async () => {
    const res = await request('/api/prompt-library/entries');
    assert.strictEqual(res.ok, true);
    assert.deepStrictEqual(res.data, { items: [1, 2], total: 2 });
  });
});
