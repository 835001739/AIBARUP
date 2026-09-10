/* ============================================================
   AIBAR · 通用 UI 组件
   Toast（底部中央 + 撤销）/ Modal / 确认框 / 抽屉 / 剪贴板 /
   骨架屏 / 空状态 / 局部加载 / 转义与格式化
   无障碍：Esc 关闭、焦点可见、打开时移动焦点并在关闭后归还。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  /* ---------------------------------------------------------- 工具 */

  function el(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  /** HTML 转义：所有来自接口 / 模型的文本都视为不可信数据 */
  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatBytes(size) {
    var value = Number(size);
    if (!isFinite(value) || value <= 0) return '—';
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value = value / 1024;
      index += 1;
    }
    return (index === 0 ? value : value.toFixed(value < 10 ? 1 : 0)) + ' ' + units[index];
  }

  function formatTime(value) {
    if (!value) return '—';
    var text = String(value).replace('T', ' ');
    return text.length > 19 ? text.slice(0, 19) : text;
  }

  function formatDuration(ms) {
    var value = Number(ms);
    if (!isFinite(value) || value < 0) return '—';
    if (value < 1000) return value + ' ms';
    return (value / 1000).toFixed(1) + ' s';
  }

  /** 设置按钮 / 容器的加载态，保留原有文字宽度避免布局跳变 */
  function setBusy(node, busy) {
    if (!node) return;
    if (busy) {
      node.classList.add('is-loading');
      node.setAttribute('aria-busy', 'true');
      if (node.tagName === 'BUTTON') node.disabled = true;
    } else {
      node.classList.remove('is-loading');
      node.removeAttribute('aria-busy');
      if (node.tagName === 'BUTTON' && !node.dataset.keepDisabled) node.disabled = false;
    }
  }

  /**
   * 让一个按钮在请求期间进入「忙」态：禁用 + 换文案 + loading 样式，结束时自动复原。
   *
   * 存在的意义：出图这类操作后端要跑很久，没有 busy 反馈时用户会以为没点上而连点，
   * 结果同一批页面被重复入队，白白多跑好几倍的图。
   *
   * @param {HTMLElement} node 按钮（可为 null，此时原样返回 promise）
   * @param {string} [busyText] 忙态文案，省略则保留原文案
   * @param {Promise} promise 要等待的 promise
   * @returns {Promise} 原 promise（透传结果/错误）
   */
  function withBusy(node, busyText, promise) {
    if (!node) return promise;
    var original = null;
    if (busyText && node.tagName === 'BUTTON') {
      original = node.textContent;
      node.textContent = busyText;
    }
    setBusy(node, true);
    var release = function () {
      setBusy(node, false);
      if (original !== null) node.textContent = original;
    };
    return promise.then(function (value) {
      release();
      return value;
    }, function (err) {
      release();
      throw err;
    });
  }

  /* ---------------------------------------------------------- Toast */

  var TOAST_ICON = {
    success: 'checkCircle',
    error: 'alert',
    warning: 'alert',
    info: 'info'
  };

  /**
   * 底部中央 Toast；可撤销操作通过 action 提供清晰的「撤销」按钮。
   * @param {{message:string, type?:string, action?:{label:string,onClick:Function}, duration?:number}} options
   */
  function toast(options) {
    if (!doc) return null;
    var opts = options || {};
    var region = doc.getElementById('toast-region');
    if (!region) return null;

    var type = opts.type || 'info';
    var node = el('div', 'toast toast-' + type);
    node.setAttribute('role', type === 'error' ? 'alert' : 'status');

    var icon = el('span', 'toast-icon');
    icon.innerHTML = AIBAR.icons.get(TOAST_ICON[type] || 'info', 16);
    node.appendChild(icon);

    var msg = el('span', 'toast-msg', opts.message);
    node.appendChild(msg);

    var timer = null;
    var closed = false;
    function close() {
      if (closed) return;
      closed = true;
      if (timer) clearTimeout(timer);
      node.classList.add('is-leaving');
      setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 200);
    }

    if (opts.action && opts.action.label) {
      var actionBtn = el('button', 'toast-action', opts.action.label);
      actionBtn.type = 'button';
      actionBtn.addEventListener('click', function () {
        close();
        try {
          opts.action.onClick();
        } catch (err) {
          /* 撤销失败不应再次打断用户 */
        }
      });
      node.appendChild(actionBtn);
    }

    var closeBtn = el('button', 'toast-close');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', '关闭提示');
    closeBtn.innerHTML = AIBAR.icons.get('close', 14);
    closeBtn.addEventListener('click', close);
    node.appendChild(closeBtn);

    region.appendChild(node);
    var duration = opts.duration || (opts.action ? 7000 : 3200);
    timer = setTimeout(close, duration);
    return { close: close, node: node };
  }

  function toastSuccess(message, action) {
    return toast({ message: message, type: 'success', action: action });
  }

  function toastError(message, action) {
    return toast({ message: message, type: 'error', action: action, duration: 6000 });
  }

  /** 把 ApiError / 未知异常转成可读错误提示 */
  function errorText(err, fallback) {
    if (!err) return fallback || '操作失败';
    if (typeof err === 'string') return err;
    return err.message || fallback || '操作失败';
  }

  /* ---------------------------------------------------------- 分页

     列表接口都支持 page/page_size 并返回 total，但此前只有词库接了分页，
     工作流与案例库一律写死「第一页 60 条」——案例库实际有 2700 条，
     用户能看到 2%，且界面上没有任何提示。这里抽出通用控件供所有列表复用。
     ------------------------------------------------------------------ */

  /**
   * 分页的纯计算部分（不碰 DOM，可单测）：把「当前页 / 总数 / 每页条数」收敛成
   * 一个自洽的模型。当前页会被夹到 [1, totalPages]，避免筛选后总数变小、
   * 页码却还停在第 40 页导致列表空白。
   */
  function pagerModel(page, total, pageSize) {
    var size = Math.max(1, Math.floor(Number(pageSize) || 1));
    var totalItems = Math.max(0, Math.floor(Number(total) || 0));
    var totalPages = Math.max(1, Math.ceil(totalItems / size));
    var current = Math.min(Math.max(1, Math.floor(Number(page) || 1)), totalPages);
    return {
      page: current,
      pageSize: size,
      total: totalItems,
      totalPages: totalPages,
      hasPrev: current > 1,
      hasNext: current < totalPages
    };
  }

  function pagerButton(label, target, enabled, onJump) {
    var btn = el('button', 'btn btn-ghost btn-sm', label);
    btn.type = 'button';
    btn.disabled = !enabled;
    if (enabled) btn.addEventListener('click', function () { onJump(target); });
    return btn;
  }

  /**
   * 渲染分页控件：首页 / 上一页 / 页码信息 / 下一页 / 末页。
   * 全空（total=0）时只留一条「共 0 条」，不摆一排禁用按钮。
   *
   * @param {HTMLElement} host 容器，会被清空后重绘
   * @param {{page:number, total:number, pageSize:number, unit?:string,
   *          onJump:(page:number)=>void}} options
   * @returns {object|null} 归一化后的分页模型；host 或 onJump 缺失时返回 null
   */
  function pager(host, options) {
    var opts = options || {};
    if (!host || typeof opts.onJump !== 'function') return null;

    var model = pagerModel(opts.page, opts.total, opts.pageSize);
    host.innerHTML = '';
    host.appendChild(el('span', 'pager-info',
      '第 ' + model.page + ' / ' + model.totalPages + ' 页 · 共 ' + model.total + ' ' + (opts.unit || '条')));

    if (!model.total) return model;

    host.appendChild(pagerButton('首页', 1, model.hasPrev, opts.onJump));
    host.appendChild(pagerButton('上一页', model.page - 1, model.hasPrev, opts.onJump));
    host.appendChild(pagerButton('下一页', model.page + 1, model.hasNext, opts.onJump));
    host.appendChild(pagerButton('末页', model.totalPages, model.hasNext, opts.onJump));
    return model;
  }

  /* ---------------------------------------------------------- 弹层栈与 Esc */

  var layerStack = [];

  function pushLayer(entry) {
    layerStack.push(entry);
    doc.body.classList.add('has-layer');
  }

  function popLayer(entry) {
    var index = layerStack.indexOf(entry);
    if (index >= 0) layerStack.splice(index, 1);
    if (!layerStack.length) doc.body.classList.remove('has-layer');
  }

  function handleKeydown(event) {
    if (event.key !== 'Escape' || !layerStack.length) return;
    var top = layerStack[layerStack.length - 1];
    event.preventDefault();
    top.close();
  }

  function focusables(container) {
    return Array.prototype.slice.call(
      container.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter(function (node) {
      return node.offsetParent !== null || node === doc.activeElement;
    });
  }

  function trapFocus(event, container) {
    if (event.key !== 'Tab') return;
    var items = focusables(container);
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (event.shiftKey && doc.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && doc.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /* ---------------------------------------------------------- Modal */

  /**
   * 深色模态框。
   * @param {{title:string, desc?:string, body?:Node, bodyHtml?:string, actions?:Array, size?:string}} options
   */
  function modal(options) {
    var opts = options || {};
    var previous = doc.activeElement;
    var overlay = el('div', 'overlay');
    overlay.setAttribute('role', 'presentation');

    var box = el('div', 'modal' + (opts.size === 'lg' ? ' modal-lg' : ''));
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', opts.title || '对话框');

    var head = el('div', 'modal-head');
    var textWrap = el('div');
    textWrap.appendChild(el('h2', 'modal-title', opts.title || ''));
    if (opts.desc) textWrap.appendChild(el('p', 'modal-desc', opts.desc));
    head.appendChild(textWrap);

    var closeBtn = el('button', 'icon-btn');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', '关闭对话框');
    closeBtn.innerHTML = AIBAR.icons.get('close', 18);
    head.appendChild(closeBtn);
    box.appendChild(head);

    var body = el('div', 'modal-body');
    if (opts.body) body.appendChild(opts.body);
    if (opts.bodyHtml) body.innerHTML = opts.bodyHtml;
    box.appendChild(body);

    var foot = el('div', 'modal-foot');
    var entry = { close: close, root: overlay, body: body };
    var resolved = false;

    function close(result) {
      if (resolved) return;
      resolved = true;
      popLayer(entry);
      doc.removeEventListener('keydown', onKey, true);
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
      if (previous && typeof previous.focus === 'function') previous.focus();
      if (typeof opts.onClose === 'function') opts.onClose(result);
    }

    function onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close(null);
        return;
      }
      trapFocus(event, box);
    }

    (opts.actions || []).forEach(function (action) {
      var btn = el('button', 'btn btn-' + (action.variant || 'secondary'), action.label);
      btn.type = 'button';
      btn.addEventListener('click', function () {
        var result = typeof action.onClick === 'function' ? action.onClick(entry) : null;
        // onClick 返回 false 时保持打开（例如校验未通过）
        if (result !== false && action.close !== false) close(result);
      });
      foot.appendChild(btn);
    });

    if (foot.childNodes.length) box.appendChild(foot);

    closeBtn.addEventListener('click', function () { close(null); });
    overlay.addEventListener('mousedown', function (event) {
      if (event.target === overlay) close(null);
    });

    overlay.appendChild(box);
    doc.body.appendChild(overlay);
    pushLayer(entry);
    doc.addEventListener('keydown', onKey, true);

    var target = focusables(box).filter(function (node) { return node !== closeBtn; })[0] || closeBtn;
    target.focus();
    return entry;
  }

  /** 确认框：返回 Promise<boolean> */
  function confirm(options) {
    var opts = options || {};
    return new Promise(function (resolve) {
      var result = false;
      modal({
        title: opts.title || '确认操作',
        desc: opts.desc,
        bodyHtml: '<p class="text-sm text-secondary break-any">' + escapeHtml(opts.message || '') + '</p>',
        actions: [
          {
            label: opts.cancelLabel || '取消',
            variant: 'ghost',
            onClick: function () { result = false; }
          },
          {
            label: opts.confirmLabel || '确认',
            variant: opts.danger ? 'danger' : 'primary',
            onClick: function () {
              result = true;
              if (typeof opts.onConfirm === 'function') opts.onConfirm();
            }
          }
        ],
        onClose: function () { resolve(result); }
      });
    });
  }

  /* ---------------------------------------------------------- Drawer */

  /**
   * 右侧抽屉（Provider 中心 / 工作流详情 / 反推历史等）。
   * @param {{title:string, desc?:string, body?:Node, bodyHtml?:string, foot?:Node, wide?:boolean}} options
   */
  function drawer(options) {
    var opts = options || {};
    var previous = doc.activeElement;
    var scrim = el('div', 'overlay');
    scrim.style.background = 'rgba(5,6,8,.55)';
    scrim.style.justifyContent = 'flex-end';
    scrim.style.padding = '0';

    var panel = el('div', 'drawer' + (opts.wide ? ' drawer-wide' : ''));
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', opts.title || '抽屉');

    var head = el('div', 'drawer-head');
    var textWrap = el('div');
    textWrap.appendChild(el('h2', 'modal-title', opts.title || ''));
    if (opts.desc) textWrap.appendChild(el('p', 'modal-desc', opts.desc));
    head.appendChild(textWrap);

    var closeBtn = el('button', 'icon-btn');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', '关闭抽屉');
    closeBtn.innerHTML = AIBAR.icons.get('close', 18);
    head.appendChild(closeBtn);
    panel.appendChild(head);

    var body = el('div', 'drawer-body');
    if (opts.body) body.appendChild(opts.body);
    if (opts.bodyHtml) body.innerHTML = opts.bodyHtml;
    panel.appendChild(body);

    if (opts.foot) {
      var foot = el('div', 'drawer-foot');
      foot.appendChild(opts.foot);
      panel.appendChild(foot);
    }

    var entry = { close: close, root: scrim, body: body, panel: panel };
    var closed = false;

    function close() {
      if (closed) return;
      closed = true;
      popLayer(entry);
      doc.removeEventListener('keydown', onKey, true);
      if (scrim.parentNode) scrim.parentNode.removeChild(scrim);
      if (previous && typeof previous.focus === 'function') previous.focus();
      if (typeof opts.onClose === 'function') opts.onClose();
    }

    function onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      trapFocus(event, panel);
    }

    closeBtn.addEventListener('click', close);
    scrim.addEventListener('mousedown', function (event) {
      if (event.target === scrim) close();
    });

    scrim.appendChild(panel);
    doc.body.appendChild(scrim);
    pushLayer(entry);
    doc.addEventListener('keydown', onKey, true);
    closeBtn.focus();
    return entry;
  }

  /* ---------------------------------------------------------- 剪贴板 */

  function copyText(text) {
    var value = String(text === null || text === undefined ? '' : text);
    if (!value) {
      toast({ message: '没有可复制的内容', type: 'warning' });
      return Promise.resolve(false);
    }
    var done = Promise.resolve(false);
    if (root.navigator && root.navigator.clipboard && root.navigator.clipboard.writeText) {
      done = root.navigator.clipboard.writeText(value).then(function () { return true; }, function () { return legacyCopy(value); });
    } else {
      done = Promise.resolve(legacyCopy(value));
    }
    return done.then(function (okFlag) {
      toast(okFlag
        ? { message: '已复制到剪贴板', type: 'success' }
        : { message: '复制失败，请手动选择文本', type: 'error' });
      return okFlag;
    });
  }

  function legacyCopy(value) {
    if (!doc) return false;
    var area = doc.createElement('textarea');
    area.value = value;
    area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed';
    area.style.top = '-1000px';
    doc.body.appendChild(area);
    area.select();
    var okFlag = false;
    try {
      okFlag = doc.execCommand('copy');
    } catch (err) {
      okFlag = false;
    }
    doc.body.removeChild(area);
    return okFlag;
  }

  /* ---------------------------------------------------------- 骨架 / 空状态 */

  function skeletonLines(count, widths) {
    var wrap = el('div');
    for (var i = 0; i < (count || 3); i += 1) {
      var line = el('div', 'skeleton skeleton-line');
      line.style.width = (widths && widths[i]) || (100 - (i % 3) * 12) + '%';
      wrap.appendChild(line);
    }
    return wrap;
  }

  function skeletonCards(count) {
    var wrap = el('div', 'entry-list');
    for (var i = 0; i < (count || 4); i += 1) {
      var card = el('div', 'skeleton-card');
      card.appendChild(skeletonLines(3, ['62%', '100%', '45%']));
      wrap.appendChild(card);
    }
    return wrap;
  }

  /**
   * 空状态：说明 + 可选操作。
   * @param {{icon?:string,title:string,desc?:string,actions?:Array}} options
   */
  function emptyState(options) {
    var opts = options || {};
    var node = el('div', 'empty');
    var icon = el('div', 'empty-icon');
    icon.innerHTML = AIBAR.icons.get(opts.icon || 'info', 20);
    node.appendChild(icon);
    node.appendChild(el('p', 'empty-title', opts.title || '暂无数据'));
    if (opts.desc) node.appendChild(el('p', 'empty-desc', opts.desc));
    if (opts.actions && opts.actions.length) {
      var actions = el('div', 'empty-actions');
      opts.actions.forEach(function (action) {
        var btn = el('button', 'btn btn-' + (action.variant || 'secondary'), action.label);
        btn.type = 'button';
        btn.addEventListener('click', action.onClick);
        actions.appendChild(btn);
      });
      node.appendChild(actions);
    }
    return node;
  }

  /** 可操作错误态：说明 + 重试入口 */
  function errorState(message, onRetry) {
    var node = el('div', 'callout callout-error');
    var icon = el('span', 'callout-icon');
    icon.innerHTML = AIBAR.icons.get('alert', 16);
    node.appendChild(icon);
    var wrap = el('div');
    wrap.appendChild(el('div', 'break-any', message || '加载失败'));
    if (typeof onRetry === 'function') {
      var actions = el('div', 'callout-actions');
      var btn = el('button', 'btn btn-secondary btn-sm', '重试');
      btn.type = 'button';
      btn.addEventListener('click', onRetry);
      actions.appendChild(btn);
      wrap.appendChild(actions);
    }
    node.appendChild(wrap);
    return node;
  }

  function loadingInline(text) {
    var node = el('div', 'loading-inline');
    node.setAttribute('role', 'status');
    node.appendChild(el('span', 'spinner'));
    node.appendChild(el('span', '', text || '加载中…'));
    return node;
  }

  /** 下拉选择框。``options`` 形如 ``[{label, value}]``，``value`` 为默认选中项。 */
  function select(options, value) {
    var s = el('select', 'select');
    (options || []).forEach(function (o) {
      var opt = el('option', null, o.label);
      opt.value = o.value;
      if (String(o.value) === String(value)) opt.selected = true;
      s.appendChild(opt);
    });
    return s;
  }

  /**
   * 带标签的表单行：label + 控件 + 可选提示文字。
   * 返回一个 div.field-row，label 在上、控件在下（或 label 左控件右，取决于 inline）。
   *
   * @param {string} labelText 标签文字
   * @param {HTMLElement} control 控件元素（input / select 等）
   * @param {string} [hint] 标签下方的灰色提示
   * @returns {HTMLElement}
   */
  function fieldRow(labelText, control, hint) {
    var row = el('div', 'field-row');
    var label = el('label', 'field-label', labelText);
    if (control.id) label.htmlFor = control.id;
    row.appendChild(label);
    if (hint) {
      var hintEl = el('span', 'field-hint', hint);
      row.appendChild(hintEl);
    }
    row.appendChild(control);
    return row;
  }

  /** 带标签的复选框。返回 label 元素，其 checkbox 挂在 ``.checkbox`` 上。

      **必须**叫 ``.checkbox`` 而不是 ``.control``：``HTMLLabelElement`` 自身有个
      只读的 ``.control`` 属性（HTML5 标准），自定义同名属性会被静默忽略，
      导致后续读取 ``wrap.control.checked`` 永远是 undefined。
  */
  function checkbox(labelText, checked) {
    var wrap = el('label', 'field field-inline');
    var c = el('input');
    c.type = 'checkbox';
    c.checked = !!checked;
    c.style.cssText = 'width:16px;height:16px;margin-right:8px';
    wrap.appendChild(c);
    wrap.appendChild(el('span', null, labelText));
    wrap.checkbox = c;
    return wrap;
  }

  AIBAR.ui = {
    el: el,
    escapeHtml: escapeHtml,
    formatBytes: formatBytes,
    formatTime: formatTime,
    formatDuration: formatDuration,
    setBusy: setBusy,
    withBusy: withBusy,
    toast: toast,
    toastSuccess: toastSuccess,
    toastError: toastError,
    errorText: errorText,
    pager: pager,
    pagerModel: pagerModel,
    modal: modal,
    confirm: confirm,
    drawer: drawer,
    copyText: copyText,
    skeletonLines: skeletonLines,
    skeletonCards: skeletonCards,
    emptyState: emptyState,
    errorState: errorState,
    loadingInline: loadingInline,
    handleKeydown: handleKeydown,
    select: select,
    checkbox: checkbox,
    fieldRow: fieldRow
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      escapeHtml: escapeHtml,
      formatBytes: formatBytes,
      formatTime: formatTime,
      formatDuration: formatDuration,
      pagerModel: pagerModel
    };
  }
})(typeof window !== 'undefined' ? window : globalThis);
