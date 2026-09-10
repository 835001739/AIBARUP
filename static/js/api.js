/* ============================================================
   AIBAR · API 封装
   统一处理 {ok:true,data} / {ok:false,error:{code,message}}：
   - 请求带超时（AbortController），超时不抛出未捕获异常；
   - 非 2xx / ok:false / 网络错误一律转成 ApiError，由调用方决定提示方式；
   - 所有 URL 与请求体字段严格对齐 docs/API_CONTRACT.md。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};

  var DEFAULT_TIMEOUT = 20000;

  /* 长任务超时：AI 拆集、批量入队、整本重刷提示词这类操作后端要跑几十秒到几分钟，
     沿用 20s 默认超时会让前端先 abort、后端还在跑，用户一重试就重复入队/重复出图。 */
  var LONG_TIMEOUT = 180000;      // 3 分钟
  var EXTRA_LONG_TIMEOUT = 600000; // 10 分钟（整本批量入队）

  /* ---------------------------------------------------------- envelope 解析 */

  function normalizeError(error) {
    if (typeof error === 'string' && error) {
      return { code: 'error', message: error };
    }
    if (error && typeof error === 'object') {
      return {
        code: String(error.code || 'error'),
        message: String(error.message || '请求失败')
      };
    }
    return { code: 'unknown_error', message: '请求失败' };
  }

  /**
   * 解析后端统一 envelope。
   * @param {*} payload 已 JSON 解析的响应体
   * @returns {{ok:boolean, data:*, error:{code:string,message:string}|null}}
   */
  function parseEnvelope(payload) {
    if (!payload || typeof payload !== 'object') {
      return { ok: false, data: null, error: { code: 'bad_response', message: '响应格式错误' } };
    }
    if (payload.ok === false) {
      return { ok: false, data: null, error: normalizeError(payload.error) };
    }
    var data = Object.prototype.hasOwnProperty.call(payload, 'data') ? payload.data : payload;
    return { ok: true, data: data, error: null };
  }

  /* ---------------------------------------------------------- 错误类型 */

  function ApiError(code, message, status) {
    this.name = 'ApiError';
    this.code = code || 'unknown_error';
    this.message = message || '请求失败';
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  /* ---------------------------------------------------------- 工具 */

  function buildQuery(params) {
    if (!params) return '';
    var parts = [];
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      if (value === null || value === undefined || value === '') return;
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  /**
   * 底层发送：按 HTTP 语义抛出 ApiError，供 get/post/... 沿用 Promise 拒绝式调用。
   * 新代码建议直接使用 request()，由调用方自行决定提示方式。
   */
  function send(method, path, options) {
    var opts = options || {};
    var url = path + buildQuery(opts.params);
    var timeout = opts.timeout || DEFAULT_TIMEOUT;
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = null;

    // 调用方传入的信号用于「主动取消」（用户点了取消按钮）。
    // 超时也是 abort，两者在 fetch 层无法区分，所以这里自己记账：
    // 记错的话，用户点取消会被报成「请求超时」。
    var external = opts.signal || null;
    var abortedByCaller = false;
    var onExternalAbort = null;
    if (external && external.aborted) {
      // 已经取消过了就别再发请求：省一次往返，也避免「取消后又成功」这种矛盾结果
      return Promise.reject(new ApiError('aborted', '请求已取消', 0));
    }
    if (controller && external) {
      onExternalAbort = function () {
        abortedByCaller = true;
        controller.abort();
      };
      external.addEventListener('abort', onExternalAbort, { once: true });
    }

    var init = {
      method: method,
      headers: {},
      credentials: 'same-origin'
    };
    if (controller) init.signal = controller.signal;

    if (opts.body !== undefined && opts.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    } else if (opts.form) {
      init.body = opts.form; // FormData：不设置 Content-Type，交给浏览器补 boundary
    }

    var promise = fetch(url, init).then(function (resp) {
      return resp.text().then(function (raw) {
        var payload = null;
        if (raw) {
          try {
            payload = JSON.parse(raw);
          } catch (err) {
            payload = null;
          }
        }
        var envelope = parseEnvelope(payload);
        if (!resp.ok) {
          // HTTP 错误优先使用 envelope 中的错误，其次按状态码给出可读提示
          var fallback = { code: 'http_' + resp.status, message: '请求失败（HTTP ' + resp.status + '）' };
          var error = envelope.error || (payload === null ? fallback : fallback);
          if (!envelope.error && payload && payload.ok !== false && typeof payload.message === 'string') {
            error = { code: 'http_' + resp.status, message: payload.message };
          }
          throw new ApiError(error.code, error.message, resp.status);
        }
        if (!envelope.ok) {
          throw new ApiError(envelope.error.code, envelope.error.message, resp.status);
        }
        return envelope.data;
      });
    }, function (err) {
      if (err && err.name === 'AbortError') {
        if (abortedByCaller) {
          throw new ApiError('aborted', '请求已取消', 0);
        }
        throw new ApiError('timeout', '请求超时，请稍后重试', 0);
      }
      throw new ApiError('network_error', '网络连接失败，请检查服务是否运行', 0);
    });

    if (controller) {
      timer = setTimeout(function () { controller.abort(); }, timeout);
      var release = function () {
        clearTimeout(timer);
        if (onExternalAbort && external) external.removeEventListener('abort', onExternalAbort);
      };
      promise = promise.then(function (value) {
        release();
        return value;
      }, function (err) {
        release();
        throw err;
      });
    }
    return promise;
  }

  /**
   * 统一请求入口：fetch + AbortController 超时，统一解包 {ok, data, error}。
   * 失败不抛异常，返回 { ok:false, error:{code,message} }，由调用方决定如何提示。
   *
   * @param {string} path 接口路径
   * @param {{method?:string, body?:*, form?:FormData, params?:Object, timeout?:number}} [options]
   * @returns {Promise<{ok:boolean, data:*, error:{code:string,message:string}|null}>}
   */
  function request(path, options) {
    var opts = options || {};
    var method = String(opts.method || 'GET').toUpperCase();
    return send(method, path, {
      params: opts.params,
      body: opts.body,
      form: opts.form,
      timeout: opts.timeout,
      signal: opts.signal
    }).then(function (data) {
      return { ok: true, data: data, error: null };
    }, function (err) {
      return { ok: false, data: null, error: normalizeError(err) };
    });
  }

  function controller() {
    return typeof AbortController === 'function' ? new AbortController() : null;
  }

  var api = {
    parseEnvelope: parseEnvelope,
    normalizeError: normalizeError,
    buildQuery: buildQuery,
    ApiError: ApiError,
    controller: controller,
    request: request,
    DEFAULT_TIMEOUT: DEFAULT_TIMEOUT,
    LONG_TIMEOUT: LONG_TIMEOUT,
    EXTRA_LONG_TIMEOUT: EXTRA_LONG_TIMEOUT,
    get: function (path, params, options) {
      return send('GET', path, {
        params: params,
        timeout: options && options.timeout,
        signal: options && options.signal
      });
    },
    post: function (path, body, options) {
      var opts = { body: body === undefined ? {} : body };
      if (options && options.timeout) opts.timeout = options.timeout;
      if (options && options.signal) opts.signal = options.signal;
      return send('POST', path, opts);
    },
    put: function (path, body, options) {
      var opts = { body: body === undefined ? {} : body };
      if (options && options.timeout) opts.timeout = options.timeout;
      if (options && options.signal) opts.signal = options.signal;
      return send('PUT', path, opts);
    },
    patch: function (path, body, options) {
      var opts = { body: body === undefined ? {} : body };
      if (options && options.timeout) opts.timeout = options.timeout;
      if (options && options.signal) opts.signal = options.signal;
      return send('PATCH', path, opts);
    },
    del: function (path, options) {
      return send('DELETE', path, {
        timeout: options && options.timeout,
        signal: options && options.signal
      });
    },
    upload: function (path, formData, options) {
      return send('POST', path, {
        form: formData,
        timeout: (options && options.timeout) || 60000,
        signal: options && options.signal
      });
    }
  };

  AIBAR.api = api;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      parseEnvelope: parseEnvelope,
      normalizeError: normalizeError,
      buildQuery: buildQuery,
      ApiError: ApiError
    };
  }

  if (typeof window !== 'undefined') {
    window.AibarUtil = window.AibarUtil || {};
    window.AibarUtil.parseEnvelope = parseEnvelope;
    window.AibarUtil.normalizeError = normalizeError;
    window.AibarUtil.buildQuery = buildQuery;
  }
})(typeof window !== 'undefined' ? window : globalThis);
