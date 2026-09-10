/* ============================================================
   AIBAR · 应用外壳（PRD M8.3）
   - hash 路由 + data-tab 导航，刷新后按 hash 恢复，无 hash 默认 studio
   - 顶部上下文栏标题/说明与导航选中态联动
   - 侧栏计数徽标、ComfyUI 状态、自动同步开关、立即同步
   - ≤1024 抽屉导航，Esc 关闭，键盘可达
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  var PAGES = {
    studio: {
      title: '提示词工作台',
      desc: '挑词条、写原文、一键扩写，或切换到图片反推重建提示词'
    },
    workflows: {
      title: '工作流',
      desc: 'ComfyUI 工作流归档，可检索节点与提示词并下载原始 JSON'
    },
    gallery: {
      title: '画廊',
      desc: '按时间浏览生成产物，查看提示词、参数与关联工作流'
    },
    cases: {
      title: '案例库',
      desc: '带提示词的作品按风格分组展示，可直接复用'
    },
    models: {
      title: '模型',
      desc: '扫描本地模型目录，查看类型、位置与体积'
    },
    logs: {
      title: '同步日志',
      desc: '同步、启动与反推任务的执行记录'
    },
    comic: {
      title: '漫画工作室',
      desc: '维护漫画大工作流：概览、章节、分镜预制提示词与 ComfyUI 出图队列'
    },
    actors: {
      title: '演员库',
      desc: '把生成的人物收为演员，跨漫画复用同一张脸；关联漫画角色保持长相一致'
    },
    groups: {
      title: '组图',
      desc: '用强制预设提示词让同一个演员做连贯动作，出完一组可连播成动画'
    },
    video: {
      title: '视频转帧',
      desc: '导入视频拆成序列帧，或合成 GIF；序列帧可连播预览'
    },
    videopaint: {
      title: '视频转绘',
      desc: '把视频逐帧拆成骨架，用参考图锁脸 + 骨架控姿，逐帧重绘成统一风格，并落进组图连播'
    }
  };

  var ORDER = ['studio', 'workflows', 'gallery', 'cases', 'models', 'logs', 'comic', 'actors', 'groups', 'video', 'videopaint'];
  var DEFAULT_TAB = 'studio';

  var COUNT_KEYS = {
    workflows: 'workflows',
    gallery: 'images',
    cases: 'cases',
    models: 'models'
  };

  var state = {
    tab: DEFAULT_TAB,
    comfy: { running: false },
    autoSync: false,
    timers: {}
  };

  function ui() { return AIBAR.ui; }
  function api() { return AIBAR.api; }

  /* ---------------------------------------------------------- 路由 */

  function tabFromHash() {
    var raw = String(root.location.hash || '').replace(/^#/, '').trim();
    return PAGES[raw] ? raw : '';
  }

  function navigate(tab, options) {
    var opts = options || {};
    var next = PAGES[tab] ? tab : DEFAULT_TAB;
    var changed = next !== state.tab;
    state.tab = next;

    // 导航选中态
    var navItems = doc.querySelectorAll('.nav-item[data-tab]');
    Array.prototype.forEach.call(navItems, function (item) {
      var active = item.dataset.tab === next;
      item.classList.toggle('is-active', active);
      if (active) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });

    // 面板切换
    Array.prototype.forEach.call(doc.querySelectorAll('.panel[data-panel]'), function (panel) {
      var active = panel.dataset.panel === next;
      panel.hidden = !active;
      if (active) {
        panel.removeAttribute('aria-hidden');
      } else {
        panel.setAttribute('aria-hidden', 'true');
      }
    });

    // 顶部上下文栏
    var meta = PAGES[next];
    var title = doc.getElementById('page-title');
    var desc = doc.getElementById('page-desc');
    if (title) title.textContent = meta.title;
    if (desc) desc.textContent = meta.desc;
    doc.title = meta.title + ' · AIBAR';

    if (opts.updateHash !== false && String(root.location.hash).replace(/^#/, '') !== next) {
      // 使用 replace 避免把每次切换都堆进历史栈
      var url = root.location.pathname + root.location.search + '#' + next;
      if (root.history && root.history.replaceState) root.history.replaceState(null, '', url);
      else root.location.hash = next;
    }

    if (changed) {
      if (AIBAR.pages && AIBAR.pages.onEnter) {
        try {
          AIBAR.pages.onEnter(next);
        } catch (err) {
          /* 单个页面异常不应阻断导航 */
        }
      }
      if (AIBAR.comic && AIBAR.comic.onEnter) {
        try {
          AIBAR.comic.onEnter(next);
        } catch (err) {
          /* 同上 */
        }
      }
      if (AIBAR.actors && AIBAR.actors.onEnter) {
        try {
          AIBAR.actors.onEnter(next);
        } catch (err) {
          /* 同上 */
        }
      }
      if (AIBAR.groups && AIBAR.groups.onEnter) {
        try {
          AIBAR.groups.onEnter(next);
        } catch (err) {
          /* 同上 */
        }
      }
      if (AIBAR.video && AIBAR.video.onEnter) {
        try {
          AIBAR.video.onEnter(next);
        } catch (err) {
          /* 同上 */
        }
      }
      if (AIBAR.videopaint && AIBAR.videopaint.onEnter) {
        try {
          AIBAR.videopaint.onEnter(next);
        } catch (err) {
          /* 同上 */
        }
      }
      if (next === 'studio' && AIBAR.reverse && AIBAR.reverse.init && !AIBAR.reverse._ready) {
        AIBAR.reverse._ready = true;
        AIBAR.reverse.init();
      }
    }

    closeNavDrawer();
    return next;
  }

  /* ---------------------------------------------------------- 侧栏抽屉（≤1024 / ≤760） */

  function openNavDrawer() {
    doc.body.classList.add('nav-open');
    var toggle = doc.getElementById('btn-nav-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
    var first = doc.querySelector('.nav-item[data-tab]');
    if (first) first.focus();
  }

  function closeNavDrawer() {
    if (!doc.body.classList.contains('nav-open')) return;
    doc.body.classList.remove('nav-open');
    var toggle = doc.getElementById('btn-nav-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
  }

  function toggleNavDrawer() {
    if (doc.body.classList.contains('nav-open')) closeNavDrawer();
    else openNavDrawer();
  }

  /* ---------------------------------------------------------- 状态刷新 */

  function refreshNavCounts() {
    api().get('/api/nav/counts', {}).then(function (data) {
      var counts = data || {};
      Array.prototype.forEach.call(doc.querySelectorAll('.nav-item[data-tab]'), function (item) {
        var key = COUNT_KEYS[item.dataset.tab];
        var badge = item.querySelector('.badge-count');
        if (!badge || !key) return;
        var value = counts[key];
        badge.textContent = value === undefined || value === null ? '0' : String(value);
      });
    }, function () {
      /* 计数失败不提示：不干扰主流程 */
    });
  }

  function refreshStatus() {
    api().get('/api/status', {}).then(function (data) {
      if (!data) return;
      state.autoSync = !!data.auto_sync;
      var toggle = doc.getElementById('auto-sync');
      if (toggle) toggle.checked = state.autoSync;
      renderLastSync(data.last_sync);
      setComfyState(!!(data.comfyui && data.comfyui.running));
    }, function () {
      /* 概览失败保持上次状态 */
    });
  }

  function refreshComfyStatus(options) {
    var force = options && options.force;
    api().get('/api/comfyui/status', force ? { force: 1 } : {}).then(function (data) {
      state.comfy = data || {};
      setComfyState(!!state.comfy.running);
    }, function () {
      setComfyState(false);
    });
  }

  function setComfyState(running) {
    var dot = doc.getElementById('comfy-dot');
    var text = doc.getElementById('comfy-text');
    var start = doc.getElementById('btn-comfy-start');
    if (dot) {
      dot.className = 'status-dot ' + (running ? 'is-on' : 'is-off');
    }
    if (text) text.textContent = running ? 'ComfyUI 运行中' : 'ComfyUI 已停止';
    var host = doc.getElementById('comfy-host');
    if (host) {
      host.textContent = (state.comfy && state.comfy.host)
        ? state.comfy.host + ':' + (state.comfy.port || 8188)
        : '';
    }
    if (start) {
      // 「启动 ComfyUI」只在停止 / 异常时成为明显操作
      start.classList.toggle('is-visible', !running);
      start.hidden = running;
    }
  }

  function renderLastSync(value) {
    var node = doc.getElementById('last-sync');
    if (!node) return;
    node.textContent = value ? '最近同步 ' + ui().formatTime(value) : '尚未同步';
    node.title = node.textContent;
  }

  /* ---------------------------------------------------------- 同步 */

  function syncNow() {
    var button = doc.getElementById('btn-sync-now');
    ui().setBusy(button, true);
    api().post('/api/sync/now', {}).then(function (data) {
      ui().setBusy(button, false);
      var stats = data || {};
      ui().toastSuccess('同步完成：工作流 ' + (stats.workflows || 0) + ' · 图片 ' + (stats.images || 0) +
        ' · 新增 ' + ((stats.added_workflows || 0) + (stats.added_images || 0)));
      renderLastSync(stats.last_sync);
      refreshNavCounts();
      if (AIBAR.pages) AIBAR.pages.refreshAll();
    }, function (err) {
      ui().setBusy(button, false);
      ui().toastError(ui().errorText(err, '同步失败'));
      if (AIBAR.pages) AIBAR.pages.refreshAll();
    });
  }

  function setAutoSync(enabled) {
    api().post('/api/sync/auto', { enabled: !!enabled }).then(function (data) {
      state.autoSync = !!(data && data.enabled);
      var toggle = doc.getElementById('auto-sync');
      if (toggle) toggle.checked = state.autoSync;
      ui().toast({ message: state.autoSync ? '已开启自动同步' : '已关闭自动同步', type: 'info' });
    }, function (err) {
      var toggle = doc.getElementById('auto-sync');
      if (toggle) toggle.checked = state.autoSync;
      ui().toastError(ui().errorText(err, '自动同步切换失败'));
    });
  }

  function startComfyUI() {
    var button = doc.getElementById('btn-comfy-start');
    ui().setBusy(button, true);
    api().post('/api/comfyui/start', {}).then(function (data) {
      ui().setBusy(button, false);
      if (data && data.started) {
        ui().toastSuccess(data.message || 'ComfyUI 正在启动');
      } else {
        ui().toast({
          message: (data && data.message) || 'ComfyUI 未能启动，请检查目录配置',
          type: 'warning'
        });
      }
      refreshComfyStatus({ force: true });
    }, function (err) {
      ui().setBusy(button, false);
      ui().toastError(ui().errorText(err, '启动 ComfyUI 失败'));
    });
  }

  /* ---------------------------------------------------------- 绑定与启动 */

  function bind() {
    Array.prototype.forEach.call(doc.querySelectorAll('.nav-item[data-tab]'), function (item) {
      item.addEventListener('click', function () {
        navigate(item.dataset.tab);
      });
    });

    var toggle = doc.getElementById('btn-nav-toggle');
    if (toggle) toggle.addEventListener('click', toggleNavDrawer);

    var scrim = doc.getElementById('sidebar-scrim');
    if (scrim) scrim.addEventListener('click', closeNavDrawer);

    var syncBtn = doc.getElementById('btn-sync-now');
    if (syncBtn) syncBtn.addEventListener('click', syncNow);

    var autoSync = doc.getElementById('auto-sync');
    if (autoSync) {
      autoSync.addEventListener('change', function () { setAutoSync(autoSync.checked); });
    }

    var start = doc.getElementById('btn-comfy-start');
    if (start) start.addEventListener('click', startComfyUI);

    doc.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') closeNavDrawer();
    });

    root.addEventListener('hashchange', function () {
      var tab = tabFromHash();
      if (tab && tab !== state.tab) navigate(tab, { updateHash: false });
    });
  }

  function startPolling() {
    state.timers.comfy = setInterval(function () {
      if (doc.hidden) return;
      refreshComfyStatus();
    }, 20000);
    state.timers.counts = setInterval(function () {
      if (doc.hidden) return;
      refreshNavCounts();
      refreshStatus();
    }, 60000);
  }

  function boot() {
    // 全局兜底：此前任何「没人接的 Promise 拒绝」都是纯静默失败——
    // 比如删除章节时服务端已经删掉了，但刷新列表的那步抛了异常，
    // 界面就停在旧数据上，用户看到的就是「删除无效」。现在把它变成可见的提示。
    root.addEventListener('unhandledrejection', function (event) {
      var reason = event && event.reason;
      var message = (reason && reason.message) ? reason.message : String(reason || '未知错误');
      try {
        if (AIBAR.ui && AIBAR.ui.toastError) AIBAR.ui.toastError('操作未完成：' + message);
      } catch (err) { /* 兜底逻辑本身不能再抛 */ }
      if (root.console && typeof root.console.error === 'function') {
        root.console.error('[unhandledrejection]', reason);
      }
    });

    // 图标渲染：侧栏与顶部按钮
    Array.prototype.forEach.call(doc.querySelectorAll('[data-icon]'), function (node) {
      var name = node.dataset.icon;
      var size = parseInt(node.dataset.iconSize || '18', 10);
      node.innerHTML = AIBAR.icons.get(name, size);
    });

    bind();

    try {
      AIBAR.studio.init();
    } catch (err) {
      /* 工作台初始化失败不阻断其他页面 */
    }
    try {
      AIBAR.pages.init();
    } catch (err) {
      /* 同上 */
    }
    try {
      AIBAR.comic.init();
    } catch (err) {
      /* 同上 */
    }
    try {
      AIBAR.actors.init();
    } catch (err) {
      /* 同上 */
    }
    try {
      AIBAR.groups.init();
    } catch (err) {
      /* 同上 */
    }
    try {
      AIBAR.video.init();
    } catch (err) {
      /* 同上 */
    }
    try {
      AIBAR.videopaint.init();
    } catch (err) {
      /* 同上 */
    }

    var initial = tabFromHash() || DEFAULT_TAB;
    navigate(initial);

    if (initial === 'studio' && AIBAR.reverse && AIBAR.reverse.init && !AIBAR.reverse._ready) {
      AIBAR.reverse._ready = true;
      AIBAR.reverse.init();
    }

    refreshNavCounts();
    refreshStatus();
    refreshComfyStatus();
    startPolling();
  }

  if (doc) {
    if (doc.readyState === 'loading') {
      doc.addEventListener('DOMContentLoaded', boot);
    } else {
      boot();
    }
  }

  AIBAR.app = {
    navigate: navigate,
    refreshNavCounts: refreshNavCounts,
    refreshStatus: refreshStatus,
    refreshComfyStatus: refreshComfyStatus,
    syncNow: syncNow,
    closeNavDrawer: closeNavDrawer,
    openNavDrawer: openNavDrawer,
    getTab: function () { return state.tab; }
  };
})(typeof window !== 'undefined' ? window : globalThis);
