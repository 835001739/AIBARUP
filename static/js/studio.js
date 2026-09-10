/* ============================================================
   AIBAR · M8.5 提示词工作台（创作画布）
   首屏顺序：模式与引导 → 大型原始提示词画布 → 紧凑基础选项 →
   可折叠高级选项 → 粘性操作栏。
   操作栏只保留一个主按钮（一键扩写），上下文操作在产生有效结果前禁用。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  var SS = {
    advanced: 'aibar:studio:advanced-open',
    libCollapsed: 'aibar:studio:lib-collapsed',
    resultCollapsed: 'aibar:studio:result-collapsed',
    historyOpen: 'aibar:studio:history-open',
    mode: 'aibar:studio:mode'
  };

  var INTENSITIES = [
    { key: 'conservative', label: '保守' },
    { key: 'balanced', label: '均衡' },
    { key: 'creative', label: '创意' }
  ];

  var MEDIA_LABELS = { image: '图片', video: '视频', music: '歌曲' };

  var state = {
    mode: 'expand',            // expand | reverse
    media: 'image',            // 词库筛选媒体类型
    mediaBeforeReverse: 'image',
    profiles: [],
    templates: [],
    workflows: [],
    lastCaret: null,
    baseline: '',              // 最近一次扩写提交的原文（恢复原文用）
    result: null,
    expanding: false,
    history: [],
    historyLoaded: false
  };

  var refs = {};

  function ui() { return AIBAR.ui; }
  function api() { return AIBAR.api; }

  function ssGet(key, fallback) {
    try {
      var raw = root.sessionStorage.getItem(key);
      return raw === null ? fallback : raw;
    } catch (err) {
      return fallback;
    }
  }

  function ssSet(key, value) {
    try {
      root.sessionStorage.setItem(key, String(value));
    } catch (err) {
      /* 忽略存储不可用 */
    }
  }

  /* ---------------------------------------------------------- 初始化 */

  function init() {
    refs.modeSeg = doc.getElementById('seg-mode');
    refs.mediaSeg = doc.getElementById('seg-media');
    refs.body = doc.getElementById('studio-body');
    refs.lib = doc.getElementById('studio-lib');
    refs.result = doc.getElementById('studio-result');
    refs.prompt = doc.getElementById('prompt-input');
    refs.count = doc.getElementById('prompt-count');
    refs.caret = doc.getElementById('prompt-caret');
    refs.canvasWrap = doc.getElementById('prompt-canvas');
    refs.profile = doc.getElementById('opt-profile');
    refs.intensity = doc.getElementById('opt-intensity');
    refs.template = doc.getElementById('opt-template');
    refs.advToggle = doc.getElementById('adv-toggle');
    refs.advPanel = doc.getElementById('adv-panel');
    refs.workflow = doc.getElementById('opt-workflow');
    refs.btnExpand = doc.getElementById('btn-expand');
    refs.btnRestore = doc.getElementById('btn-restore');
    refs.context = doc.getElementById('context-actions');
    refs.btnApply = doc.getElementById('btn-apply-workflow');
    refs.btnLaunch = doc.getElementById('btn-launch');
    refs.btnSaveHistory = doc.getElementById('btn-save-history');
    refs.resultBody = doc.getElementById('result-body');
    refs.historyToggle = doc.getElementById('history-toggle');
    refs.historyPanel = doc.getElementById('history-panel');
    refs.historyList = doc.getElementById('history-list');
    refs.viewExpand = doc.getElementById('view-expand');
    refs.viewReverse = doc.getElementById('view-reverse');

    bindModeSeg();
    bindMediaSeg();
    bindPrompt();
    bindColumns();
    bindAdvanced();
    bindActions();
    bindHistory();

    restoreLayoutState();
    fillIntensity();
    loadLibraryMeta();
    loadWorkflows();
    updateCharCount();
  }

  /* ---------------------------------------------------------- 顶部模式 / 媒体 */

  function bindModeSeg() {
    if (!refs.modeSeg) return;
    var buttons = refs.modeSeg.querySelectorAll('[data-mode]');
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.addEventListener('click', function () {
        setMode(btn.dataset.mode);
      });
    });
  }

  function setMode(mode, options) {
    var next = mode === 'reverse' ? 'reverse' : 'expand';
    var opts = options || {};
    if (next === state.mode && !opts.force) return;
    state.mode = next;
    ssSet(SS.mode, next);

    Array.prototype.forEach.call(refs.modeSeg.querySelectorAll('[data-mode]'), function (btn) {
      var active = btn.dataset.mode === next;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-checked', active ? 'true' : 'false');
    });

    if (next === 'reverse') {
      // 反推锁定图片媒体，退出后恢复此前的媒体选择（PRD M9.3）
      state.mediaBeforeReverse = state.media;
      setMedia('image', { silent: true });
      if (refs.viewExpand) refs.viewExpand.hidden = true;
      if (refs.viewReverse) refs.viewReverse.hidden = false;
      if (refs.body) refs.body.classList.add('is-reverse');
      lockMediaForReverse(true);
      if (AIBAR.reverse && AIBAR.reverse.onEnter) AIBAR.reverse.onEnter();
    } else {
      if (refs.viewExpand) refs.viewExpand.hidden = false;
      if (refs.viewReverse) refs.viewReverse.hidden = true;
      if (refs.body) refs.body.classList.remove('is-reverse');
      lockMediaForReverse(false);
      if (state.mediaBeforeReverse && state.mediaBeforeReverse !== state.media) {
        setMedia(state.mediaBeforeReverse, { silent: true });
      }
      if (AIBAR.reverse && AIBAR.reverse.onLeave) AIBAR.reverse.onLeave();
    }
    if (!opts.silent) {
      ui().toast({
        message: next === 'reverse' ? '已进入图片反推模式，媒体类型锁定为图片' : '已回到正向扩写模式',
        type: 'info'
      });
    }
  }

  function lockMediaForReverse(locked) {
    if (!refs.mediaSeg) return;
    Array.prototype.forEach.call(refs.mediaSeg.querySelectorAll('[data-media]'), function (btn) {
      if (btn.dataset.media === 'image') return;
      btn.disabled = locked;
      btn.setAttribute('aria-disabled', locked ? 'true' : 'false');
      btn.title = locked ? '图片反推模式下锁定为图片' : '';
    });
  }

  function bindMediaSeg() {
    if (!refs.mediaSeg) return;
    Array.prototype.forEach.call(refs.mediaSeg.querySelectorAll('[data-media]'), function (btn) {
      btn.addEventListener('click', function () {
        if (btn.disabled) return;
        setMedia(btn.dataset.media);
      });
    });
  }

  /**
   * 切换媒体类型：只驱动词库筛选，不清空原始提示词，并给出明确反馈。
   */
  function setMedia(media, options) {
    var opts = options || {};
    var next = media || 'image';
    var changed = next !== state.media;
    state.media = next;
    Array.prototype.forEach.call(refs.mediaSeg.querySelectorAll('[data-media]'), function (btn) {
      var active = btn.dataset.media === next;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-checked', active ? 'true' : 'false');
    });
    if (changed) {
      if (AIBAR.library) AIBAR.library.setMediaType(next);
      if (!opts.silent) {
        ui().toast({
          message: '已切换到「' + (MEDIA_LABELS[next] || next) + '」词库，原始提示词保持不变',
          type: 'info'
        });
      }
    }
  }

  /* ---------------------------------------------------------- 原始提示词画布 */

  function bindPrompt() {
    if (!refs.prompt) return;
    var track = function () {
      state.lastCaret = refs.prompt.selectionStart;
      updateCaretHint();
    };
    ['keyup', 'click', 'select', 'input', 'focus', 'blur'].forEach(function (name) {
      refs.prompt.addEventListener(name, track);
    });
    refs.prompt.addEventListener('input', function () {
      updateCharCount();
      updateCaretHint();
    });
    refs.prompt.addEventListener('focus', function () {
      if (refs.canvasWrap) refs.canvasWrap.classList.add('is-focused');
    });
    refs.prompt.addEventListener('blur', function () {
      if (refs.canvasWrap) refs.canvasWrap.classList.remove('is-focused');
    });
    refs.prompt.addEventListener('keydown', function (event) {
      // 不拦截文本域常规快捷键：Ctrl/Cmd+Enter 作为扩写快捷方式
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault();
        expand();
      }
    });
  }

  function updateCharCount() {
    if (!refs.prompt || !refs.count) return;
    var length = refs.prompt.value.length;
    refs.count.textContent = length + ' / 2000 字符';
    refs.count.classList.toggle('is-warn', length > 1800);
  }

  function updateCaretHint() {
    if (!refs.caret) return;
    if (doc.activeElement === refs.prompt && typeof refs.prompt.selectionStart === 'number') {
      refs.caret.textContent = '插入位置：第 ' + refs.prompt.selectionStart + ' 字符';
    } else {
      refs.caret.textContent = '未聚焦时插入到末尾';
    }
  }

  /* ---------------------------------------------------------- 列折叠 / 布局状态 */

  function bindColumns() {
    bindCollapse('lib', 'studio-lib', 'btn-lib-collapse', 'btn-lib-expand', 'btn-lib-close', SS.libCollapsed);
    bindCollapse('result', 'studio-result', 'btn-result-collapse', 'btn-result-expand', 'btn-result-close', SS.resultCollapsed);

    var openLib = doc.getElementById('btn-open-library');
    if (openLib) openLib.addEventListener('click', function () { openDrawer(refs.lib, true); });
    var openResult = doc.getElementById('btn-open-result');
    if (openResult) openResult.addEventListener('click', function () { openDrawer(refs.result, true); });

    // 抽屉模式下 Esc 关闭
    doc.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (refs.lib && refs.lib.classList.contains('is-open')) openDrawer(refs.lib, false);
      if (refs.result && refs.result.classList.contains('is-open')) openDrawer(refs.result, false);
    });
  }

  function bindCollapse(name, colId, collapseBtnId, expandBtnId, closeBtnId, storageKey) {
    var col = doc.getElementById(colId);
    var collapseBtn = doc.getElementById(collapseBtnId);
    var expandBtn = doc.getElementById(expandBtnId);
    var closeBtn = doc.getElementById(closeBtnId);
    if (!col) return;

    function apply(collapsed) {
      col.classList.toggle('is-collapsed', collapsed);
      if (refs.body) refs.body.classList.toggle('is-' + name + '-collapsed', collapsed);
      if (collapseBtn) collapseBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      ssSet(storageKey, collapsed ? '1' : '0');
    }

    if (collapseBtn) collapseBtn.addEventListener('click', function () { apply(true); });
    if (expandBtn) expandBtn.addEventListener('click', function () { apply(false); });
    if (closeBtn) closeBtn.addEventListener('click', function () { openDrawer(col, false); });

    // 折叠状态在当前会话保留（筛选状态本身不丢失）
    apply(ssGet(storageKey, '0') === '1');
  }

  function openDrawer(col, open) {
    if (!col) return;
    if (open) {
      col.classList.remove('is-collapsed');
      col.classList.add('is-open');
      var focusable = col.querySelector('button, input, select');
      if (focusable) focusable.focus();
    } else {
      col.classList.remove('is-open');
    }
  }

  function restoreLayoutState() {
    var mode = ssGet(SS.mode, 'expand');
    setMode(mode, { silent: true, force: true });
  }

  /* ---------------------------------------------------------- 高级设置 */

  function bindAdvanced() {
    if (!refs.advToggle || !refs.advPanel) return;
    var open = ssGet(SS.advanced, '0') === '1';
    function apply(next) {
      refs.advPanel.hidden = !next;
      refs.advToggle.setAttribute('aria-expanded', next ? 'true' : 'false');
      ssSet(SS.advanced, next ? '1' : '0');
    }
    apply(open);
    refs.advToggle.addEventListener('click', function () {
      apply(refs.advToggle.getAttribute('aria-expanded') !== 'true');
    });
  }

  /* ---------------------------------------------------------- 基础选项数据 */

  function fillIntensity() {
    if (!refs.intensity) return;
    refs.intensity.innerHTML = '';
    INTENSITIES.forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.key;
      option.textContent = item.label;
      refs.intensity.appendChild(option);
    });
    refs.intensity.value = 'balanced';
  }

  function loadLibraryMeta() {
    api().get('/api/prompt-library', {}).then(function (data) {
      state.profiles = (data && data.profiles) || [];
      state.templates = (data && data.templates) || [];
      fillProfiles();
      fillTemplates();
      if (AIBAR.library && AIBAR.library.init && !AIBAR.library._ready) {
        AIBAR.library._ready = true;
        AIBAR.library.init();
      }
    }, function (err) {
      ui().toastError(ui().errorText(err, '提示词知识库加载失败'));
      // 知识库失败也要让词库可用（词库数据来自 M7 接口）
      if (AIBAR.library && AIBAR.library.init && !AIBAR.library._ready) {
        AIBAR.library._ready = true;
        AIBAR.library.init();
      }
    });
  }

  function fillProfiles() {
    if (!refs.profile) return;
    refs.profile.innerHTML = '';
    state.profiles.forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.key;
      option.textContent = item.label;
      refs.profile.appendChild(option);
    });
    if (!refs.profile.value && state.profiles.length) {
      var generic = state.profiles.filter(function (item) { return item.key === 'generic'; })[0];
      refs.profile.value = generic ? generic.key : state.profiles[0].key;
    }
  }

  function fillTemplates() {
    if (!refs.template) return;
    refs.template.innerHTML = '';
    var none = doc.createElement('option');
    none.value = '';
    none.textContent = '不套用模板';
    refs.template.appendChild(none);
    state.templates.forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.id;
      option.textContent = item.name;
      option.title = item.description || '';
      refs.template.appendChild(option);
    });
  }

  function loadWorkflows() {
    api().get('/api/workflows', { page: 1, page_size: 100 }).then(function (data) {
      state.workflows = (data && data.items) || [];
      fillWorkflows();
    }, function () {
      state.workflows = [];
      fillWorkflows();
    });
  }

  function fillWorkflows() {
    if (!refs.workflow) return;
    refs.workflow.innerHTML = '';
    var none = doc.createElement('option');
    none.value = '';
    none.textContent = '未选择';
    refs.workflow.appendChild(none);
    state.workflows.forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.filename;
      option.textContent = item.name;
      refs.workflow.appendChild(option);
    });
  }

  /* ---------------------------------------------------------- 操作栏 */

  function bindActions() {
    if (refs.btnExpand) refs.btnExpand.addEventListener('click', expand);
    if (refs.btnRestore) refs.btnRestore.addEventListener('click', restoreOriginal);
    if (refs.btnApply) refs.btnApply.addEventListener('click', applyToWorkflow);
    if (refs.btnLaunch) refs.btnLaunch.addEventListener('click', launchWithPrompt);
    if (refs.btnSaveHistory) refs.btnSaveHistory.addEventListener('click', saveHistory);
    updateContextActions();
  }

  function updateContextActions() {
    var hasResult = !!(state.result && state.result.expanded_positive);
    if (refs.context) refs.context.hidden = !hasResult;
    if (refs.btnApply) refs.btnApply.disabled = !hasResult;
    if (refs.btnLaunch) refs.btnLaunch.disabled = !hasResult;
    if (refs.btnSaveHistory) refs.btnSaveHistory.disabled = !hasResult;
  }

  function expand() {
    if (state.expanding) return;
    var text = getPromptText();
    if (!text.trim()) {
      ui().toast({ message: '请先输入原始提示词', type: 'warning' });
      if (refs.prompt) refs.prompt.focus();
      return;
    }
    if (text.length > 2000) {
      ui().toast({ message: '原始提示词超过 2000 字符上限', type: 'error' });
      return;
    }

    state.baseline = text;
    state.expanding = true;
    ui().setBusy(refs.btnExpand, true);
    renderResultLoading();

    var body = {
      original_prompt: text,
      profile: refs.profile ? refs.profile.value : 'generic',
      intensity: refs.intensity ? refs.intensity.value : 'balanced',
      template_id: refs.template && refs.template.value ? refs.template.value : null,
      options: {},
      provider: 'rules'
    };

    api().post('/api/prompts/expand', body, { timeout: api().LONG_TIMEOUT }).then(function (data) {
      state.expanding = false;
      ui().setBusy(refs.btnExpand, false);
      state.result = data;
      renderResult(data);
      ui().toast({ message: '扩写完成', type: 'success' });
    }, function (err) {
      state.expanding = false;
      ui().setBusy(refs.btnExpand, false);
      // 失败不清空上次有效结果
      renderResultError(err);
    });
  }

  function restoreOriginal() {
    if (!state.baseline) {
      ui().toast({ message: '还没有可恢复的原文', type: 'info' });
      return;
    }
    var current = getPromptText();
    setPromptText(state.baseline);
    ui().toast({
      message: '已恢复扩写前的原文',
      type: 'info',
      action: {
        label: '撤销',
        onClick: function () { setPromptText(current); }
      }
    });
  }

  function applyToWorkflow() {
    var filename = refs.workflow ? refs.workflow.value : '';
    var workflow = state.workflows.filter(function (item) { return item.filename === filename; })[0];
    if (!workflow) {
      ui().toast({ message: '请先在高级设置中选择目标工作流', type: 'warning' });
      return;
    }
    var result = state.result;
    if (!result) return;

    var body = doc.createElement('div');
    body.className = 'filter-grid';
    body.appendChild(makeReadonly('目标工作流', workflow.name));
    body.appendChild(makeReadonly('模型档案', result.profile || ''));
    var positive = doc.createElement('div');
    positive.className = 'kv';
    positive.appendChild(ui().el('span', 'kv-key', '正向提示词'));
    positive.appendChild(ui().el('div', 'result-text', result.expanded_positive || ''));
    body.appendChild(positive);
    if (result.expanded_negative) {
      var negative = doc.createElement('div');
      negative.className = 'kv';
      negative.appendChild(ui().el('span', 'kv-key', '负向提示词'));
      negative.appendChild(ui().el('div', 'result-text', result.expanded_negative));
      body.appendChild(negative);
    }
    var tip = ui().el('div', 'callout callout-info');
    tip.innerHTML = '<span class="callout-icon">' + AIBAR.icons.get('info', 16) +
      '</span><span>覆盖工作流模板中的提示词需要二次确认。确认后提示词会复制到剪贴板，请在 ComfyUI 中粘贴到对应节点。</span>';
    body.appendChild(tip);

    ui().modal({
      title: '应用到工作流模板',
      desc: '确认后将覆盖该工作流中的提示词内容。',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost' },
        {
          label: '确认应用',
          variant: 'primary',
          onClick: function () {
            ui().copyText(result.expanded_positive || '');
            ui().toast({ message: '已复制正向提示词，可在 ComfyUI 中粘贴', type: 'success' });
          }
        }
      ]
    });
  }

  function makeReadonly(label, value) {
    var wrap = doc.createElement('div');
    wrap.className = 'kv';
    wrap.appendChild(ui().el('span', 'kv-key', label));
    wrap.appendChild(ui().el('div', 'text-sm break-any', value));
    return wrap;
  }

  function launchWithPrompt() {
    var result = state.result;
    if (!result) return;
    api().get('/api/comfyui/status', {}).then(function (status) {
      var running = !!(status && status.running);
      var body = doc.createElement('div');
      body.className = 'filter-grid';
      body.appendChild(makeReadonly('ComfyUI 状态', running ? '运行中' : '已停止'));
      var tip = ui().el('div', 'callout ' + (running ? 'callout-info' : 'callout-warning'));
      tip.innerHTML = '<span class="callout-icon">' + AIBAR.icons.get('info', 16) +
        '</span><span>' + (running
          ? '将复制本次扩写的正向提示词，并打开 ComfyUI 页面由你粘贴执行。'
          : 'ComfyUI 当前未运行，可先启动后再打开。') + '</span>';
      body.appendChild(tip);

      var actions = [{ label: '取消', variant: 'ghost' }];
      if (!running) {
        actions.push({
          label: '启动 ComfyUI',
          variant: 'secondary',
          close: false,
          onClick: function () {
            api().post('/api/comfyui/start', {}).then(function (data) {
              ui().toast(data && data.started
                ? { message: 'ComfyUI 正在启动', type: 'success' }
                : { message: (data && data.message) || '未能启动 ComfyUI', type: 'warning' });
              if (AIBAR.app && AIBAR.app.refreshComfyStatus) AIBAR.app.refreshComfyStatus();
            }, function (err) {
              ui().toastError(ui().errorText(err, '启动失败'));
            });
          }
        });
      }
      actions.push({
        label: '复制并打开 ComfyUI',
        variant: 'primary',
        onClick: function () {
          ui().copyText(result.expanded_positive || '');
          var host = (status && status.host) || '127.0.0.1';
          var port = (status && status.port) || 8188;
          root.open('http://' + host + ':' + port, '_blank', 'noopener');
        }
      });

      ui().modal({
        title: '使用此提示词启动',
        desc: 'AIBAR 不代你提交生成任务，只负责把提示词带到 ComfyUI。',
        body: body,
        actions: actions
      });
    }, function (err) {
      ui().toastError(ui().errorText(err, '无法读取 ComfyUI 状态'));
    });
  }

  function saveHistory() {
    var result = state.result;
    if (!result) return;
    var payload = {
      original_prompt: result.original_prompt || state.baseline || '',
      expanded_positive: result.expanded_positive || '',
      expanded_negative: result.expanded_negative || '',
      profile: result.profile || 'generic',
      intensity: result.intensity || 'balanced',
      provider: result.provider || 'rules',
      template_id: result.template_id || null,
      sections: result.sections || [],
      additions: result.additions || [],
      warnings: result.warnings || [],
      is_favorite: false
    };
    ui().setBusy(refs.btnSaveHistory, true);
    api().post('/api/prompts/history', payload).then(function () {
      ui().setBusy(refs.btnSaveHistory, false);
      ui().toastSuccess('已保存到扩写历史');
      loadHistory(true);
    }, function (err) {
      ui().setBusy(refs.btnSaveHistory, false);
      ui().toastError(ui().errorText(err, '保存失败'));
    });
  }

  /* ---------------------------------------------------------- 结果区渲染 */

  function renderResultLoading() {
    if (!refs.resultBody) return;
    refs.resultBody.innerHTML = '';
    var loading = ui().loadingInline('正在扩写，保留原始提示词与当前设置…');
    loading.id = 'result-loading';
    refs.resultBody.appendChild(loading);
  }

  function renderResultError(err) {
    if (!refs.resultBody) return;
    var node = ui().errorState(ui().errorText(err, '扩写失败'), function () { expand(); });
    node.id = 'result-error';
    if (state.result) {
      // 保留上次有效结果，错误提示置顶
      refs.resultBody.innerHTML = '';
      refs.resultBody.appendChild(node);
      refs.resultBody.appendChild(renderResultView(state.result));
    } else {
      refs.resultBody.innerHTML = '';
      refs.resultBody.appendChild(node);
      refs.resultBody.appendChild(renderEmptyState());
    }
  }

  function renderEmptyState() {
    var node = ui().emptyState({
      icon: 'sparkles',
      title: '还没有扩写结果',
      desc: '扩写后会在这里出现：正向提示词、适用时的负向提示词、按维度拆分的新增内容，以及模型适配警告。'
    });
    node.id = 'result-empty';
    var tips = ui().el('ul', 'kv-list text-meta text-secondary');
    ['先在左侧词库中点选片段，再回到画布一键扩写',
      '扩写不会改动你的原始提示词，随时可以恢复',
      '支持 Ctrl / Cmd + Enter 快速提交'].forEach(function (text) {
      var item = ui().el('li', '', '· ' + text);
      tips.appendChild(item);
    });
    node.appendChild(tips);
    return node;
  }

  function renderResult(data) {
    if (!refs.resultBody) return;
    refs.resultBody.innerHTML = '';
    refs.resultBody.appendChild(renderResultView(data));
    updateContextActions();
  }

  function renderResultView(data) {
    var wrap = doc.createElement('div');
    wrap.className = 'view';
    wrap.id = 'result-view';

    /* 正向提示词置顶 + 明显复制操作 */
    var positive = ui().el('section', 'result-block');
    var posHead = ui().el('div', 'result-block-head');
    posHead.appendChild(ui().el('span', '', '正向提示词'));
    var profileLabel = profileLabelOf(data.profile);
    if (profileLabel) posHead.appendChild(ui().el('span', 'badge', profileLabel));
    posHead.appendChild(ui().el('span', 'grow'));
    var copyPos = ui().el('button', 'btn btn-secondary btn-sm');
    copyPos.type = 'button';
    copyPos.id = 'btn-copy-positive';
    copyPos.innerHTML = AIBAR.icons.get('copy', 14) + '<span>复制</span>';
    copyPos.addEventListener('click', function () { ui().copyText(data.expanded_positive || ''); });
    posHead.appendChild(copyPos);
    positive.appendChild(posHead);
    positive.appendChild(ui().el('div', 'result-text', data.expanded_positive || ''));
    wrap.appendChild(positive);

    /* 负向提示词：仅在有内容时出现 */
    if (data.expanded_negative) {
      var negative = ui().el('section', 'result-block');
      var negHead = ui().el('div', 'result-block-head');
      negHead.appendChild(ui().el('span', '', '负向提示词'));
      negHead.appendChild(ui().el('span', 'grow'));
      var copyNeg = ui().el('button', 'btn btn-secondary btn-sm');
      copyNeg.type = 'button';
      copyNeg.id = 'btn-copy-negative';
      copyNeg.innerHTML = AIBAR.icons.get('copy', 14) + '<span>复制</span>';
      copyNeg.addEventListener('click', function () { ui().copyText(data.expanded_negative || ''); });
      negHead.appendChild(copyNeg);
      negative.appendChild(negHead);
      negative.appendChild(ui().el('div', 'result-text', data.expanded_negative));
      wrap.appendChild(negative);
    }

    /* 维度片段 */
    var sections = data.sections || [];
    if (sections.length) {
      var secBlock = ui().el('section', 'result-block');
      var secHead = ui().el('div', 'result-block-head');
      secHead.appendChild(ui().el('span', '', '维度片段'));
      secHead.appendChild(ui().el('span', 'grow'));
      secHead.appendChild(ui().el('span', 'badge', sections.length + ' 项'));
      secBlock.appendChild(secHead);
      var secList = ui().el('div', 'kv-list');
      sections.forEach(function (item) {
        var node = ui().el('div', 'section-item');
        var head = ui().el('div', 'entry-foot');
        head.appendChild(ui().el('span', 'badge', item.dimension_label || item.dimension || '未分类'));
        if (item.is_new) head.appendChild(ui().el('span', 'badge badge-brand', '新增'));
        node.appendChild(head);
        node.appendChild(ui().el('div', 'section-text', item.text || ''));
        secList.appendChild(node);
      });
      secBlock.appendChild(secList);
      wrap.appendChild(secBlock);
    }

    /* 新增说明 */
    var additions = data.additions || [];
    if (additions.length) {
      var addBlock = ui().el('section', 'result-block');
      addBlock.appendChild(blockHead('本次新增', additions.length + ' 项'));
      var addList = ui().el('ul', 'kv-list text-meta text-secondary');
      additions.forEach(function (text) {
        addList.appendChild(ui().el('li', 'break-any', '· ' + text));
      });
      addBlock.appendChild(addList);
      wrap.appendChild(addBlock);
    }

    /* 模型警告 */
    var warnings = data.warnings || [];
    if (warnings.length) {
      var warn = ui().el('div', 'callout callout-warning');
      warn.innerHTML = '<span class="callout-icon">' + AIBAR.icons.get('alert', 16) + '</span>';
      var warnList = ui().el('ul', 'kv-list');
      warnings.forEach(function (text) {
        warnList.appendChild(ui().el('li', 'break-any', '· ' + text));
      });
      var warnWrap = ui().el('div');
      warnWrap.appendChild(ui().el('div', 'text-sm', '模型适配提示'));
      warnWrap.appendChild(warnList);
      warn.appendChild(warnWrap);
      wrap.appendChild(warn);
    }

    var meta = ui().el('div', 'entry-foot');
    meta.appendChild(ui().el('span', 'badge', '强度 ' + intensityLabel(data.intensity)));
    if (data.duration_ms !== undefined) {
      meta.appendChild(ui().el('span', 'badge', '耗时 ' + data.duration_ms + ' ms'));
    }
    meta.appendChild(ui().el('span', 'badge', '引擎 ' + (data.provider || 'rules')));
    wrap.appendChild(meta);

    return wrap;
  }

  function blockHead(title, extra) {
    var head = ui().el('div', 'result-block-head');
    head.appendChild(ui().el('span', '', title));
    head.appendChild(ui().el('span', 'grow'));
    if (extra) head.appendChild(ui().el('span', 'badge', extra));
    return head;
  }

  function profileLabelOf(key) {
    var found = state.profiles.filter(function (item) { return item.key === key; })[0];
    return found ? found.label : '';
  }

  function intensityLabel(key) {
    var found = INTENSITIES.filter(function (item) { return item.key === key; })[0];
    return found ? found.label : (key || '均衡');
  }

  /* ---------------------------------------------------------- 历史 */

  function bindHistory() {
    if (!refs.historyToggle || !refs.historyPanel) return;
    var open = ssGet(SS.historyOpen, '0') === '1';
    function apply(next) {
      refs.historyPanel.hidden = !next;
      refs.historyToggle.setAttribute('aria-expanded', next ? 'true' : 'false');
      ssSet(SS.historyOpen, next ? '1' : '0');
      if (next) loadHistory();
    }
    apply(open);
    refs.historyToggle.addEventListener('click', function () {
      apply(refs.historyToggle.getAttribute('aria-expanded') !== 'true');
    });
  }

  function loadHistory(force) {
    if (!refs.historyList) return;
    if (state.historyLoaded && !force) {
      renderHistory();
      return;
    }
    refs.historyList.innerHTML = '';
    refs.historyList.appendChild(ui().loadingInline('正在加载历史…'));
    api().get('/api/prompts/history', { limit: 20 }).then(function (data) {
      state.history = (data && data.items) || [];
      state.historyLoaded = true;
      renderHistory();
    }, function (err) {
      refs.historyList.innerHTML = '';
      refs.historyList.appendChild(ui().errorState(ui().errorText(err, '历史加载失败'), function () { loadHistory(true); }));
    });
  }

  function renderHistory() {
    if (!refs.historyList) return;
    refs.historyList.innerHTML = '';
    if (!state.history.length) {
      refs.historyList.appendChild(ui().emptyState({
        icon: 'list',
        title: '暂无扩写历史',
        desc: '保存扩写结果后可以在这里复用、收藏或删除。'
      }));
      return;
    }
    state.history.forEach(function (item) {
      refs.historyList.appendChild(renderHistoryItem(item));
    });
  }

  function renderHistoryItem(item) {
    var node = ui().el('div', 'history-item');
    var head = ui().el('div', 'entry-foot');
    head.appendChild(ui().el('span', 'badge', profileLabelOf(item.profile) || item.profile || ''));
    head.appendChild(ui().el('span', 'badge', intensityLabel(item.intensity)));
    head.appendChild(ui().el('span', '', ui().formatTime(item.created_at)));
    if (item.is_favorite) head.appendChild(ui().el('span', 'badge badge-warning', '已收藏'));
    node.appendChild(head);
    node.appendChild(ui().el('div', 'history-text clamp-2', item.expanded_positive || ''));

    var actions = ui().el('div', 'result-actions');

    var reuse = ui().el('button', 'btn btn-secondary btn-sm', '复用');
    reuse.type = 'button';
    reuse.addEventListener('click', function () {
      setPromptText(item.original_prompt || '');
      state.result = {
        original_prompt: item.original_prompt,
        expanded_positive: item.expanded_positive,
        expanded_negative: item.expanded_negative,
        profile: item.profile,
        intensity: item.intensity,
        provider: item.provider,
        sections: item.sections || [],
        additions: item.additions || [],
        warnings: item.warnings || []
      };
      renderResult(state.result);
      ui().toastSuccess('已载入该历史结果');
    });
    actions.appendChild(reuse);

    var fav = ui().el('button', 'btn btn-ghost btn-sm', item.is_favorite ? '取消收藏' : '收藏');
    fav.type = 'button';
    fav.addEventListener('click', function () {
      api().put('/api/prompts/history/' + encodeURIComponent(item.id) + '/favorite', { favorite: !item.is_favorite })
        .then(function (data) {
          item.is_favorite = !!(data && data.is_favorite);
          renderHistory();
        }, function (err) {
          ui().toastError(ui().errorText(err, '操作失败'));
        });
    });
    actions.appendChild(fav);

    var del = ui().el('button', 'btn btn-danger btn-sm');
    del.type = 'button';
    del.setAttribute('aria-label', '删除该历史记录');
    del.innerHTML = AIBAR.icons.get('trash', 14);
    del.addEventListener('click', function () {
      ui().confirm({
        title: '删除这条历史？',
        message: '删除后无法恢复，原始提示词仍保留在画布中。',
        confirmLabel: '删除',
        danger: true,
        onConfirm: function () {
          api().del('/api/prompts/history/' + encodeURIComponent(item.id)).then(function () {
            state.history = state.history.filter(function (row) { return row.id !== item.id; });
            renderHistory();
            ui().toastSuccess('已删除该历史记录');
          }, function (err) {
            ui().toastError(ui().errorText(err, '删除失败'));
          });
        }
      });
    });
    actions.appendChild(del);

    node.appendChild(actions);
    return node;
  }

  /* ---------------------------------------------------------- 对外接口（供词库回填 / 画廊反推） */

  function getPromptText() {
    return refs.prompt ? refs.prompt.value : '';
  }

  function setPromptText(text) {
    if (!refs.prompt) return;
    refs.prompt.value = text === null || text === undefined ? '' : String(text);
    updateCharCount();
    updateCaretHint();
  }

  function getProfile() {
    return refs.profile ? refs.profile.value : 'generic';
  }

  /**
   * 词库回填：光标插入 + 去重 + 可撤销 Toast（PRD M7.3）。
   * @returns {boolean} 是否真的插入
   */
  function insertFromLibrary(item) {
    var text = item && item.prompt_text ? String(item.prompt_text) : '';
    if (!text.trim()) return false;
    var current = getPromptText();
    var caret = (doc.activeElement === refs.prompt && typeof refs.prompt.selectionStart === 'number')
      ? refs.prompt.selectionStart
      : state.lastCaret;
    var result = AIBAR.logic.insertAtCursor(current, text, caret, getProfile());

    if (!result.inserted) {
      if (result.reason === 'duplicate') {
        ui().toast({ message: '原始提示词中已有相同片段，未重复插入', type: 'warning' });
      }
      return false;
    }
    var previous = current;
    var previousCaret = caret;
    setPromptText(result.text);
    if (refs.prompt && typeof result.cursorPos === 'number') {
      try {
        refs.prompt.setSelectionRange(result.cursorPos, result.cursorPos);
      } catch (err) {
        /* 某些状态下设置选区会抛错，忽略即可 */
      }
    }
    state.lastCaret = result.cursorPos;
    updateCaretHint();

    ui().toast({
      message: '已加入原始提示词：' + (item.title || '片段'),
      type: 'success',
      action: {
        label: '撤销',
        onClick: function () {
          setPromptText(previous);
          state.lastCaret = previousCaret;
          if (refs.prompt && typeof previousCaret === 'number') {
            try {
              refs.prompt.setSelectionRange(previousCaret, previousCaret);
            } catch (err) {
              /* 忽略 */
            }
          }
        }
      }
    });
    return true;
  }

  /** 反推结果填入画布（不自动扩写） */
  function fillFromReverse(text) {
    var result = AIBAR.logic.insertAtCursor(getPromptText(), text, state.lastCaret, getProfile());
    if (!result.inserted) {
      if (result.reason === 'duplicate') {
        ui().toast({ message: '原始提示词中已有相同内容，未重复插入', type: 'warning' });
      }
      return false;
    }
    setPromptText(result.text);
    state.lastCaret = result.cursorPos;
    ui().toastSuccess('已填入原始提示词，可直接一键扩写');
    return true;
  }

  /** 画廊「反推提示词」入口 */
  function enterReverseWithImage(imageId) {
    if (AIBAR.app) AIBAR.app.navigate('studio');
    setMode('reverse', { silent: true });
    if (AIBAR.reverse) AIBAR.reverse.loadGalleryImage(imageId);
  }

  AIBAR.studio = {
    init: init,
    getPromptText: getPromptText,
    setPromptText: setPromptText,
    getProfile: getProfile,
    getProfiles: function () { return state.profiles; },
    getMediaType: function () { return state.media; },
    insertFromLibrary: insertFromLibrary,
    fillFromReverse: fillFromReverse,
    enterReverseWithImage: enterReverseWithImage,
    setMode: setMode,
    expand: expand,
    focusPrompt: function () {
      if (refs.prompt) refs.prompt.focus();
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
