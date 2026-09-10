/* api 的可中断请求：外部 signal 必须能真正掐断在途请求，且与超时区分开。
   运行：node --test tests/js/

   为什么要单独一个文件：旧 api-request.test.js 里的响应桩返回的是裸对象而不是
   Promise，fetch(...).then 直接炸，属于桩本身写错（见 OPTIMIZATION_REVIEW「过时测试」）。
   这里用正确的桩，避免把新断言塞进一个本来就失败的用例里。 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

require('../../static/js/api.js');

const api = globalThis.AIBAR.api;

/* ---------------------------------------------------------- 桩 */

/** 用桩 fetch 跑一段断言，结束或失败都还原全局 fetch */
function withFetch(impl, fn) {
  const original = globalThis.fetch;
  globalThis.fetch = impl;
  return Promise.resolve()
    .then(fn)
    .then(
      (value) => {
        restoreFetch(original);
        return value;
      },
      (err) => {
        restoreFetch(original);
        throw err;
      }
    );
}

function restoreFetch(original) {
  if (typeof original === 'undefined') delete globalThis.fetch;
  else globalThis.fetch = original;
}

/** 返回 Promise 的响应桩（旧测试的裸对象桩会在 .then 处崩） */
function jsonResponse(body, status) {
  const code = status === undefined ? 200 : status;
  return Promise.resolve({
    ok: code >= 200 && code < 300,
    status: code,
    text: () => Promise.resolve(JSON.stringify(body))
  });
}

/** 一直挂着，直到 signal 被 abort 才以 AbortError 拒绝 */
function hangingResponse(init) {
  return new Promise((resolve, reject) => {
    const signal = init && init.signal;
    if (!signal) return resolve(jsonResponse({ ok: true, data: {} }));
    signal.addEventListener('abort', () => {
      const err = new Error('The operation was aborted');
      err.name = 'AbortError';
      reject(err);
    });
    return undefined;
  });
}

function abortError() {
  const err = new Error('aborted');
  err.name = 'AbortError';
  return err;
}

/* ---------------------------------------------------------- 主动取消 */

test('外部 signal 触发 abort 时，fetch 收到的 signal 立即被中断', async () => {
  let innerAborted = false;
  const controller = api.controller();

  await withFetch((url, init) => {
    init.signal.addEventListener('abort', () => {
      innerAborted = true;
    });
    return hangingResponse(init);
  }, async () => {
    const pending = api.request('/api/prompt-reverse/jobs', {
      method: 'POST',
      body: { async: true },
      timeout: 60000,
      signal: controller.signal
    });
    controller.abort();
    const res = await pending;
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error.code, 'aborted', '用户取消不能被报成超时');
  });

  assert.strictEqual(innerAborted, true, '内部 controller 必须跟着外部 signal 一起 abort');
});

test('aborted 与 timeout 是两种不同的错误码', async () => {
  const controller = api.controller();
  await withFetch((url, init) => hangingResponse(init), async () => {
    const cancelled = api.request('/api/slow', { timeout: 60000, signal: controller.signal });
    controller.abort();
    assert.strictEqual((await cancelled).error.code, 'aborted');
  });

  await withFetch((url, init) => hangingResponse(init), async () => {
    const timedOut = api.request('/api/slow', { timeout: 20 });
    assert.strictEqual((await timedOut).error.code, 'timeout');
  });
});

test('已经 abort 过的 signal 直接短路，不再发请求', async () => {
  const controller = api.controller();
  controller.abort();
  let called = false;

  await withFetch(() => {
    called = true;
    return jsonResponse({ ok: true, data: {} });
  }, async () => {
    const res = await api.request('/api/anything', { timeout: 5000, signal: controller.signal });
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.error.code, 'aborted');
  });

  assert.strictEqual(called, false, '已经取消的请求不该打到后端');
});

test('请求正常结束后不再监听外部 signal（不留悬挂监听器）', async () => {
  const controller = api.controller();
  await withFetch(() => jsonResponse({ ok: true, data: { id: 7 } }), async () => {
    const res = await api.request('/api/ping', { timeout: 5000, signal: controller.signal });
    assert.strictEqual(res.ok, true);
  });
  // 再 abort 不应有任何影响：监听器已经摘掉
  controller.abort();
  assert.strictEqual(controller.signal.aborted, true);
});

test('各个 HTTP 动词都能带 signal 下发', async () => {
  const verbs = [
    ['get', () => api.get('/api/x', {}, { signal: api.controller().signal })],
    ['post', () => api.post('/api/x', {}, { signal: api.controller().signal })],
    ['put', () => api.put('/api/x', {}, { signal: api.controller().signal })],
    ['patch', () => api.patch('/api/x', {}, { signal: api.controller().signal })],
    ['del', () => api.del('/api/x', { signal: api.controller().signal })]
  ];

  for (const [name, call] of verbs) {
    let hasSignal = false;
    await withFetch((url, init) => {
      hasSignal = !!(init && init.signal);
      return jsonResponse({ ok: true, data: {} });
    }, call);
    assert.strictEqual(hasSignal, true, name + ' 必须转发 signal');
  }
});

test('controller 在没有 AbortController 的环境里返回 null 而不是抛错', () => {
  const Original = globalThis.AbortController;
  delete globalThis.AbortController;
  try {
    assert.strictEqual(api.controller(), null);
  } finally {
    globalThis.AbortController = Original;
  }
});

test('没有 AbortController 时请求照样能完成（老浏览器降级）', async () => {
  const Original = globalThis.AbortController;
  delete globalThis.AbortController;
  try {
    await withFetch(() => jsonResponse({ ok: true, data: { id: 9 } }), async () => {
      const res = await api.request('/api/ping');
      assert.strictEqual(res.ok, true);
      assert.deepStrictEqual(res.data, { id: 9 });
    });
  } finally {
    globalThis.AbortController = Original;
  }
});

test('外部 signal 中断时，错误仍走统一信封，不抛异常', async () => {
  const controller = api.controller();
  await withFetch((url, init) => hangingResponse(init), async () => {
    const pending = api.request('/api/slow', { timeout: 60000, signal: controller.signal });
    setTimeout(() => controller.abort(), 5);
    const res = await pending;
    assert.strictEqual(res.ok, false);
    assert.strictEqual(res.data, null);
    assert.strictEqual(typeof res.error.message, 'string');
    assert.ok(res.error.message.length > 0);
  });
});

test('abort 后 fetch 仍以 AbortError 拒绝（模拟真实浏览器行为）', async () => {
  const controller = api.controller();
  await withFetch(() => Promise.reject(abortError()), async () => {
    const res = await api.request('/api/slow', { timeout: 5000, signal: controller.signal });
    // 外部 signal 没被触发过，所以这只能算超时
    assert.strictEqual(res.error.code, 'timeout');
  });
});
