/* ============================================================
   AIBAR · 组图（M14）
   - 组图 = 同一人物的**连贯动作**序列，每一帧只有动作文本在变
   - 提示词由组图的强制预设统一拼装，前端不提供逐帧改提示词的入口
   - 出图：整组串行入队 + 进度轮询；单帧可单独重生成
   - 连播：拉取 playlist 全量预加载后逐帧切换，像 GIF 一样播放
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  var LIST_LIMIT = 60;

  var state = {
    keyword: '',
    poll: {}          // group_id → 是否正在轮询出图进度
  };

  function api() { return AIBAR.api; }
  function ui() { return AIBAR.ui; }
  // doc 在 SSR / Node 测试环境下是 null（groups.js 会被 tests/js 直接 require），
  // 这里必须空值短路，否则连「加载模块」都会抛异常。
  function host() { return doc ? doc.getElementById('groups-root') : null; }

  function icon(name, size) { return AIBAR.icons.get(name, size || 14); }

  function btn(variant, iconName, label, onClick, title) {
    var b = ui().el('button', 'btn btn-' + (variant || 'secondary') + ' btn-sm');
    b.type = 'button';
    if (iconName) {
      var i = ui().el('span', 'btn-icon');
      i.innerHTML = icon(iconName, 14);
      b.appendChild(i);
    }
    b.appendChild(ui().el('span', null, label));
    if (title) b.title = title;
    b.addEventListener('click', function (ev) { ev.stopPropagation(); onClick(); });
    return b;
  }

  /* ---------------------------------------------------------- 状态徽标 */

  var STATUS_META = {
    idle: { text: '待出图', tone: 'muted' },
    generating: { text: '出图中', tone: 'warning' },
    ready: { text: '已就绪', tone: 'success' },
    partial: { text: '部分完成', tone: 'warning' },
    failed: { text: '出图失败', tone: 'error' }
  };

  var FRAME_STATUS_META = {
    pending: { text: '待出', tone: 'muted' },
    generating: { text: '出图中', tone: 'warning' },
    done: { text: '已出', tone: 'success' },
    failed: { text: '失败', tone: 'error' }
  };

  function badge(text, tone) {
    var cls = 'badge';
    if (tone === 'success') cls += ' badge-success';
    else if (tone === 'warning') cls += ' badge-warning';
    else if (tone === 'error') cls += ' badge-error';
    return ui().el('span', cls, text);
  }

  function statusBadge(status) {
    var meta = STATUS_META[status] || STATUS_META.idle;
    return badge(meta.text, meta.tone);
  }

  function clampInt(v, fallback, lo, hi) {
    var n = parseInt(v, 10);
    if (isNaN(n)) n = fallback;
    return Math.max(lo, Math.min(hi, n));
  }

  /* ---------------------------------------------------------- 生命周期 */

  function init() { /* 组图无需预加载 */ }

  function onEnter(tab) {
    if (tab !== 'groups') return;
    if (!host()) return;
    if (!host().dataset.ready) {
      render();
      host().dataset.ready = '1';
    }
  }

  /* ---------------------------------------------------------- 列表 */

  function render() {
    var root_ = host();
    if (!root_) return;
    root_.innerHTML = '';

    var bar = ui().el('div', 'page-toolbar');
    var search = ui().el('div', 'search');
    var sIcon = ui().el('span', 'search-icon');
    sIcon.innerHTML = icon('search', 15);
    search.appendChild(sIcon);
    var input = ui().el('input', 'input');
    input.type = 'search';
    input.placeholder = '搜索组图名称或说明';
    input.setAttribute('aria-label', '搜索组图');
    input.value = state.keyword;
    input.addEventListener('input', function () {
      state.keyword = input.value;
      loadGroups(grid);
    });
    search.appendChild(input);
    bar.appendChild(search);

    bar.appendChild(ui().el('span', 'grow'));

    var newBtn = btn('primary', 'plus', '新建组图', function () { openCreate(); });
    bar.appendChild(newBtn);
    var reloadBtn = btn('secondary', 'refresh', '刷新', function () { loadGroups(grid); });
    bar.appendChild(reloadBtn);

    root_.appendChild(bar);

    var tip = ui().el('p', 'entry-text text-tertiary groups-tip');
    tip.textContent = '组图 = 同一人物的连贯动作序列：在组图上设定一次「强制预设提示词」，' +
      '每一帧只写一个动作，出图后点封面即可连播成动画。';
    root_.appendChild(tip);

    var grid = ui().el('div', 'card-grid card-grid--shots');
    root_.appendChild(grid);

    loadGroups(grid);
  }

  function loadGroups(grid) {
    if (!grid) return;
    grid.innerHTML = '';
    grid.appendChild(ui().loadingInline('加载组图…'));
    var qs = '?limit=' + LIST_LIMIT + '&offset=0';
    if (state.keyword) qs += '&keyword=' + encodeURIComponent(state.keyword);
    api().get('/api/comic/groups' + qs).then(function (data) {
      grid.innerHTML = '';
      var items = (data && data.items) || [];
      if (!items.length) {
        grid.appendChild(ui().emptyState({
          icon: 'film',
          title: state.keyword ? '没有匹配的组图' : '还没有组图',
          desc: state.keyword ? '换个关键词试试。' : '点「新建组图」，绑定一个演员并写好动作序列即可开始。'
        }));
        return;
      }
      items.forEach(function (g) {
        grid.appendChild(renderGroupCard(g));
        if (g.is_generating) startPolling(g.id, grid);
      });
    }, function (err) {
      grid.innerHTML = '';
      grid.appendChild(ui().errorState(ui().errorText(err, '加载组图失败'), function () { loadGroups(grid); }));
    });
  }

  function renderGroupCard(g) {
    var card = ui().el('article', 'entry-card shot-card');

    // 封面：点击即连播（组图的核心交互）
    var cover = ui().el('div', 'shot-cover');
    cover.setAttribute('role', 'button');
    cover.tabIndex = 0;
    cover.title = '点击连播';
    if (g.cover_url) {
      var img = ui().el('img', 'shot-cover-img');
      img.src = g.cover_url;
      img.alt = g.name || '组图封面';
      img.loading = 'lazy';
      cover.appendChild(img);
      var playVeil = ui().el('div', 'shot-play-veil');
      playVeil.innerHTML = icon('play', 28);
      cover.appendChild(playVeil);
    } else {
      cover.appendChild(ui().el('div', 'shot-cover-empty', '尚未出图'));
    }
    cover.addEventListener('click', function () { openPlayer(g); });
    cover.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openPlayer(g); }
    });
    card.appendChild(cover);

    var body = ui().el('div', 'entry-body');

    var head = ui().el('div', 'shot-card-head');
    head.appendChild(ui().el('h3', 'entry-title clamp-1', g.name || '未命名组图'));
    head.appendChild(statusBadge(g.status));
    body.appendChild(head);

    var meta = ui().el('div', 'shot-meta');
    meta.appendChild(badge('帧 ' + (g.done_count || 0) + '/' + (g.frame_count || 0), 'muted'));
    if (g.actor_id) meta.appendChild(badge('已绑演员', 'muted'));
    meta.appendChild(ui().el('span', 'shot-meta-span', '连播 ' + (g.frame_interval || 300) + 'ms/帧'));
    body.appendChild(meta);

    if (g.description) {
      body.appendChild(ui().el('p', 'entry-text clamp-2 text-tertiary', g.description));
    }

    // 组图卡的操作是主操作（连播/出图），**不能**沿用 .entry-actions 的 hover 才显示
    var actions = ui().el('div', 'shot-actions');
    actions.appendChild(btn('primary', 'play', '连播', function () { openPlayer(g); }));
    actions.appendChild(btn('secondary', 'cpu', '出图', function () { openGenerate(g); }, '只出「没出过 / 失败」的帧'));
    actions.appendChild(btn('secondary', 'sliders', '预设', function () { openEdit(g); }));
    actions.appendChild(btn('secondary', 'list', '动作帧', function () { openFrames(g); }));
    // 出图中才给「取消」：整组几十帧要跑好几分钟，没有出口等于把用户锁死在等待里
    if (g.is_generating) {
      actions.appendChild(btn('ghost', 'close', '取消出图', function () { cancelGenerate(g); }));
    }
    actions.appendChild(btn('ghost', 'trash', '删除', function () { removeGroup(g); }));
    body.appendChild(actions);

    card.appendChild(body);
    return card;
  }

  /* ---------------------------------------------------------- 进度轮询 */

  function startPolling(id, grid) {
    if (state.poll[id]) return;
    state.poll[id] = true;
    (function tick() {
      api().get('/api/comic/groups/' + id + '/progress').then(function (p) {
        if (p && p.running) {
          setTimeout(tick, 2000);
          return;
        }
        state.poll[id] = false;
        if (grid) loadGroups(grid);
        var failed = (p && p.failed) || 0;
        var done = (p && p.done) || 0;
        if (failed) ui().toast({ message: '出图完成：成功 ' + done + ' 帧，失败 ' + failed + ' 帧', type: 'warning' });
        else ui().toastSuccess('组图出图完成：' + done + ' 帧');
      }, function () {
        state.poll[id] = false;
      });
    })();
  }

  /* ---------------------------------------------------------- 表单零件 */

  function fieldRow(label, control, hint) {
    var wrap = ui().el('label', 'field');
    wrap.appendChild(ui().el('span', 'field-label', label));
    wrap.appendChild(control);
    if (hint) wrap.appendChild(ui().el('span', 'field-hint', hint));
    return wrap;
  }

  function textInput(value, placeholder) {
    var i = ui().el('input', 'input');
    i.type = 'text';
    i.value = value || '';
    if (placeholder) i.placeholder = placeholder;
    return i;
  }

  function textarea(value, rows, placeholder) {
    var t = ui().el('textarea', 'textarea');
    t.rows = rows || 3;
    t.value = value || '';
    if (placeholder) t.placeholder = placeholder;
    return t;
  }

  function checkbox(labelText, checked) {
    var wrap = ui().el('label', 'field field-inline');
    var c = ui().el('input');
    c.type = 'checkbox';
    c.checked = !!checked;
    c.style.cssText = 'width:16px;height:16px;margin-right:8px';
    wrap.appendChild(c);
    wrap.appendChild(ui().el('span', null, labelText));
    // **必须**叫 `.checkbox`，不能叫 `.control`：HTMLLabelElement 自身有个只读的
    // `.control` 属性（HTML5 标准），自定义同名属性会被静默忽略，
    // 导致后续读取 `wrap.control.checked` 永远 undefined。
    wrap.checkbox = c;
    return wrap;
  }

  function select(options, value) {
    var s = ui().el('select', 'select');
    (options || []).forEach(function (o) {
      var opt = ui().el('option', null, o.label);
      opt.value = o.value;
      if (String(o.value) === String(value)) opt.selected = true;
      s.appendChild(opt);
    });
    return s;
  }

  /* ---------------------------------------------------------- 新建 / 编辑组图 */

  function openCreate() {
    openGroupForm(null, function (payload) {
      api().post('/api/comic/groups', payload).then(function (g) {
        ui().toastSuccess('组图已创建，去填动作帧吧');
        var grid = host() && host().querySelector('.card-grid');
        loadGroups(grid);
        if (g && g.id) openFrames(g, true);
      }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
    });
  }

  function openEdit(g) {
    openGroupForm(g, function (payload) {
      api().patch('/api/comic/groups/' + g.id, payload).then(function () {
        ui().toastSuccess('预设已保存');
        loadGroups(host() && host().querySelector('.card-grid'));
      }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
    });
  }

  function openGroupForm(g, onSubmit) {
    var body = ui().el('div', 'form-stack');
    var isEdit = !!g;

    var nameInput = textInput(g && g.name, '如：少年拔刀三连');
    body.appendChild(fieldRow('组图名称', nameInput));

    var descInput = textarea(g && g.description, 2, '这组动作想表达什么（可选，仅用于列表里辨识）');
    body.appendChild(fieldRow('说明', descInput));

    // 演员下拉：绑定后人物锚点由演员定妆提供，是「同一张脸」的保证
    var actorSel = select([{ label: '（不绑定，用下方手填人物设定）', value: '' }]);
    body.appendChild(fieldRow('绑定演员', actorSel,
      '强烈建议绑定：演员定妆信息会作为人物锚点注入每一帧，这是连播时脸不漂的根本保障'));
    loadActorsInto(actorSel, g && g.actor_id);

    var anchorInput = textarea(g && g.anchor_override, 2,
      '补充本组图特有的人物设定，如「今天扎马尾、戴围巾」');
    body.appendChild(fieldRow('人物设定补充（可选）', anchorInput,
      '追加在演员锚点之后；不绑演员时它就是唯一的人物描述'));

    var prefixInput = textarea(g && g.preset_prefix, 2, '如：雨天的天台，黄昏，远景');
    body.appendChild(fieldRow('强制预设 · 场景 / 画风', prefixInput, '所有帧共用，逐帧改不了'));

    var suffixInput = textarea(g && g.preset_suffix, 2, '如：电影感侧光，浅景深，8k');
    body.appendChild(fieldRow('强制预设 · 镜头 / 画质', suffixInput, '所有帧共用，追加在动作之后'));

    var negInput = textarea(g && g.preset_negative, 2, '如：blurry, extra fingers, watermark');
    body.appendChild(fieldRow('强制预设 · 负面词', negInput, '反漂移词会自动追加，无需重复填写'));

    var adv = ui().el('div', 'form-grid-2');
    var seedInput = textInput(g && g.base_seed, '留空自动生成');
    adv.appendChild(fieldRow('基准种子', seedInput));
    var stepInput = textInput(g && g.seed_step != null ? g.seed_step : 0, '0');
    adv.appendChild(fieldRow('种子步长', stepInput,
      '0 = 所有帧同种子（脸最稳，动作幅度小）；调大则动作幅度更大但脸会略漂'));
    var intervalInput = textInput(g && g.frame_interval != null ? g.frame_interval : 300, '300');
    adv.appendChild(fieldRow('连播间隔（毫秒）', intervalInput));
    var loopWrap = checkbox('循环播放', g ? g.loop_play !== 0 : true);
    adv.appendChild(loopWrap);
    body.appendChild(adv);

    var refreshWrap = null;
    if (isEdit) {
      refreshWrap = checkbox('保存后按新预设重算全部帧提示词', true);
      body.appendChild(refreshWrap);
    }

    ui().modal({
      title: isEdit ? '编辑组图预设 · ' + (g.name || '') : '新建组图',
      desc: '提示词一律由这里的预设拼装：人物锚点 → 一致性指令 → 场景 → 动作 → 镜头。',
      body: body,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: isEdit ? '保存' : '创建',
          variant: 'primary',
          onClick: function () {
            var name = String(nameInput.value || '').trim();
            if (!isEdit && !name) name = '未命名组图';
            if (isEdit && !name) { ui().toastError('组图名称不能为空'); return false; }

            var payload = {
              name: name,
              description: String(descInput.value || '').trim(),
              actor_id: actorSel.value ? parseInt(actorSel.value, 10) : null,
              anchor_override: String(anchorInput.value || '').trim(),
              preset_prefix: String(prefixInput.value || '').trim(),
              preset_suffix: String(suffixInput.value || '').trim(),
              preset_negative: String(negInput.value || '').trim(),
              seed_step: clampInt(stepInput.value, 0, 0, 1000000),
              frame_interval: clampInt(intervalInput.value, 300, 40, 3000),
              loop_play: loopWrap.checkbox.checked
            };
            var seedRaw = String(seedInput.value || '').trim();
            if (seedRaw) payload.base_seed = clampInt(seedRaw, 1, 0, 2147483647);
            if (refreshWrap) payload.refresh = refreshWrap.control.checked;
            onSubmit(payload);
            return true;
          }
        }
      ]
    });
  }

  function loadActorsInto(sel, currentId) {
    api().get('/api/comic/actors?limit=200&offset=0').then(function (d) {
      var items = (d && d.items) || [];
      items.forEach(function (a) {
        var opt = ui().el('option', null, a.name + (a.aliases ? '（' + a.aliases + '）' : ''));
        opt.value = String(a.id);
        if (currentId && String(a.id) === String(currentId)) opt.selected = true;
        sel.appendChild(opt);
      });
    }, function () { /* 加载失败不影响表单：用户仍可手填人物设定 */ });
  }

  /* ---------------------------------------------------------- 动作帧编辑 */

  function openFrames(g, isNew) {
    // **默认逐帧卡片** 而不是多行文本框——文本框一次编辑 20 帧容易但定位"第三张表情不对"
    // 这种问题得肉眼扫整段，多帧卡片则一眼看到缩略图 + 状态 + 错误信息，把"哪帧需要重生成"
    // 这种高频操作的成本压到最低。多行文本只在「批量替换/添加」时短暂出现。
    var body = ui().el('div', 'shot-frames-modal');

    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '**逐帧管理**：每张卡显示缩略图、动作描述与状态，可独立改动作文本、上移/下移、' +
      '重生成或删除。提示词始终按组图预设自动重算（单帧改不了）；改预设后点「重算全部提示词」。';
    body.appendChild(tip);

    // 工具栏
    var toolbar = ui().el('div', 'shot-frames-toolbar');
    var addBtn = ui().el('button', 'btn btn-secondary btn-sm');
    addBtn.type = 'button';
    addBtn.appendChild(ui().el('span', 'btn-icon', ''));
    addBtn.appendChild(ui().el('span', null, '添加一帧'));
    addBtn.addEventListener('click', function () { appendFrame(); });

    var refreshPromptsBtn = ui().el('button', 'btn btn-ghost btn-sm');
    refreshPromptsBtn.type = 'button';
    refreshPromptsBtn.appendChild(ui().el('span', null, '重算全部提示词'));
    refreshPromptsBtn.title = '按组图当前预设覆盖全部帧的提示词（不改动作）';
    refreshPromptsBtn.addEventListener('click', function () { refreshAllPrompts(); });

    var bulkBtn = ui().el('button', 'btn btn-ghost btn-sm');
    bulkBtn.type = 'button';
    bulkBtn.appendChild(ui().el('span', null, '批量编辑…'));
    bulkBtn.addEventListener('click', function () { showBulkEditor(); });

    toolbar.appendChild(addBtn);
    toolbar.appendChild(refreshPromptsBtn);
    toolbar.appendChild(bulkBtn);
    body.appendChild(toolbar);

    // 逐帧卡片网格
    var grid = ui().el('div', 'shot-frames-grid');
    body.appendChild(grid);

    // 批量编辑区（默认隐藏）
    var bulk = ui().el('div', 'shot-bulk-wrap');
    bulk.hidden = true;
    var ta = textarea('', 14, '');
    bulk.appendChild(fieldRow('批量文本（一行一个动作）', ta));
    var bulkFooter = ui().el('div', 'shot-bulk-foot');
    var saveBulkBtn = ui().el('button', 'btn btn-secondary btn-sm');
    saveBulkBtn.type = 'button';
    saveBulkBtn.appendChild(ui().el('span', null, '覆盖保存'));
    saveBulkBtn.addEventListener('click', function () { saveBulk(); });
    var backBtn = ui().el('button', 'btn btn-ghost btn-sm');
    backBtn.type = 'button';
    backBtn.appendChild(ui().el('span', null, '返回逐帧视图'));
    backBtn.addEventListener('click', function () { showGrid(); });
    bulkFooter.appendChild(saveBulkBtn);
    bulkFooter.appendChild(backBtn);
    bulk.appendChild(bulkFooter);
    body.appendChild(bulk);

    ui().modal({
      title: '动作帧 · ' + (g.name || ''),
      desc: '每一帧只有动作在变，其余全部来自组图预设。',
      body: body,
      size: 'lg',
      actions: [{ label: '关闭', variant: 'ghost', onClick: function () { return true; } }]
    });

    var frames = [];

    function load() {
      api().get('/api/comic/groups/' + g.id).then(function (d) {
        frames = (d && d.frames) || [];
        renderGrid();
        ta.value = frames.map(function (f) { return f.action_text || ''; }).join('\n');
      }, function (err) { ui().toastError(ui().errorText(err, '加载帧失败')); });
    }

    function renderGrid() {
      grid.innerHTML = '';
      if (!frames.length) {
        grid.appendChild(ui().emptyState({
          icon: 'film',
          title: '还没有帧',
          desc: '点上方「添加一帧」开始，或「批量编辑」一次性贴入多行。'
        }));
        return;
      }
      frames.forEach(function (f, idx) {
        grid.appendChild(renderFrameCard(f, idx));
      });
    }

    function renderFrameCard(f, idx) {
      var card = ui().el('div', 'shot-frame-card');
      if (f.status === 'done') card.classList.add('is-done');
      if (f.status === 'failed') card.classList.add('is-failed');
      if (f.status === 'generating') card.classList.add('is-generating');

      var thumb = ui().el('div', 'shot-frame-thumb');
      if (f.image_url) {
        var img = ui().el('img', 'shot-frame-thumb-img');
        img.src = f.image_url;
        img.alt = f.action_text || ('帧 ' + (idx + 1));
        img.loading = 'lazy';
        thumb.appendChild(img);
      } else {
        thumb.appendChild(ui().el('span', 'shot-frame-thumb-empty', '#' + (idx + 1)));
      }
      card.appendChild(thumb);

      var meta = ui().el('div', 'shot-frame-meta');
      var head = ui().el('div', 'shot-frame-head');
      head.appendChild(badge('#' + (idx + 1), 'muted'));
      var meta2 = FRAME_STATUS_META[f.status];
      if (f.status && meta2) head.appendChild(badge(meta2.text, meta2.tone));
      meta.appendChild(head);

      // 点击动作文本 → 内联编辑（单帧独立更新，无需整组重算）
      var actionLabel = ui().el('button', 'shot-frame-action clamp-3');
      actionLabel.type = 'button';
      actionLabel.title = '点击修改动作（提示词自动按当前预设重算）';
      actionLabel.textContent = f.action_text || '（无动作描述）';
      actionLabel.addEventListener('click', function () { editActionInline(f, actionLabel); });
      meta.appendChild(actionLabel);

      if (f.error_message) {
        var err = ui().el('div', 'shot-frame-error');
        err.textContent = f.error_message;
        err.title = err.textContent;
        meta.appendChild(err);
      }

      var actions = ui().el('div', 'shot-frame-actions');
      actions.appendChild(makeMiniBtn('上移', function () { moveFrame(f.id, idx - 1); }, idx === 0));
      actions.appendChild(makeMiniBtn('下移', function () { moveFrame(f.id, idx + 1); }, idx === frames.length - 1));
      actions.appendChild(makeMiniBtn('重生成', function () { regenFrame(f.id, f); }));
      actions.appendChild(makeMiniBtn('删除', function () { deleteFrame(f.id); }, false, 'shot-frame-btn-danger'));
      meta.appendChild(actions);

      card.appendChild(meta);
      return card;
    }

    function makeMiniBtn(label, onClick, disabled, extraClass) {
      var cls = 'btn btn-ghost btn-sm shot-frame-btn' + (extraClass ? ' ' + extraClass : '');
      var b = ui().el('button', cls);
      b.type = 'button';
      b.appendChild(ui().el('span', null, label));
      if (disabled) {
        b.disabled = true;
      } else {
        b.addEventListener('click', onClick);
      }
      return b;
    }

    function editActionInline(frame, labelNode) {
      var editor = ui().el('textarea', 'textarea shot-frame-edit');
      editor.rows = 2;
      editor.value = frame.action_text || '';
      labelNode.replaceWith(editor);
      editor.focus();
      editor.select();

      function commit() {
        var newText = String(editor.value || '').trim();
        if (newText === (frame.action_text || '')) { editor.replaceWith(labelNode); return; }
        if (!newText) { ui().toastError('动作描述不能为空'); editor.replaceWith(labelNode); return; }
        api().patch('/api/comic/frames/' + frame.id, { action_text: newText }).then(function () {
          ui().toastSuccess('动作已更新（提示词已按当前预设重算）');
          load();
        }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
      }
      editor.addEventListener('blur', commit);
      editor.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); editor.blur(); }
        else if (e.key === 'Escape') { editor.replaceWith(labelNode); }
      });
    }

    function moveFrame(id, newIdx) {
      if (newIdx < 0 || newIdx >= frames.length) return;
      api().patch('/api/comic/frames/' + id, { order_idx: newIdx }).then(function () {
        load();
      }, function (err) { ui().toastError(ui().errorText(err, '调序失败')); });
    }

    function regenFrame(id, frame) {
      api().post('/api/comic/frames/' + id + '/regenerate', {}).then(function () {
        ui().toastSuccess('已入队重生成「' + (frame.action_text || '该帧') + '」');
        startPolling(g.id, host() && host().querySelector('.card-grid'));
        load();
      }, function (err) { ui().toastError(ui().errorText(err, '重生成失败')); });
    }

    function deleteFrame(id) {
      ui().confirm({
        title: '删除该帧',
        message: '确定删除该帧？对应的产出图会一并删除（其它帧不受影响）。',
        confirmLabel: '删除',
        danger: true
      }).then(function (yes) {
        if (!yes) return;
        api().del('/api/comic/frames/' + id).then(function () {
          ui().toastSuccess('帧已删除');
          load();
        }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
      });
    }

    function appendFrame() {
      // 添加一帧：把现有动作 + 占位串一起 PUT，最少请求数
      var actions = frames.map(function (f) { return f.action_text || ''; });
      actions.push('第' + (frames.length + 1) + '步');
      api().put('/api/comic/groups/' + g.id + '/frames', { actions: actions }).then(function () {
        ui().toastSuccess('已添加');
        load();
      }, function (err) { ui().toastError(ui().errorText(err, '添加失败')); });
    }

    function refreshAllPrompts() {
      api().post('/api/comic/groups/' + g.id + '/refresh-prompts', {}).then(function () {
        ui().toastSuccess('提示词已按当前预设重算（已出图的帧保持原图，需重出才会换）');
        load();
      }, function (err) { ui().toastError(ui().errorText(err, '重算失败')); });
    }

    function showBulkEditor() {
      ta.value = frames.map(function (f) { return f.action_text || ''; }).join('\n');
      grid.hidden = true;
      bulk.hidden = false;
      ta.focus();
    }

    function showGrid() {
      bulk.hidden = true;
      grid.hidden = false;
    }

    function saveBulk() {
      var actions = String(ta.value || '').split('\n')
        .map(function (s) { return s.trim(); })
        .filter(function (s) { return !!s; });
      if (!actions.length) { ui().toastError('至少写一个动作'); return; }
      api().put('/api/comic/groups/' + g.id + '/frames', { actions: actions }).then(function () {
        ui().toastSuccess('已保存 ' + actions.length + ' 帧');
        load();
        showGrid();
      }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
    }

    load();
  }

  /* ---------------------------------------------------------- 出图 */

  function openGenerate(g) {
    var body = ui().el('div', 'form-stack');
    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '整组**串行**出图（ComfyUI 单卡排队，并发只会互相抢显存）。' +
      '串行跑完一组需要几分钟，进度会在卡片上体现。';
    body.appendChild(tip);

    var onlyMissing = checkbox('只出「没出过 / 失败」的帧', true);
    body.appendChild(onlyMissing);

    ui().modal({
      title: '整组出图 · ' + (g.name || ''),
      desc: '共 ' + (g.frame_count || 0) + ' 帧，已出 ' + (g.done_count || 0) + ' 帧。',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '开始出图',
          variant: 'primary',
          onClick: function () {
            api().post('/api/comic/groups/' + g.id + '/generate',
              { only_missing: onlyMissing.control.checked }).then(function (d) {
                if (!d || !d.queued) { ui().toast({ message: '没有需要出的帧', type: 'info' }); return; }
                ui().toastSuccess('已入队 ' + d.queued + ' 帧，串行出图中…');
                startPolling(g.id, host() && host().querySelector('.card-grid'));
                loadGroups(host() && host().querySelector('.card-grid'));
              }, function (err) { ui().toastError(ui().errorText(err, '入队失败')); });
            return true;
          }
        }
      ]
    });
  }

  function cancelGenerate(g) {
    api().post('/api/comic/groups/' + g.id + '/cancel', {}).then(function () {
      ui().toast({ message: '已请求取消：当前这一帧出完后停止', type: 'info' });
      loadGroups(host() && host().querySelector('.card-grid'));
    }, function (err) { ui().toastError(ui().errorText(err, '取消失败')); });
  }

  /* ---------------------------------------------------------- 删除 */

  function removeGroup(g) {
    ui().confirm({
      title: '删除组图',
      message: '确定删除组图「' + (g.name || '未命名组图') + '」？其全部动作帧与产出图会一并删除。此操作不可恢复。',
      confirmLabel: '删除',
      danger: true
    }).then(function (yes) {
      if (!yes) return;
      api().del('/api/comic/groups/' + g.id).then(function () {
        ui().toastSuccess('组图已删除');
        loadGroups(host() && host().querySelector('.card-grid'));
      }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
    });
  }

  /* ---------------------------------------------------------- 连播播放器 */

  function openPlayer(g) {
    // 连播播放器抽成了通用组件（static/js/player.js），组图与 M15 视频序列帧
    // 共用一份——各写一份不仅重复，更糟的是两边会慢慢漂移：这边修了「速度档
    // 不匹配自定义 interval」的 bug，视频那边照样踩。
    api().get('/api/comic/groups/' + g.id + '/playlist').then(function (d) {
      var items = (d && d.items) || [];
      if (!items.length) {
        ui().toast({ message: '还没有已出图的帧：先填动作帧并出图，再回来连播', type: 'warning' });
        return;
      }
      AIBAR.player.open({
        title: d.name || (g && g.name) || '组图',
        items: items.map(function (it) { return { url: it.url, label: it.action || '' }; }),
        interval: d.interval,
        loop: d.loop
      });
    }, function (err) { ui().toastError(ui().errorText(err, '加载连播清单失败')); });
  }

  /* ---------------------------------------------------------- 导出 */

  AIBAR.groups = {
    init: init,
    onEnter: onEnter,
    refresh: function () { loadGroups(host() && host().querySelector('.card-grid')); },
    openPlayer: openPlayer,
    openFrames: openFrames
  };

  // 双环境导出：浏览器挂 window.AIBAR，Node 下供 tests/js 断言模块契约
  if (typeof module !== 'undefined' && module.exports) module.exports = AIBAR.groups;
})(typeof window !== 'undefined' ? window : globalThis);
