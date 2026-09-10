/* ============================================================
   AIBAR · M9 图片提示词反推 + M10 Provider 中心
   - 素材与预览 / 分析设置 / 结构化结果 三区（窄屏分段切换）
   - 阶段进度「读取图片 / 检查元数据 / 视觉分析 / 结构化整理」，不使用虚假百分比
   - Provider 状态中文原因 + 与原因对应的恢复操作，不显示原始异常堆栈
   - 外部 Provider 发起请求前必须确认图片外发
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  var AUTO_KEY = 'auto';

  /* ============================================================
     一、纯逻辑：Provider 状态文案映射
     ============================================================ */

  var STATUS_MAP = {
    ready: {
      label: '可用',
      tone: 'success',
      action: 'none',
      actionLabel: '',
      hint: '可以立即用于反推分析。'
    },
    unconfigured: {
      label: '未配置',
      tone: 'neutral',
      action: 'configure',
      actionLabel: '配置高级 Provider',
      hint: '尚未配置服务地址或密钥，可改用本地 Provider 或元数据恢复。'
    },
    offline: {
      label: '服务未运行',
      tone: 'error',
      action: 'start_comfyui',
      actionLabel: '启动 ComfyUI',
      hint: 'ComfyUI 未运行或无法访问，启动后重新检测即可恢复。'
    },
    missing_node: {
      label: '缺少节点',
      tone: 'error',
      action: 'check_nodes',
      actionLabel: '查看缺失节点',
      hint: 'ComfyUI 缺少该 Provider 需要的自定义节点。'
    },
    missing_model: {
      label: '缺少模型',
      tone: 'error',
      action: 'check_nodes',
      actionLabel: '查看缺失模型',
      hint: '模型文件缺失或不完整。'
    },
    missing_mmproj: {
      label: '缺少视觉投影文件',
      tone: 'error',
      action: 'check_nodes',
      actionLabel: '查看缺失文件',
      hint: '缺少 mmproj 视觉投影文件，模型无法处理图片。'
    },
    incompatible_runtime: {
      label: '运行时不兼容',
      tone: 'error',
      action: 'configure',
      actionLabel: '查看安装指引',
      hint: '推理运行时依赖不兼容，需要安装对应构建。'
    },
    out_of_memory: {
      label: '内存不足',
      tone: 'warning',
      action: 'retry',
      actionLabel: '释放内存后重试',
      hint: '显存或内存不足，关闭其他大模型后重试。'
    },
    unauthorized: {
      label: '未确认外发',
      tone: 'warning',
      action: 'consent',
      actionLabel: '确认图片外发',
      hint: '外部 Provider 需要你先确认把图片发送到第三方服务。'
    },
    error: {
      label: '异常',
      tone: 'error',
      action: 'refresh',
      actionLabel: '重新检测',
      hint: 'Provider 检测失败，重新检测后再试。'
    }
  };

  var UNKNOWN_STATUS = {
    label: '未知状态',
    tone: 'neutral',
    action: 'refresh',
    actionLabel: '重新检测',
    hint: '状态无法识别，请重新检测。'
  };

  /**
   * Provider 状态 -> 中文原因 + 下一步操作。
   * @param {string} status 后端状态枚举
   * @returns {{status:string,label:string,tone:string,action:string,actionLabel:string,hint:string}}
   */
  function formatProviderStatus(status) {
    var key = String(status || '').trim();
    var found = STATUS_MAP[key];
    if (!found) {
      return {
        status: key || 'unknown',
        label: UNKNOWN_STATUS.label,
        tone: UNKNOWN_STATUS.tone,
        action: UNKNOWN_STATUS.action,
        actionLabel: UNKNOWN_STATUS.actionLabel,
        hint: UNKNOWN_STATUS.hint
      };
    }
    return {
      status: key,
      label: found.label,
      tone: found.tone,
      action: found.action,
      actionLabel: found.actionLabel,
      hint: found.hint
    };
  }

  function qualityTierLabel(tier) {
    if (tier === 'advanced') return '高级';
    if (tier === 'original') return '原始元数据';
    return '基础';
  }

  var STAGE_KEYS = ['reading', 'metadata', 'vision', 'structuring'];
  var STAGE_LABELS = {
    reading: '读取图片',
    metadata: '检查元数据',
    vision: '视觉分析',
    structuring: '结构化整理'
  };

  /* 任务终态：进入这些状态就不必再轮询（与 reverse/service.py 的 JOB_STATUSES 对齐） */
  var STATUS_PENDING = 'pending';
  var STATUS_COMPLETED = 'completed';
  var STATUS_CANCELLED = 'cancelled';
  var TERMINAL_STATUSES = [STATUS_COMPLETED, 'failed', STATUS_CANCELLED];

  /* 提交任务只需落一行库，不该等推理；轮询每次都是一次轻量读 */
  var SUBMIT_TIMEOUT_MS = 30000;
  var POLL_TIMEOUT_MS = 10000;
  var POLL_INTERVAL_MS = 700;
  /* 连续拉不到才判定失败：单次网络抖动不该让几十秒的分析白跑 */
  var POLL_MAX_MISSES = 3;

  /** 是否已是终态（纯函数，供轮询与收口共用）。 */
  function isTerminalStatus(status) {
    return TERMINAL_STATUSES.indexOf(String(status || '')) !== -1;
  }

  /** 后端 stage 的中文兜底文案（后端也返回 stage_label，这里只作降级）。 */
  var FALLBACK_STAGE_LABELS = {
    queued: '排队中',
    done: '已完成',
    failed: '已失败',
    cancelled: '已取消'
  };

  /**
   * 后端任务 -> 界面要高亮的阶段。纯函数，不碰 DOM，便于单测。
   *
   * @param {{status?:string, stage?:string, stage_label?:string}} job
   * @returns {{status:string, stage:string, label:string, activeKeys:string[], terminal:boolean}}
   */
  function stageView(job) {
    var data = job || {};
    var status = String(data.status || '');
    var stage = String(data.stage || '');
    var index = STAGE_KEYS.indexOf(stage);
    if (status === STATUS_PENDING) index = -1;         // 还没开始，一格都别点亮
    if (status === STATUS_COMPLETED) index = STAGE_KEYS.length - 1;
    if (status === 'failed' || status === STATUS_CANCELLED) index = -1;
    return {
      status: status,
      stage: stage,
      label: String(
        data.stage_label || STAGE_LABELS[stage] || FALLBACK_STAGE_LABELS[stage] || ''
      ),
      activeKeys: index >= 0 ? STAGE_KEYS.slice(0, index + 1) : [],
      terminal: isTerminalStatus(status)
    };
  }

  /* ============================================================
     二、DOM 层
     ============================================================ */

  var state = {
    mode: 'auto',
    image: null,          // {kind:'upload'|'gallery', id, preview_url, filename, width, height, size_bytes, format, metadata:'unknown'|'yes'|'no'}
    job: null,
    result: null,
    stale: false,
    running: false,
    cancelled: false,
    pollTimer: null,      // 轮询真实进度的定时器（替代原来的假进度条）
    polling: false,       // 上一轮请求是否还在路上
    pollMisses: 0,        // 连续拉取失败次数
    controller: null,     // 本次任务的 AbortController，点取消时中断在途请求
    providers: [],
    defaultProvider: AUTO_KEY,
    step: 'source',
    history: []
  };

  var refs = {};

  function ui() { return AIBAR.ui; }
  function api() { return AIBAR.api; }

  /* ---------------------------------------------------------- 初始化 */

  function init() {
    loadErrorCodes();
    refs.steps = doc.getElementById('rev-steps');
    refs.colSource = doc.getElementById('rev-col-source');
    refs.colSettings = doc.getElementById('rev-col-settings');
    refs.colResults = doc.getElementById('rev-col-results');
    refs.drop = doc.getElementById('rev-drop');
    refs.file = doc.getElementById('rev-file');
    refs.choose = doc.getElementById('btn-rev-choose');
    refs.paste = doc.getElementById('btn-rev-paste');
    refs.previewWrap = doc.getElementById('rev-preview-wrap');
    refs.preview = doc.getElementById('rev-preview');
    refs.meta = doc.getElementById('rev-meta');
    refs.clear = doc.getElementById('btn-rev-clear');
    refs.sourceNote = doc.getElementById('rev-source-note');
    refs.sourceMode = doc.getElementById('rev-source-mode');
    refs.profile = doc.getElementById('rev-profile');
    refs.precision = doc.getElementById('rev-precision');
    refs.provider = doc.getElementById('rev-provider');
    refs.providerNote = doc.getElementById('rev-provider-note');
    refs.manage = doc.getElementById('btn-manage-providers');
    refs.run = doc.getElementById('btn-reverse-run');
    refs.cancel = doc.getElementById('btn-reverse-cancel');
    refs.stages = doc.getElementById('rev-stages');
    refs.status = doc.getElementById('rev-status');
    refs.resultMeta = doc.getElementById('rev-result-meta');
    refs.sections = doc.getElementById('rev-sections');
    refs.warnings = doc.getElementById('rev-warnings');
    refs.historyList = doc.getElementById('rev-history-list');
    refs.historyRefresh = doc.getElementById('btn-rev-history-refresh');

    bindSteps();
    bindUpload();
    bindSettings();
    bindActions();
    bindPaste();

    loadTargetModes();
    loadProviders();
    loadHistory();
    renderImage();
    renderStages([], '');
    renderResultEmpty();
  }

  function onEnter() {
    if (!refs.provider) return;
    if (!state.providers.length) loadProviders();
  }

  function onLeave() {
    /* 离开反推模式时保留图片与结果，回到模式时继续可用 */
  }

  /* ---------------------------------------------------------- 窄屏分段切换 */

  function bindSteps() {
    if (!refs.steps) return;
    Array.prototype.forEach.call(refs.steps.querySelectorAll('[data-step]'), function (btn) {
      btn.addEventListener('click', function () { setStep(btn.dataset.step); });
    });
    // 视口跨过断点时重新应用：宽屏三区并列，窄屏才按步骤分段
    if (root.addEventListener) {
      root.addEventListener('resize', applyStep);
    }
    setStep('source');
  }

  function isNarrow() {
    return !!(root.matchMedia && root.matchMedia('(max-width: 1024px)').matches);
  }

  /*
   * hidden 只用于窄屏分段切换。宽屏下三区始终并列，
   * 若把 hidden 带上会导致读屏软件跳过可见内容。
   */
  function applyStep() {
    var narrow = isNarrow();
    [['colSource', 'source'], ['colSettings', 'settings'], ['colResults', 'result']]
      .forEach(function (pair) {
        var node = refs[pair[0]];
        if (!node) return;
        node.hidden = narrow && state.step !== pair[1];
      });
  }

  function setStep(step) {
    state.step = step;
    Array.prototype.forEach.call(refs.steps.querySelectorAll('[data-step]'), function (btn) {
      var active = btn.dataset.step === step;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    applyStep();
  }

  /* ---------------------------------------------------------- 素材与预览 */

  function bindUpload() {
    if (refs.choose && refs.file) {
      refs.choose.addEventListener('click', function () { refs.file.click(); });
      refs.file.addEventListener('change', function () {
        if (refs.file.files && refs.file.files[0]) uploadFile(refs.file.files[0]);
        refs.file.value = '';
      });
    }
    if (refs.paste) {
      refs.paste.addEventListener('click', function () {
        ui().toast({ message: '请按 Ctrl / Cmd + V 粘贴剪贴板中的图片', type: 'info' });
        if (refs.drop) refs.drop.focus();
      });
    }
    if (refs.clear) {
      refs.clear.addEventListener('click', function () {
        if (state.result) {
          ui().confirm({
            title: '替换当前图片？',
            message: '已有反推结果，替换图片后旧结果会被标记为过期且不可误用。',
            confirmLabel: '替换图片',
            onConfirm: function () { clearImage(); }
          });
          return;
        }
        clearImage();
      });
    }
    if (!refs.drop) return;

    ['dragenter', 'dragover'].forEach(function (name) {
      refs.drop.addEventListener(name, function (event) {
        event.preventDefault();
        refs.drop.classList.add('is-dragover');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      refs.drop.addEventListener(name, function (event) {
        event.preventDefault();
        refs.drop.classList.remove('is-dragover');
      });
    });
    refs.drop.addEventListener('drop', function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files[0]) uploadFile(files[0]);
    });
  }

  function bindPaste() {
    doc.addEventListener('paste', function (event) {
      var view = doc.getElementById('view-reverse');
      if (!view || view.hidden) return;
      var items = event.clipboardData && event.clipboardData.items;
      if (!items) return;
      var file = null;
      for (var i = 0; i < items.length; i += 1) {
        if (items[i].kind === 'file') {
          var candidate = items[i].getAsFile();
          if (candidate && /^image\//.test(candidate.type)) {
            file = candidate;
            break;
          }
        }
      }
      if (!file) {
        ui().toast({ message: '剪贴板中没有受支持的图片', type: 'warning' });
        return;
      }
      event.preventDefault();
      uploadFile(file);
    });
  }

  function uploadFile(file) {
    var allowed = ['image/png', 'image/jpeg', 'image/jpg', 'image/webp'];
    var name = (file.name || '').toLowerCase();
    var okExt = /\.(png|jpe?g|webp)$/.test(name);
    if (file.type && allowed.indexOf(file.type) === -1 && !okExt) {
      ui().toast({ message: '仅支持 png / jpg / jpeg / webp 图片', type: 'error' });
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      ui().toast({ message: '图片超过 20MB 上限', type: 'error' });
      return;
    }
    var form = new FormData();
    form.append('file', file);
    ui().setBusy(refs.choose, true);
    api().upload('/api/prompt-reverse/uploads', form).then(function (data) {
      ui().setBusy(refs.choose, false);
      state.image = {
        kind: 'upload',
        id: data.upload_id,
        image_id: null,
        upload_id: data.upload_id,
        content_hash: data.content_hash || '',
        preview_url: data.preview_url || '',
        filename: data.filename || file.name,
        format: formatOf(data.filename || file.name),
        width: data.width,
        height: data.height,
        size_bytes: data.size_bytes,
        metadata: data.has_metadata ? 'yes' : 'no'
      };
      markStale();
      renderImage();
      ui().toastSuccess('图片已就绪' + (data.has_metadata ? '：检测到原始元数据' : '：未检测到元数据'));
      setStep('settings');
    }, function (err) {
      ui().setBusy(refs.choose, false);
      ui().toastError(ui().errorText(err, '图片上传失败'));
    });
  }

  function formatOf(name) {
    var match = /\.([a-z0-9]+)$/i.exec(String(name || ''));
    return match ? match[1].toUpperCase() : '—';
  }

  /** 画廊「反推提示词」入口：按 image_id 引用，不复制源文件 */
  function loadGalleryImage(imageId) {
    if (!imageId) return;
    api().get('/api/gallery/' + encodeURIComponent(imageId), {}).then(function (item) {
      state.image = {
        kind: 'gallery',
        id: item.id,
        image_id: item.id,
        upload_id: null,
        content_hash: '',
        preview_url: item.url || (item.gallery_path ? '/static/' + item.gallery_path : ''),
        filename: item.filename || '画廊图片',
        format: formatOf(item.filename),
        width: item.width,
        height: item.height,
        size_bytes: item.size_bytes,
        metadata: item.prompt ? 'yes' : 'unknown'
      };
      markStale();
      renderImage();
      ui().toastSuccess('已从画廊载入图片，可直接开始反推');
      setStep('settings');
    }, function (err) {
      ui().toastError(ui().errorText(err, '无法读取该画廊图片'));
    });
  }

  function clearImage() {
    state.image = null;
    markStale();
    renderImage();
    renderResultEmpty();
    setStep('source');
  }

  /** 替换图片后旧结果标记过期，禁止误用 */
  function markStale() {
    if (!state.result) return;
    state.stale = true;
    renderResult();
  }

  function renderImage() {
    var image = state.image;
    if (refs.previewWrap) refs.previewWrap.hidden = !image;
    if (refs.drop) refs.drop.hidden = !!image;
    if (refs.run) refs.run.disabled = !image || state.running;
    // 还没有图片时不出现「更换图片」这个无意义入口
    if (refs.clear) refs.clear.hidden = !image;
    if (!image || !refs.preview || !refs.meta) return;

    refs.preview.src = image.preview_url || '';
    refs.preview.alt = '待反推图片预览：' + (image.filename || '');

    var rows = [
      ['文件名', image.filename || '—'],
      ['格式', image.format || '—'],
      ['尺寸', (image.width && image.height) ? image.width + ' × ' + image.height : '—'],
      ['大小', ui().formatBytes(image.size_bytes)],
      ['来源', image.kind === 'gallery' ? '画廊记录' : '本地上传'],
      ['原始元数据', image.metadata === 'yes' ? '检测到原始元数据'
        : (image.metadata === 'no' ? '未检测到元数据' : '待检测')]
    ];
    refs.meta.innerHTML = '';
    rows.forEach(function (row) {
      var dt = doc.createElement('dt');
      dt.textContent = row[0];
      var dd = doc.createElement('dd');
      dd.textContent = row[1];
      refs.meta.appendChild(dt);
      refs.meta.appendChild(dd);
    });

    if (refs.sourceNote) {
      refs.sourceNote.className = 'callout callout-info';
      refs.sourceNote.innerHTML = '<span class="callout-icon">' + AIBAR.icons.get('shield', 16) +
        '</span><span>不显示图片的本机路径；元数据恢复与本地 Provider 仅在本机处理，外部 Provider 需确认后外发。</span>';
    }
  }

  /* ---------------------------------------------------------- 分析设置 */

  function loadTargetModes() {
    api().get('/api/prompt-reverse/target-modes', {}).then(function (data) {
      var modes = (data && data.source_modes) || [];
      var profiles = (data && data.profiles) || [];
      var precisions = (data && data.precisions) || [];
      fillSelect(refs.sourceMode, modes, '自动（优先元数据）');
      if (refs.sourceMode) refs.sourceMode.value = 'auto';
      fillSelect(refs.profile, profiles, '');
      if (refs.profile) refs.profile.value = 'generic';
      fillSelect(refs.precision, precisions, '');
      if (refs.precision) refs.precision.value = 'standard';
    }, function () {
      // 接口不可用时退回最小可用选项，不阻塞界面
      fillSelect(refs.sourceMode, [
        { key: 'auto', label: '自动（优先元数据）' },
        { key: 'metadata', label: '仅恢复元数据' },
        { key: 'vision', label: '视觉重新分析' }
      ], '');
      fillSelect(refs.precision, [
        { key: 'fast', label: '快速' },
        { key: 'standard', label: '标准' },
        { key: 'fine', label: '精细' }
      ], '');
      if (refs.precision) refs.precision.value = 'standard';
    });
  }

  function fillSelect(select, items, placeholder) {
    if (!select) return;
    select.innerHTML = '';
    if (placeholder) {
      var empty = doc.createElement('option');
      empty.value = '';
      empty.textContent = placeholder;
      select.appendChild(empty);
    }
    (items || []).forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.key;
      option.textContent = item.label;
      select.appendChild(option);
    });
  }

  function bindSettings() {
    [refs.sourceMode, refs.profile, refs.precision].forEach(function (node) {
      if (!node) return;
      // 切换来源 / 模型档案 / 精度都不自动发起请求（PRD M9.3）
      node.addEventListener('change', function () { updateProviderNote(); });
    });
    if (refs.provider) {
      refs.provider.addEventListener('change', function () { updateProviderNote(); });
    }
    if (refs.manage) {
      refs.manage.addEventListener('click', openProviderCenter);
    }
    if (refs.historyRefresh) {
      refs.historyRefresh.addEventListener('click', function () { loadHistory(); });
    }
  }

  /** 只展示已启用项；未启用显示配置说明，不显示不可点击的伪选项 */
  function renderProviderOptions() {
    if (!refs.provider) return;
    var available = state.providers.filter(function (item) {
      return item.available || item.key === 'metadata';
    });
    refs.provider.innerHTML = '';
    var auto = doc.createElement('option');
    auto.value = AUTO_KEY;
    auto.textContent = '自动选择（本地优先）';
    refs.provider.appendChild(auto);
    available.forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.key;
      option.textContent = item.label + '（' + (item.local ? '本地' : '外部') + ' · ' + qualityTierLabel(item.quality_tier) + '）';
      refs.provider.appendChild(option);
    });
    if (state.defaultProvider && state.defaultProvider !== AUTO_KEY) {
      refs.provider.value = state.defaultProvider;
    } else {
      refs.provider.value = AUTO_KEY;
    }
    updateProviderNote();
  }

  function currentProvider() {
    if (!refs.provider) return null;
    var key = refs.provider.value || AUTO_KEY;
    return state.providers.filter(function (item) { return item.key === key; })[0] || null;
  }

  function updateProviderNote() {
    if (!refs.providerNote) return;
    var provider = currentProvider();
    var external = provider && provider.local === false;
    refs.providerNote.className = 'callout ' + (external ? 'callout-warning' : 'callout-info');
    refs.providerNote.innerHTML = '<span class="callout-icon">' +
      AIBAR.icons.get(external ? 'cloud' : 'shield', 16) +
      '</span><span>' + (external
        ? '该 Provider 为外部服务，发起分析前会先请你确认：图片将发送至第三方服务。'
        : '元数据恢复与本地 Provider 均在本机处理，图片不会外发。') + '</span>';
  }

  /* ---------------------------------------------------------- 执行反推 */

  function bindActions() {
    if (refs.run) refs.run.addEventListener('click', runJob);
    if (refs.cancel) refs.cancel.addEventListener('click', cancelJob);
    var fill = doc.getElementById('btn-rev-fill');
    if (fill) fill.addEventListener('click', function () {
      if (!state.result || state.stale) return;
      AIBAR.studio.fillFromReverse(state.result.formatted_positive || '');
    });
    var cont = doc.getElementById('btn-rev-expand');
    if (cont) cont.addEventListener('click', function () {
      if (!state.result || state.stale) return;
      var text = state.result.formatted_positive || '';
      var filled = AIBAR.studio.fillFromReverse(text);
      if (filled) {
        AIBAR.studio.setMode('expand', { silent: true });
        AIBAR.studio.expand();
      } else {
        // 已存在相同内容时直接进入扩写流程
        AIBAR.studio.setMode('expand', { silent: true });
        AIBAR.studio.expand();
      }
    });
    var copyPos = doc.getElementById('btn-rev-copy-pos');
    if (copyPos) copyPos.addEventListener('click', function () {
      if (state.result) ui().copyText(state.result.formatted_positive || '');
    });
    var copyNeg = doc.getElementById('btn-rev-copy-neg');
    if (copyNeg) copyNeg.addEventListener('click', function () {
      if (state.result) ui().copyText(state.result.formatted_negative || '');
    });
    var save = doc.getElementById('btn-rev-save');
    if (save) save.addEventListener('click', saveRecord);
  }

  function runJob() {
    if (state.running || !state.image) return;
    var provider = currentProvider();
    var providerKey = provider ? provider.key : AUTO_KEY;

    var start = function () {
      state.running = true;
      state.cancelled = false;
      state.job = null;
      state.pollMisses = 0;
      state.controller = api().controller();
      ui().setBusy(refs.run, true);
      if (refs.cancel) refs.cancel.hidden = false;
      if (refs.run) refs.run.disabled = true;
      renderStages([], 'active');
      if (refs.status) refs.status.textContent = '提交任务…';

      var body = {
        source_mode: refs.sourceMode ? refs.sourceMode.value : 'auto',
        profile: refs.profile ? refs.profile.value : 'generic',
        precision: refs.precision ? refs.precision.value : 'standard',
        provider: providerKey,
        options: {},
        // 让后端立即返回 job_id（202 pending），阶段进度靠轮询 GET /jobs/<id> 拿真的。
        // 不这么做的话，job_id 只能和结果一起回来，界面只能靠定时器假装推进。
        async: true
      };
      if (state.image.kind === 'gallery') body.image_id = state.image.image_id;
      else body.upload_id = state.image.upload_id;

      api().post('/api/prompt-reverse/jobs', body, {
        timeout: SUBMIT_TIMEOUT_MS,
        signal: state.controller ? state.controller.signal : undefined
      }).then(function (data) {
        if (!state.running) return;         // 提交期间就被取消了
        if (state.cancelled) { settleCancelled(); return; }
        state.job = data;
        if (isTerminalStatus(data.status)) { settleFromJob(data); return; }
        // 后端若忽略 async（旧版同步执行），data 已是完整结果，这里不会走到
        startPolling();
      }, function (err) {
        finishRun();
        if (state.cancelled) { settleCancelled(); return; }
        failRun(err);
      });
    };

    // 外部 Provider：发起请求前必须确认图片外发（PRD M9.5 / M10.3）
    if (provider && provider.local === false) {
      confirmExternal(provider).then(function (okFlag) {
        if (okFlag) start();
      });
      return;
    }
    start();
  }

  function confirmExternal(provider) {
    // 已确认过则直接放行；否则必须先由用户确认，确认失败一律不发起请求
    return api().get('/api/prompt-reverse/providers/' + encodeURIComponent(provider.key) + '/consent', {})
      .then(function (data) {
        if (data && data.consented) return true;
        return ui().confirm({
          title: '图片将发送至第三方服务',
          message: '「' + provider.label + '」是外部服务，分析时这张图片会被上传到该服务。确认后仅记录服务标识与时间，不保存密钥。',
          confirmLabel: '确认外发并分析'
        });
      }, function () {
        return false;
      })
      .then(function (agreed) {
        if (!agreed) return false;
        return api().post(
          '/api/prompt-reverse/providers/' + encodeURIComponent(provider.key) + '/consent',
          { consented: true }
        ).then(function () { return true; }, function () { return false; });
      });
  }

  function finishRun() {
    state.running = false;
    stopPolling();
    state.polling = false;
    state.controller = null;
    ui().setBusy(refs.run, false);
    if (refs.cancel) refs.cancel.hidden = true;
    if (refs.run) refs.run.disabled = !state.image;
  }

  function cancelJob() {
    if (!state.running) return;
    state.cancelled = true;
    var jobId = state.job && state.job.job_id;
    if (jobId) {
      // 用另一个 controller 发取消：它不能被下面的 abort 一起干掉
      api().post('/api/prompt-reverse/jobs/' + encodeURIComponent(jobId) + '/cancel', {}).then(function () {
        /* 取消只停止未完成任务，不删除上次成功结果 */
      }, function () {
        /* 忽略：客户端已放弃本次结果 */
      });
    }
    if (state.controller) state.controller.abort();
    finishRun();
    settleCancelled();
  }

  /* ---------------------------------------------------------- 真实进度轮询 */

  function startPolling() {
    stopPolling();
    if (refs.status) refs.status.textContent = '排队中…';
    state.pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    pollOnce();  // 立刻拉一次，别让用户干等第一个间隔
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function pollOnce() {
    if (!state.running || state.polling) return;  // 上一轮还没回来就跳过，避免请求叠罗汉
    var jobId = state.job && state.job.job_id;
    if (!jobId) return;
    state.polling = true;
    api().get('/api/prompt-reverse/jobs/' + encodeURIComponent(jobId), {}, {
      timeout: POLL_TIMEOUT_MS,
      signal: state.controller ? state.controller.signal : undefined
    }).then(function (data) {
      state.polling = false;
      if (!state.running) return;
      state.pollMisses = 0;
      state.job = data;
      renderProgress(data);
      if (isTerminalStatus(data.status)) settleFromJob(data);
    }, function (err) {
      state.polling = false;
      if (!state.running || state.cancelled) return;
      // 网络抖一下不算失败，连续几次拉不到才放弃
      state.pollMisses += 1;
      if (state.pollMisses < POLL_MAX_MISSES) return;
      finishRun();
      failRun(err);
    });
  }

  /** 用后端返回的真实 stage 渲染进度（纯展示，不推进任何自造状态）。 */
  function renderProgress(job) {
    var view = stageView(job);
    if (refs.status) refs.status.textContent = view.label ? view.label + '…' : '分析中…';
    renderStages(view.activeKeys, 'active');
  }

  /** 任务进入终态后的收口：成功渲染结果，失败/取消给出对应提示。 */
  function settleFromJob(data) {
    if (data.status === STATUS_COMPLETED) {
      finishRun();
      if (state.cancelled) return;
      state.job = data;
      state.result = data;
      state.stale = false;
      if (state.image) {
        state.image.metadata = (data.source_type === 'metadata') ? 'yes' : 'no';
        renderImage();
      }
      renderStages(STAGE_KEYS, 'done');
      if (refs.status) refs.status.textContent = '分析完成';
      renderResult();
      loadHistory();
      ui().toastSuccess('反推完成：' + (data.source_label || data.source_type || ''));
      return;
    }
    if (data.status === STATUS_CANCELLED) {
      finishRun();
      settleCancelled();
      return;
    }
    finishRun();
    if (state.cancelled) { settleCancelled(); return; }
    failRun({ code: data.error_code || 'reverse_failed', message: '反推失败' });
  }

  function settleCancelled() {
    if (refs.status) refs.status.textContent = '已取消，保留上次成功结果';
    renderStages([], '');
  }

  function failRun(err) {
    if (refs.status) refs.status.textContent = '分析失败';
    renderStages(STAGE_KEYS, 'failed');
    renderJobError(err);
  }

  /* ---------------------------------------------------------- 错误码中文化

     反推任务在库里只存 error_code（没有 message），失败时前端只能显示一句「反推失败」，
     于是「余额不足」「模型没下载」「图片格式不支持」全被合并成同一句话，用户无法自助排查。
     这里拉一次全项目统一的码表（core/error_text.py）缓存起来。 */

  var _errorCodeMap = null;

  function loadErrorCodes() {
    if (_errorCodeMap) return;
    api().get('/api/error-codes').then(function (data) {
      _errorCodeMap = data || {};
    }, function () {
      _errorCodeMap = {};
    });
  }

  /** 错误码 → 中文说明；没有映射时退回后端给的具体文案，再退回通用兜底。 */
  function jobErrorText(code, err) {
    var mapped = (_errorCodeMap && code && _errorCodeMap[code]) || '';
    if (mapped) return mapped;
    return ui().errorText(err, '反推失败');
  }

  function renderStages(activeKeys, mode) {
    if (!refs.stages) return;
    refs.stages.innerHTML = '';
    STAGE_KEYS.forEach(function (key) {
      var node = ui().el('div', 'stage');
      var isActive = activeKeys.indexOf(key) !== -1;
      if (isActive && mode === 'active') node.classList.add('is-active');
      if (isActive && mode === 'done') node.classList.add('is-done');
      if (mode === 'failed' && key === 'vision') node.classList.add('is-failed');
      node.appendChild(ui().el('span', 'stage-dot'));
      node.appendChild(ui().el('span', '', STAGE_LABELS[key]));
      node.setAttribute('aria-current', isActive && mode === 'active' ? 'step' : 'false');
      refs.stages.appendChild(node);
    });
  }

  /* ---------------------------------------------------------- 结构化结果 */

  function renderResultEmpty() {
    if (!refs.sections) return;
    refs.sections.innerHTML = '';
    refs.sections.appendChild(ui().emptyState({
      icon: 'image',
      title: '还没有反推结果',
      desc: '选择图片并点击「开始反推」后，这里会出现可编辑的结构化片段、置信度与不确定性说明。'
    }));
    if (refs.resultMeta) refs.resultMeta.innerHTML = '';
    if (refs.warnings) refs.warnings.innerHTML = '';
    updateResultActions(false);
  }

  function updateResultActions(enabled) {
    ['btn-rev-fill', 'btn-rev-expand', 'btn-rev-copy-pos', 'btn-rev-copy-neg', 'btn-rev-save'].forEach(function (id) {
      var node = doc.getElementById(id);
      if (node) node.disabled = !enabled;
    });
  }

  function renderResult() {
    if (!refs.sections) return;
    var result = state.result;
    if (!result) {
      renderResultEmpty();
      return;
    }

    /* 结果页持续显示 provider / model / quantization / quality_tier / duration_ms */
    if (refs.resultMeta) {
      refs.resultMeta.innerHTML = '';
      var provider = state.providers.filter(function (item) { return item.key === result.provider; })[0];
      var meta = [
        ['Provider', (provider && provider.label) || result.provider || '—'],
        ['模型', result.provider_model || (provider && provider.model) || '—'],
        ['量化', (provider && provider.quantization) || '—'],
        ['质量级别', qualityTierLabel(result.quality_tier)],
        ['耗时', ui().formatDuration(result.duration_ms)]
      ];
      meta.forEach(function (row) {
        var badge = ui().el('span', 'badge');
        badge.appendChild(ui().el('span', 'text-tertiary', row[0] + '：'));
        badge.appendChild(ui().el('span', '', String(row[1])));
        refs.resultMeta.appendChild(badge);
      });
      var source = ui().el('span', 'badge badge-brand', result.source_label || result.source_type || '反推结果');
      refs.resultMeta.appendChild(source);
      if (state.stale) {
        refs.resultMeta.appendChild(ui().el('span', 'badge badge-warning', '图片已更换，结果已过期'));
      }
    }

    /* 警告 */
    if (refs.warnings) {
      refs.warnings.innerHTML = '';
      var warnings = result.warnings || [];
      if (warnings.length) {
        var callout = ui().el('div', 'callout callout-warning');
        callout.innerHTML = '<span class="callout-icon">' + AIBAR.icons.get('alert', 16) + '</span>';
        var list = ui().el('ul', 'kv-list');
        warnings.forEach(function (text) {
          list.appendChild(ui().el('li', 'break-any', '· ' + text));
        });
        var wrap = ui().el('div');
        wrap.appendChild(ui().el('div', 'text-sm', '不确定性提示'));
        wrap.appendChild(list);
        callout.appendChild(wrap);
        refs.warnings.appendChild(callout);
      }
    }

    refs.sections.innerHTML = '';
    var sections = result.sections || [];
    if (!sections.length) {
      refs.sections.appendChild(ui().emptyState({
        icon: 'search',
        title: '没有可用片段',
        desc: '本次分析没有产生可靠的结构化片段，可尝试提高精度或更换 Provider。'
      }));
      updateResultActions(!state.stale);
      return;
    }
    sections.forEach(function (section, index) {
      refs.sections.appendChild(renderSection(section, index));
    });
    updateResultActions(!state.stale);
  }

  function renderSection(section, index) {
    var node = ui().el('div', 'rev-section');
    node.dataset.sectionId = String(section.id || index);

    var head = ui().el('div', 'rev-section-head');
    head.appendChild(ui().el('span', 'badge', section.dimension_label || section.dimension || '未分类'));
    if (section.subcategory) head.appendChild(ui().el('span', 'badge', section.subcategory));

    var confValue = Number(section.confidence || 0);
    var confText = ui().el('span', 'conf-text', '置信度 ' + Math.round(confValue * 100) + '%');
    head.appendChild(confText);
    head.appendChild(ui().el('span', 'grow'));

    var copy = ui().el('button', 'icon-btn icon-btn-sm');
    copy.type = 'button';
    copy.setAttribute('aria-label', '复制该片段');
    copy.innerHTML = AIBAR.icons.get('copy', 14);
    copy.addEventListener('click', function () { ui().copyText(section.text || ''); });
    head.appendChild(copy);

    var remove = ui().el('button', 'icon-btn icon-btn-sm');
    remove.type = 'button';
    remove.setAttribute('aria-label', '删除该片段');
    remove.innerHTML = AIBAR.icons.get('trash', 14);
    remove.addEventListener('click', function () {
      state.result.sections.splice(index, 1);
      renderResult();
      persistEdit();
    });
    head.appendChild(remove);
    node.appendChild(head);

    var bar = ui().el('div', 'conf-bar');
    var fill = doc.createElement('i');
    fill.style.width = Math.max(0, Math.min(100, Math.round(confValue * 100))) + '%';
    bar.appendChild(fill);
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', '置信度 ' + Math.round(confValue * 100) + '%');
    node.appendChild(bar);

    var area = ui().el('textarea', 'rev-section-text input');
    area.value = section.text || '';
    area.setAttribute('aria-label', '片段内容，可编辑');
    if (section.editable === false) area.disabled = true;
    var editTimer = null;
    area.addEventListener('input', function () {
      section.text = area.value;
      if (editTimer) clearTimeout(editTimer);
      editTimer = setTimeout(persistEdit, 500);
    });
    node.appendChild(area);

    if (section.uncertainty) {
      var uncertain = ui().el('div', 'text-meta');
      uncertain.className = 'text-tertiary break-any';
      uncertain.textContent = '不确定：' + section.uncertainty;
      node.appendChild(uncertain);
    }

    var foot = ui().el('label', 'switch text-meta');
    var check = doc.createElement('input');
    check.type = 'checkbox';
    check.checked = section.selected_for_library !== false;
    check.addEventListener('change', function () {
      section.selected_for_library = check.checked;
      persistEdit();
    });
    var track = ui().el('span', 'switch-track');
    foot.appendChild(check);
    foot.appendChild(track);
    foot.appendChild(ui().el('span', '', '保存时纳入词库'));
    node.appendChild(foot);

    return node;
  }

  /** 保存用户编辑（PATCH），失败不清空已渲染结果 */
  function persistEdit() {
    var jobId = state.job && state.job.job_id;
    if (!jobId || !state.result) return;
    var sections = (state.result.sections || []).map(function (item) {
      return {
        id: item.id,
        dimension: item.dimension,
        subcategory: item.subcategory || '',
        text: item.text || '',
        confidence: item.confidence,
        uncertainty: item.uncertainty || '',
        evidence: item.evidence || '',
        editable: item.editable !== false,
        selected_for_library: item.selected_for_library !== false
      };
    });
    api().patch('/api/prompt-reverse/jobs/' + encodeURIComponent(jobId) + '/result', {
      sections: sections,
      profile: refs.profile ? refs.profile.value : 'generic'
    }).then(function (data) {
      state.result = data;
    }, function (err) {
      ui().toastError(ui().errorText(err, '编辑保存失败'));
    });
  }

  function saveRecord() {
    var jobId = state.job && state.job.job_id;
    if (!jobId) return;
    var button = doc.getElementById('btn-rev-save');
    ui().setBusy(button, true);
    api().post('/api/prompt-reverse/jobs/' + encodeURIComponent(jobId) + '/save', {}).then(function (data) {
      ui().setBusy(button, false);
      ui().toastSuccess('已入库：新增 ' + (data.inserted || 0) + ' · 合并 ' + (data.merged || 0) +
        ' · 候选 ' + (data.candidates || 0) + ' · 丢弃 ' + (data.discarded || 0));
      loadHistory();
    }, function (err) {
      ui().setBusy(button, false);
      ui().toastError(ui().errorText(err, '保存失败'));
    });
  }

  function renderJobError(err) {
    var code = err && err.code ? String(err.code) : '';
    var node = ui().el('div', 'callout callout-error');
    node.innerHTML = '<span class="callout-icon">' + AIBAR.icons.get('alert', 16) + '</span>';
    var wrap = ui().el('div');
    wrap.appendChild(ui().el('div', 'break-any', jobErrorText(code, err)));

    var actions = ui().el('div', 'callout-actions');
    var recovery = recoveryActions(code);
    recovery.forEach(function (action) {
      var btn = ui().el('button', 'btn btn-secondary btn-sm', action.label);
      btn.type = 'button';
      btn.addEventListener('click', action.onClick);
      actions.appendChild(btn);
    });
    var retry = ui().el('button', 'btn btn-secondary btn-sm', '重试');
    retry.type = 'button';
    retry.addEventListener('click', runJob);
    actions.appendChild(retry);
    var offline = (code === 'comfyui_offline' || code === 'provider_offline' || code === 'offline');
    if (!offline) {
      var editorBtn = ui().el('button', 'btn btn-ghost btn-sm', '在 ComfyUI 编辑器中打开');
      editorBtn.type = 'button';
      editorBtn.addEventListener('click', openComfyEditor);
      actions.appendChild(editorBtn);
    }
    wrap.appendChild(actions);
    node.appendChild(wrap);

    if (refs.warnings) {
      refs.warnings.innerHTML = '';
      refs.warnings.appendChild(node);
    }
    // 失败保留图片、设置和上次有效结果
    if (!state.result) renderResultEmpty();
  }

  /** 按错误原因给出恢复操作（PRD M10.4） */
  function openComfyEditor() {
    api().get('/api/comfyui/status', {}).then(function (d) {
      var url = 'http://' + (d.host || '127.0.0.1') + ':' + (d.port || 8188);
      window.open(url, '_blank');
    }, function () {
      window.open('http://127.0.0.1:8188', '_blank');
    });
  }

  function recoveryActions(code) {
    var list = [];
    if (code === 'comfyui_offline' || code === 'provider_offline' || code === 'offline') {
      list.push({
        label: '启动 ComfyUI',
        onClick: function () {
          api().post('/api/comfyui/start', {}).then(function (data) {
            ui().toast(data && data.started ? { message: 'ComfyUI 正在启动', type: 'success' }
              : { message: (data && data.message) || '未能启动 ComfyUI', type: 'warning' });
            if (AIBAR.app) AIBAR.app.refreshComfyStatus();
          }, function (err2) {
            ui().toastError(ui().errorText(err2, '启动失败'));
          });
        }
      });
    }
    if (code === 'missing_node' || code === 'missing_model' || code === 'missing_mmproj') {
      list.push({ label: '查看缺失节点/模型', onClick: openProviderCenter });
    }
    if (code === 'provider_unavailable' || code === 'unconfigured' || code === 'unauthorized' ||
      code === 'incompatible_runtime' || code === 'not_configured') {
      list.push({ label: '配置高级 Provider', onClick: openProviderCenter });
    }
    list.push({
      label: '重新检测 Provider',
      onClick: function () { refreshProviders(true); }
    });
    list.push({
      label: '改用元数据恢复',
      onClick: function () {
        if (refs.sourceMode) refs.sourceMode.value = 'metadata';
        updateProviderNote();
        ui().toast({ message: '已切换为仅恢复元数据，点击开始反推重试', type: 'info' });
      }
    });
    return list;
  }

  /* ---------------------------------------------------------- Provider 中心 */

  function loadProviders() {
    api().get('/api/prompt-reverse/providers', {}).then(function (data) {
      state.providers = (data && data.items) || [];
      state.defaultProvider = (data && data.default_provider) || AUTO_KEY;
      renderProviderOptions();
      if (state.result) renderResult();
    }, function () {
      state.providers = [];
      renderProviderOptions();
    });
  }

  function refreshProviders(toastFlag) {
    api().post('/api/prompt-reverse/providers/refresh', {}).then(function (data) {
      state.providers = (data && data.items) || [];
      state.defaultProvider = (data && data.default_provider) || AUTO_KEY;
      renderProviderOptions();
      renderProviderCards();
      if (toastFlag) ui().toastSuccess('已重新检测全部 Provider');
      if (AIBAR.app) AIBAR.app.refreshComfyStatus();
    }, function (err) {
      ui().toastError(ui().errorText(err, '重新检测失败'));
    });
  }

  function openProviderCenter() {
    var body = doc.createElement('div');
    body.className = 'filter-grid';
    body.id = 'provider-center';

    var head = ui().el('div', 'control-row');
    head.appendChild(ui().el('span', 'text-meta text-secondary', 'Provider 状态随 ComfyUI 与本地模型变化，可随时重新检测。'));
    var refresh = ui().el('button', 'btn btn-secondary btn-sm', '重新检测');
    refresh.type = 'button';
    refresh.addEventListener('click', function () { refreshProviders(true); });
    head.appendChild(refresh);
    body.appendChild(head);

    var list = ui().el('div', 'view');
    list.id = 'provider-list';
    body.appendChild(list);

    var entry = ui().drawer({
      title: 'Provider 中心',
      desc: '本地 Provider 零配置优先；外部 Provider 需逐次确认图片外发。',
      wide: true,
      body: body
    });
    state.providerDrawer = entry;
    renderProviderCards();
  }

  function renderProviderCards() {
    var list = doc.getElementById('provider-list');
    if (!list) return;
    list.innerHTML = '';

    if (!state.providers.length) {
      list.appendChild(ui().emptyState({
        icon: 'cpu',
        title: '没有可用的 Provider',
        desc: '可先启动 ComfyUI 让本地 BLIP 自动可用，或配置高级视觉 Provider。',
        actions: [
          { label: '启动 ComfyUI', variant: 'primary', onClick: startComfyUI },
          { label: '重新检测', variant: 'secondary', onClick: function () { refreshProviders(true); } }
        ]
      }));
      return;
    }

    state.providers.forEach(function (item) {
      list.appendChild(renderProviderCard(item));
    });
  }

  function startComfyUI() {
    api().post('/api/comfyui/start', {}).then(function (data) {
      ui().toast(data && data.started ? { message: 'ComfyUI 正在启动', type: 'success' }
        : { message: (data && data.message) || '未能启动 ComfyUI', type: 'warning' });
      if (AIBAR.app) AIBAR.app.refreshComfyStatus();
      refreshProviders(false);
    }, function (err) {
      ui().toastError(ui().errorText(err, '启动失败'));
    });
  }

  function renderProviderCard(item) {
    var status = formatProviderStatus(item.status);
    var card = ui().el('div', 'result-block');

    var head = ui().el('div', 'result-block-head');
    head.appendChild(ui().el('span', '', item.label || item.key));
    head.appendChild(ui().el('span', 'badge', item.local ? '本地' : '外部'));
    head.appendChild(ui().el('span', 'badge', qualityTierLabel(item.quality_tier)));
    head.appendChild(ui().el('span', 'grow'));
    var toneClass = status.tone === 'success' ? 'badge-success'
      : (status.tone === 'warning' ? 'badge-warning' : (status.tone === 'error' ? 'badge-error' : 'badge'));
    head.appendChild(ui().el('span', 'badge ' + toneClass, status.label));
    if (item.is_default) head.appendChild(ui().el('span', 'badge badge-brand', '默认'));
    card.appendChild(head);

    var meta = ui().el('div', 'entry-foot');
    meta.appendChild(ui().el('span', '', '标识：' + (item.key || '—')));
    meta.appendChild(ui().el('span', '', '模型：' + (item.model || '—')));
    if (item.quantization) meta.appendChild(ui().el('span', '', '量化：' + item.quantization));
    meta.appendChild(ui().el('span', '', '最近检测：' + ui().formatTime(item.last_checked_at)));
    meta.appendChild(ui().el('span', '', '状态：可用=' + (item.available ? '是' : '否')));
    card.appendChild(meta);

    var hint = ui().el('div', 'text-meta text-secondary', status.hint);
    card.appendChild(hint);
    if (item.reason) {
      card.appendChild(ui().el('div', 'text-meta text-tertiary break-any', '原因：' + item.reason));
    }

    var actions = ui().el('div', 'result-actions');

    var test = ui().el('button', 'btn btn-secondary btn-sm', '测试连接');
    test.type = 'button';
    test.addEventListener('click', function () {
      ui().setBusy(test, true);
      api().post('/api/prompt-reverse/providers/' + encodeURIComponent(item.key) + '/test', {})
        .then(function (data) {
          ui().setBusy(test, false);
          renderTestLayers(card, data && data.layers ? data.layers : []);
        }, function (err) {
          ui().setBusy(test, false);
          ui().toastError(ui().errorText(err, '测试失败'));
        });
    });
    actions.appendChild(test);

    if (item.is_default) {
      var unset = ui().el('button', 'btn btn-ghost btn-sm', '取消默认');
      unset.type = 'button';
      unset.addEventListener('click', function () { setDefault(AUTO_KEY); });
      actions.appendChild(unset);
    } else if (item.available) {
      var setDefaultBtn = ui().el('button', 'btn btn-ghost btn-sm', '设为默认');
      setDefaultBtn.type = 'button';
      setDefaultBtn.addEventListener('click', function () { setDefault(item.key); });
      actions.appendChild(setDefaultBtn);
    }

    if (status.action === 'start_comfyui') {
      var startBtn = ui().el('button', 'btn btn-secondary btn-sm', status.actionLabel);
      startBtn.type = 'button';
      startBtn.addEventListener('click', startComfyUI);
      actions.appendChild(startBtn);
    } else if (status.action === 'consent' && item.local === false) {
      var consentBtn = ui().el('button', 'btn btn-secondary btn-sm', status.actionLabel);
      consentBtn.type = 'button';
      consentBtn.addEventListener('click', function () {
        api().post('/api/prompt-reverse/providers/' + encodeURIComponent(item.key) + '/consent', { consented: true })
          .then(function () {
            ui().toastSuccess('已确认该外部 Provider 的图片外发');
            refreshProviders(false);
          }, function (err) {
            ui().toastError(ui().errorText(err, '确认失败'));
          });
      });
      actions.appendChild(consentBtn);
    }

    card.appendChild(actions);
    var layers = ui().el('div', 'kv-list');
    layers.className = 'kv-list test-layers';
    card.appendChild(layers);
    return card;
  }

  function renderTestLayers(card, layers) {
    var host = card.querySelector('.test-layers');
    if (!host) return;
    host.innerHTML = '';
    if (!layers.length) {
      host.appendChild(ui().el('div', 'text-meta text-tertiary', '没有返回分层结果'));
      return;
    }
    layers.forEach(function (layer) {
      var pending = layer.status === 'pending' || layer.reason_code === 'not_run';
      var status = pending
        ? { label: '未执行', tone: 'neutral' }
        : formatProviderStatus(layer.status);
      var row = ui().el('div', 'entry-foot');
      var label = layer.name === 'service' ? '服务可达'
        : (layer.name === 'model' ? '模型/节点可用'
          : (layer.name === 'inference' ? '最小视觉请求' : layer.name));
      row.appendChild(ui().el('span', 'badge', label));
      row.appendChild(ui().el('span', '', status.label));
      if (layer.latency_ms) row.appendChild(ui().el('span', '', layer.latency_ms + ' ms'));
      if (layer.reason_code && layer.reason_code !== 'not_run') {
        row.appendChild(ui().el('span', 'text-tertiary', layer.reason_code));
      }
      host.appendChild(row);
    });
  }

  function setDefault(key) {
    api().patch('/api/prompt-reverse/providers/default', { provider_key: key }).then(function (data) {
      state.defaultProvider = (data && data.default_provider) || key;
      loadProviders();
      renderProviderCards();
      ui().toastSuccess(key === AUTO_KEY ? '已恢复自动选择' : '已设为默认 Provider');
    }, function (err) {
      ui().toastError(ui().errorText(err, '设置失败'));
    });
  }

  /* ---------------------------------------------------------- 反推历史 */

  function loadHistory() {
    if (!refs.historyList) return;
    refs.historyList.innerHTML = '';
    refs.historyList.appendChild(ui().loadingInline('正在加载反推历史…'));
    api().get('/api/prompt-reverse/history', { limit: 10 }).then(function (data) {
      state.history = (data && data.items) || [];
      renderHistory();
    }, function (err) {
      refs.historyList.innerHTML = '';
      refs.historyList.appendChild(ui().errorState(ui().errorText(err, '历史加载失败'), loadHistory));
    });
  }

  function renderHistory() {
    refs.historyList.innerHTML = '';
    if (!state.history.length) {
      refs.historyList.appendChild(ui().emptyState({
        icon: 'list',
        title: '暂无反推记录',
        desc: '完成一次反推并保存后，记录会出现在这里，可复用或删除。'
      }));
      return;
    }
    state.history.forEach(function (item) {
      var node = ui().el('div', 'history-item');
      var head = ui().el('div', 'entry-foot');
      head.appendChild(ui().el('span', 'badge', item.source_type === 'metadata' ? '元数据恢复' : '视觉反推'));
      head.appendChild(ui().el('span', 'badge', item.provider || '—'));
      head.appendChild(ui().el('span', '', ui().formatTime(item.created_at)));
      if (item.stale) head.appendChild(ui().el('span', 'badge badge-warning', '已过期'));
      node.appendChild(head);
      node.appendChild(ui().el('div', 'text-meta text-tertiary',
        '片段 ' + (item.section_count || 0) + ' 项 · ' + (item.stage_label || item.stage || '')));

      var actions = ui().el('div', 'result-actions');
      var reuse = ui().el('button', 'btn btn-secondary btn-sm', '查看结果');
      reuse.type = 'button';
      reuse.addEventListener('click', function () {
        api().get('/api/prompt-reverse/jobs/' + encodeURIComponent(item.job_id), {}).then(function (data) {
          state.job = data;
          state.result = data;
          state.stale = !!data.stale;
          renderResult();
          setStep('result');
        }, function (err) {
          ui().toastError(ui().errorText(err, '读取结果失败'));
        });
      });
      actions.appendChild(reuse);

      var del = ui().el('button', 'btn btn-danger btn-sm');
      del.type = 'button';
      del.setAttribute('aria-label', '删除该反推记录');
      del.innerHTML = AIBAR.icons.get('trash', 14);
      del.addEventListener('click', function () {
        ui().confirm({
          title: '删除这条反推记录？',
          message: '删除后会清理不再被引用的上传缓存；已入库的词条不会被删除。',
          confirmLabel: '删除',
          danger: true,
          onConfirm: function () {
            api().del('/api/prompt-reverse/history/' + encodeURIComponent(item.job_id)).then(function () {
              ui().toastSuccess('已删除该反推记录');
              loadHistory();
            }, function (err) {
              ui().toastError(ui().errorText(err, '删除失败'));
            });
          }
        });
      });
      actions.appendChild(del);
      node.appendChild(actions);
      refs.historyList.appendChild(node);
    });
  }

  AIBAR.reverse = {
    init: init,
    onEnter: onEnter,
    onLeave: onLeave,
    loadGalleryImage: loadGalleryImage,
    openProviderCenter: openProviderCenter,
    getProviderCount: function () { return state.providers.length; }
  };

  /* 纯逻辑导出 */
  var logic = AIBAR.logic = AIBAR.logic || {};
  logic.formatProviderStatus = formatProviderStatus;
  logic.qualityTierLabel = qualityTierLabel;
  logic.STAGE_LABELS = STAGE_LABELS;
  logic.stageView = stageView;
  logic.isTerminalStatus = isTerminalStatus;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      formatProviderStatus: formatProviderStatus,
      qualityTierLabel: qualityTierLabel,
      STATUS_MAP: STATUS_MAP,
      STAGE_LABELS: STAGE_LABELS,
      STAGE_KEYS: STAGE_KEYS,
      TERMINAL_STATUSES: TERMINAL_STATUSES,
      isTerminalStatus: isTerminalStatus,
      stageView: stageView
    };
  }

  if (typeof window !== 'undefined') {
    window.AibarUtil = window.AibarUtil || {};
    window.AibarUtil.formatProviderStatus = formatProviderStatus;
    window.AibarUtil.qualityTierLabel = qualityTierLabel;
  }
})(typeof window !== 'undefined' ? window : globalThis);
