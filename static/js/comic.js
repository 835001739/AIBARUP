/* ============================================================
   AIBAR · 漫画工作室（M12）前端
   - 漫画概览：项目卡片列表（章节数 / 分镜数 / 完成数）
   - 章节管理：章节列表 + 每章节分镜（预制提示词 + 出图工作流）
   - 出图队列：与 ComfyUI 对接，单张 / 本章 / 全本入队，可取消
   - 大工作流完成后，对单张分镜重新生成图片
   全部走 /api/comic/*，统一用 AIBAR.api 解包，错误用 AIBAR.ui 提示。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  var _ready = false;
  var _pollTimer = null;
  var _progressBusy = false; // 进度接口是否报告仍有待出图 / 生成中的活儿
  var _jobsActive = false;   // 队列接口是否报告仍有 queued / running 任务

  var state = {
    view: 'overview', // overview | project | chapter
    projectId: null,
    chapterId: null,
    search: '',
    jobs: [],
    project: null, // 当前项目详情（局部刷新用）
    // 局部刷新用的挂载点（避免整页刷新打断正在填写的输入）
    chaptersHost: null,
    jobsHost: null,
    charactersHost: null,
    progressHost: null
  };

  function api() { return AIBAR.api; }
  function ui() { return AIBAR.ui; }
  function host() { return doc.getElementById('comic-root'); }

  /* ---------------------------------------------------------- 小工具 */

  function icon(name, size) {
    var span = ui().el('span', 'btn-icon');
    span.innerHTML = AIBAR.icons.get(name, size || 16);
    return span;
  }

  function btn(variant, iconName, label, onClick) {
    var b = ui().el('button', 'btn btn-' + (variant || 'secondary') + ' btn-sm');
    b.type = 'button';
    if (iconName) b.appendChild(icon(iconName, 14));
    if (label) b.appendChild(ui().el('span', null, label));
    if (onClick) {
      b.addEventListener('click', onClick);
    } else {
      // 没有回调的按钮（如「出图中…」）必须禁用：否则外观可点、点了却毫无反应
      b.disabled = true;
      b.style.opacity = '0.55';
      b.style.cursor = 'default';
    }
    return b;
  }

  function imageUrl(path) {
    if (!path) return '';
    var rel = String(path).replace(/^comic_outputs\//, '');
    return '/api/comic/output/' + rel;
  }

  function statusBadge(status) {
    var map = {
      draft: ['badge', '草稿'],
      production: ['badge badge-warning', '制作中'],
      done: ['badge badge-success', '已完成'],
      pending: ['badge', '待出图'],
      queued: ['badge badge-brand', '排队中'],
      generating: ['badge badge-warning', '出图中'],
      running: ['badge badge-warning', '运行中'],
      done_p: ['badge badge-success', '已完成'],
      failed: ['badge badge-error', '失败'],
      cancelled: ['badge', '已取消']
    };
    var m = map[status] || ['badge', status || '—'];
    return ui().el('span', m[0], m[1]);
  }

  function metaBadge(text) {
    return ui().el('span', 'badge', text);
  }

  /* ============================================================
     渲染入口
     ============================================================ */

  function render() {
    if (state.view === 'project') renderProject();
    else if (state.view === 'chapter') renderChapter();
    else renderOverview();
  }

  function onEnter(tab) {
    clearPoll();
    if (tab !== 'comic') return;
    if (!_ready) _ready = true;
    // 回到上次所在的位置，而不是一律踢回概览：
    // 用户常常在「章节 → 分镜」里切到别的 tab 看一眼参考图，切回来不该重新钻两层。
    try {
      render();
    } catch (err) {
      state.view = 'overview';
      state.project = null;
      state.chapter = null;
      renderOverview();
    }
  }

  function init() {
    _ready = true;
    loadErrorCodes();
  }

  /* ---------------------------------------------------------- 概览 */

  function renderOverview() {
    clearPoll();
    var h = host();
    if (!h) return;
    h.innerHTML = '';

    var toolbar = ui().el('div', 'page-toolbar');
    var search = ui().el('div', 'search');
    search.appendChild(icon('search', 15));
    var input = ui().el('input', 'input');
    input.type = 'search';
    input.placeholder = '搜索漫画名称或描述';
    input.setAttribute('aria-label', '搜索漫画');
    input.value = state.search;
    input.addEventListener('input', function () {
      state.search = input.value.trim();
      loadProjects(grid);
    });
    search.appendChild(input);
    toolbar.appendChild(search);
    toolbar.appendChild(ui().el('span', 'grow'));
    toolbar.appendChild(btn('ghost', 'import', '导入 Skill 工作流', openImportSkill));
    toolbar.appendChild(btn('primary', 'plus', '新建漫画', openCreateProject));
    h.appendChild(toolbar);

    var grid = ui().el('div', 'card-grid');
    h.appendChild(grid);
    loadProjects(grid);
  }

  function loadProjects(grid) {
    grid.innerHTML = '';
    grid.appendChild(ui().loadingInline('加载漫画列表…'));
    api().get('/api/comic/projects').then(function (data) {
      var items = (data && data.items) || [];
      if (state.search) {
        var q = state.search.toLowerCase();
        items = items.filter(function (p) {
          return (p.name || '').toLowerCase().indexOf(q) >= 0 ||
            (p.description || '').toLowerCase().indexOf(q) >= 0;
        });
      }
      renderProjectGrid(grid, items);
    }, function (err) {
      grid.innerHTML = '';
      grid.appendChild(ui().errorState(ui().errorText(err, '加载漫画失败'), function () { loadProjects(grid); }));
    });
  }

  function renderProjectGrid(grid, items) {
    grid.innerHTML = '';
    if (!items.length) {
      grid.appendChild(ui().emptyState({
        icon: 'book',
        title: '还没有漫画',
        desc: '创建第一部漫画，开始维护你的分镜大工作流。',
        actions: [{ label: '新建漫画', variant: 'primary', onClick: openCreateProject }]
      }));
      return;
    }
    items.forEach(function (p) {
      grid.appendChild(renderProjectCard(p));
    });
  }

  function renderProjectCard(p) {
    var card = ui().el('article', 'wf-card');
    card.tabIndex = 0;
    card.style.cursor = 'pointer';
    card.setAttribute('aria-label', '打开漫画：' + (p.name || ''));

    var title = ui().el('h4', 'wf-name', p.name || '未命名漫画');
    card.appendChild(title);
    if (p.status) { var st = statusBadge(p.status); st.style.marginLeft = '8px'; title.appendChild(st); }

    if (p.description) card.appendChild(ui().el('p', 'wf-prompt clamp-2', p.description));

    var meta = ui().el('div', 'result-actions');
    meta.appendChild(metaBadge('章节 ' + (p.chapter_count || 0)));
    meta.appendChild(metaBadge('分镜 ' + (p.page_count || 0)));
    meta.appendChild(metaBadge('完成 ' + (p.done_count || 0)));
    card.appendChild(meta);

    if (p.default_workflow) {
      card.appendChild(ui().el('span', 'badge', '默认工作流：' + p.default_workflow));
    }

    var actions = ui().el('div', 'result-actions');
    actions.appendChild(btn('primary', 'arrowRight', '打开', function (ev) { ev.stopPropagation(); openProject(p.id); }));
    actions.appendChild(btn('ghost', 'edit', '编辑', function (ev) { ev.stopPropagation(); openEditProject(p); }));
    actions.appendChild(btn('danger', 'trash', '删除', function (ev) {
      ev.stopPropagation();
      deleteProject(p);
    }));
    card.appendChild(actions);

    card.addEventListener('click', function () { openProject(p.id); });
    card.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openProject(p.id); }
    });
    return card;
  }

  /* ---------------------------------------------------------- 项目详情 */

  function openProject(id) {
    state.view = 'project';
    state.projectId = id;
    state.chapterId = null;
    renderProject();
  }

  function renderProject() {
    clearPoll();
    var h = host();
    if (!h) return;
    h.innerHTML = '';

    api().get('/api/comic/projects/' + state.projectId).then(function (project) {
      renderProjectShell(h, project);
    }, function (err) {
      h.appendChild(ui().errorState(ui().errorText(err, '加载漫画失败'), function () { renderProject(); }));
    });
  }

  function renderProjectShell(h, project) {
    h.innerHTML = '';
    state.project = project;
    h.appendChild(breadcrumb([
      { label: '漫画概览', onClick: renderOverview },
      { label: project.name || '未命名漫画' }
    ]));

    // 元信息卡
    var meta = ui().el('div', 'callout callout-info');
    meta.style.margin = '0 0 14px';
    meta.appendChild(ui().el('div', 'break-any', (project.description || '暂无描述')));
    var m2 = ui().el('div', 'result-actions');
    m2.style.marginTop = '8px';
    if (project.default_workflow) m2.appendChild(metaBadge('默认工作流：' + project.default_workflow));
    m2.appendChild(metaBadge('分镜 ' + (project.page_count || 0) + ' / 完成 ' + (project.done_count || 0)));
    meta.appendChild(m2);
    h.appendChild(meta);

    // 分镜工作流面板：可编辑世界观 / 剧情摘要，一键自动分集出图
    h.appendChild(renderStoryboardPanel(project));

    // 角色一致性面板：角色卡（人物锚点），保证同一个角色跨页长相一致
    var charHost = ui().el('div', 'entry-list');
    state.charactersHost = charHost;
    h.appendChild(sectionTitle('角色一致性（人物锚点）'));
    h.appendChild(charHost);
    loadCharacters(charHost, project);

    // 出图进度（实时轮询，只刷新这一块，不打断正在编辑的输入框）
    var progressHost = ui().el('div');
    state.progressHost = progressHost;
    h.appendChild(progressHost);
    loadProgress(progressHost, project);

    // 工具栏
    var toolbar = ui().el('div', 'page-toolbar');
    toolbar.appendChild(btn('primary', 'plus', '新建章节', function () { openCreateChapter(project); }));
    toolbar.appendChild(btn('secondary', 'play', '出图全本', function (ev) { generateProject(project, ev.currentTarget); }));
    toolbar.appendChild(btn('ghost', 'filter', '按条件重出', function (ev) { generateProjectFiltered(project, ev.currentTarget); }));
    toolbar.appendChild(btn('ghost', 'film', '自动分镜', function () { generateStoryboardSaved(project); }));
    toolbar.appendChild(btn('ghost', 'activity', '出图体检', function () { openPrecheck(project); }));
    toolbar.appendChild(btn('ghost', 'edit', '编辑漫画', function () { openEditProject(project); }));
    toolbar.appendChild(btn('danger', 'trash', '删除漫画', function () { deleteProject(project); }));
    toolbar.appendChild(ui().el('span', 'grow'));
    h.appendChild(toolbar);

    // 章节列表
    var chapterHost = ui().el('div', 'entry-list');
    state.chaptersHost = chapterHost;
    h.appendChild(sectionTitle('章节管理'));
    h.appendChild(chapterHost);
    loadChapters(chapterHost, project);

    // 队列
    h.appendChild(sectionTitle('出图队列'));
    var jobHost = ui().el('div', 'table-wrap');
    state.jobsHost = jobHost;
    h.appendChild(jobHost);
    loadJobs(jobHost, project.id);
  }

  /* ---------------------------------------------------------- 出图进度（实时） */

  function loadProgress(hostNode, project) {
    if (!hostNode) return;
    api().get('/api/comic/projects/' + project.id + '/progress').then(function (data) {
      renderProgress(hostNode, data);
      _progressBusy = progressBusy(data);
      scheduleLive(project);
    }, function () { /* 进度拉取失败不打扰用户，下一轮轮询自愈 */ });
  }

  function renderProgress(hostNode, p) {
    hostNode.innerHTML = '';
    var counts = (p && p.counts) || {};
    var total = (p && p.total) || 0;
    var percent = (p && p.percent) || 0;
    var online = !!(p && p.comfyui && p.comfyui.online);

    var box = ui().el('div', 'callout');
    box.style.display = 'block';
    box.style.margin = '0 0 14px';
    box.style.borderColor = counts.failed ? 'rgba(220,90,90,.35)' : 'rgba(108,114,255,.30)';
    box.style.background = 'var(--color-brand-soft)';

    var head = ui().el('div', 'control-row');
    head.style.margin = '0 0 8px';
    var t = ui().el('h3', 'text-section');
    t.style.margin = '0';
    t.appendChild(icon('activity', 18));
    t.appendChild(ui().el('span', null, '出图进度'));
    head.appendChild(t);
    head.appendChild(ui().el('span', 'grow'));
    head.appendChild(ui().el('span', 'badge', (p.done || 0) + ' / ' + total + '（' + percent + '%）'));
    head.appendChild(online
      ? ui().el('span', 'badge badge-success', 'ComfyUI 在线')
      : ui().el('span', 'badge badge-error', 'ComfyUI 离线'));
    box.appendChild(head);

    // 进度条
    var track = ui().el('div');
    track.style.cssText = 'height:8px;border-radius:999px;background:rgba(128,128,128,.20);overflow:hidden;margin:0 0 10px';
    var fill = ui().el('div');
    fill.style.cssText = 'height:100%;width:' + percent + '%;background:linear-gradient(90deg,#6c72ff,#9b7bff);transition:width .4s ease';
    track.appendChild(fill);
    box.appendChild(track);

    var row = ui().el('div', 'result-actions');
    row.appendChild(metaBadge('待出图 ' + (counts.pending || 0)));
    row.appendChild(metaBadge('排队 ' + (counts.queued || 0)));
    row.appendChild(metaBadge('生成中 ' + (counts.generating || 0)));
    row.appendChild(metaBadge('已完成 ' + (counts.done || 0)));
    if (counts.failed) row.appendChild(ui().el('span', 'badge badge-error', '失败 ' + counts.failed));
    if (p.queued_jobs) row.appendChild(metaBadge('队列任务 ' + p.queued_jobs));
    if (p.characters) row.appendChild(metaBadge('角色卡 ' + p.characters));
    box.appendChild(row);

    // 章节级进度：每一集一条细进度条，一眼看出哪一集还没跑完
    var chapters = (p && p.chapters) || [];
    if (chapters.length) {
      var chTitle = ui().el('p', 'text-section');
      chTitle.style.margin = '10px 0 6px';
      chTitle.textContent = '分集进度';
      box.appendChild(chTitle);
      chapters.forEach(function (c) {
        var line = ui().el('div', 'control-row');
        line.style.margin = '0 0 4px';
        var nm = ui().el('span', 'entry-text clamp-1', c.title || ('第 ' + c.chapter_id + ' 章'));
        nm.style.flex = '0 0 42%';
        nm.style.margin = '0';
        line.appendChild(nm);
        var tr = ui().el('div');
        tr.style.cssText = 'flex:1;height:6px;border-radius:999px;background:rgba(128,128,128,.20);overflow:hidden;align-self:center';
        var fl = ui().el('div');
        fl.style.cssText = 'height:100%;width:' + (c.percent || 0) + '%;background:linear-gradient(90deg,#6c72ff,#9b7bff);transition:width .4s ease';
        tr.appendChild(fl);
        line.appendChild(tr);
        var num = ui().el('span', 'text-tertiary', (c.done || 0) + '/' + (c.total || 0));
        num.style.flex = '0 0 auto';
        num.style.fontSize = '12px';
        line.appendChild(num);
        box.appendChild(line);
      });
    }

    if (p.active) {
      var act = ui().el('p', 'entry-text');
      act.style.margin = '8px 0 0';
      act.textContent = '正在出图：' + (p.active.page_title || ('分镜 #' + p.active.page_id)) +
        '（阶段：' + (p.active.stage || '运行中') + '）' +
        (p.active.attempt ? '，第 ' + (p.active.attempt + 1) + ' 次尝试' : '');
      box.appendChild(act);
    }
    // 预计剩余时间：只在真的还有活儿在跑时展示，避免空闲时显示无意义的 0
    if (p.eta_seconds && (p.queued_jobs || p.active)) {
      var eta = ui().el('p', 'entry-text text-tertiary');
      eta.style.margin = '4px 0 0';
      eta.textContent = '预计还需 ' + _formatEta(p.eta_seconds) +
        '（按最近出图均速 ' + _formatEta(p.avg_job_seconds || 0) + '/张估算）';
      box.appendChild(eta);
    }
    if (p.last_error) {
      var err = ui().el('p', 'entry-text');
      err.style.margin = '6px 0 0';
      err.style.color = 'var(--color-state-error)';
      err.textContent = '最近失败：' +
        (errorLabel(p.last_error.error_code, p.last_error.error_message) || '未知原因') +
        (p.last_error.page_id ? '（分镜 #' + p.last_error.page_id + '）' : '');
      box.appendChild(err);
    }
    var reasons = (p && p.failure_reasons) || [];
    if (reasons.length) {
      var rRow = ui().el('div', 'result-actions');
      rRow.style.marginTop = '6px';
      reasons.forEach(function (r) {
        rRow.appendChild(ui().el('span', 'badge badge-error', r.code + ' × ' + r.count));
      });
      box.appendChild(rRow);
    }
    if (!online) {
      var warn = ui().el('p', 'entry-text text-tertiary');
      warn.style.margin = '6px 0 0';
      warn.textContent = 'ComfyUI 未启动：已入队的分镜会自动重试（最多 3 次），启动后接着跑。';
      box.appendChild(warn);
    }

    // 队列 worker 不存活时明确告知：否则页面会永远停在 queued 而用户以为「只是慢」
    var worker = p && p.worker;
    if (worker && worker.alive === false) {
      var wErr = ui().el('p', 'entry-text');
      wErr.style.margin = '6px 0 0';
      wErr.style.color = 'var(--color-state-error)';
      wErr.textContent = '出图队列未在运行，任务不会被处理。请重启 AIBAR 服务。';
      box.appendChild(wErr);
    }

    // 卡死任务的回收入口：进程被杀会留下永远 running 的任务，页面又拒绝重复入队，
    // 不给个入口的话用户只能去改数据库。
    var stale = p && p.stale;
    if (stale && ((stale.stale_jobs || 0) + (stale.reset_pages || 0)) > 0) {
      var sRow = ui().el('div', 'result-actions');
      sRow.style.marginTop = '8px';
      var sTip = ui().el('p', 'entry-text text-tertiary');
      sTip.style.margin = '0';
      sTip.textContent = '检测到 ' + (stale.stale_jobs || 0) + ' 个卡住的任务、' +
        (stale.reset_pages || 0) + ' 张卡住的分镜（上次进程退出时中断）。';
      sRow.appendChild(sTip);
      sRow.appendChild(btn('warning', 'refresh', '重置并重新出图', function (ev) {
        reapStale(ev.currentTarget, true);
      }));
      sRow.appendChild(btn('ghost', 'undo', '仅重置', function (ev) {
        reapStale(ev.currentTarget, false);
      }));
      box.appendChild(sRow);
    }

    hostNode.appendChild(box);
  }

  /** 回收卡死的出图任务（上次进程被杀留下的 running / generating）。 */
  function reapStale(node, requeue) {
    var req = api().post('/api/comic/maintenance/reap', { requeue: !!requeue });
    return ui().withBusy(node, '处理中…', req).then(function (data) {
      var n = (data && (data.stale_jobs || 0) + (data.reset_pages || 0)) || 0;
      if (!n) {
        ui().toastSuccess('没有卡住的任务');
        return;
      }
      ui().toastSuccess(requeue
        ? ('已重置 ' + (data.reset_pages || 0) + ' 张分镜，其中 ' + (data.requeued || 0) + ' 张已重新入队')
        : ('已重置 ' + (data.reset_pages || 0) + ' 张分镜，可重新出图'));
      refreshCurrent();
    }, function (err) {
      ui().toastError(ui().errorText(err, '重置失败'));
    });
  }

  /** 秒 → 中文时长（出图动辄几十秒到几分钟，ui 自带的毫秒版在这里不够直观）。 */
  function _formatEta(seconds) {
    var s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 60) return s + ' 秒';
    var m = Math.floor(s / 60);
    var rest = s % 60;
    if (m < 60) return rest ? (m + ' 分 ' + rest + ' 秒') : (m + ' 分');
    var h = Math.floor(m / 60);
    return h + ' 小时 ' + (m % 60) + ' 分';
  }

  /* ---------------------------------------------------------- 错误码中文化

     出图失败时库里存的是机器码（comfyui_offline / no_output / …）。
     直接把这些英文码渲染给用户，等于让用户自己去猜「为什么失败、该怎么办」。
     映射表以后端 diagnostics.ERROR_TEXT 为唯一数据源，这里拉一次缓存起来。 */

  var _errorCodeMap = null;

  function loadErrorCodes() {
    if (_errorCodeMap) return;
    api().get('/api/comic/error-codes').then(function (items) {
      _errorCodeMap = items || {};
    }, function () {
      _errorCodeMap = {}; // 拉不到就退回「显示原始文案」，绝不阻塞界面
    });
  }

  /** 优先用后端给的可读文案；没文案时按错误码翻译成中文；都没有就原样返回。 */
  function errorLabel(code, message) {
    var msg = String(message || '').trim();
    if (msg) return msg;
    var key = String(code || '').trim();
    if (!key) return '';
    if (_errorCodeMap && _errorCodeMap[key]) return _errorCodeMap[key];
    return key;
  }

  function progressBusy(p) {
    var counts = (p && p.counts) || {};
    return (counts.queued || 0) + (counts.generating || 0) > 0 || ((p && p.queued_jobs) || 0) > 0;
  }

  /** 统一轮询调度：进度与队列只要还有活儿在跑，3 秒后轻量刷新一次；否则停止轮询。 */
  function scheduleLive(project) {
    clearTimeout(_pollTimer);
    _pollTimer = null;
    if (state.view !== 'project' || !project) return;
    if (AIBAR.app.getTab() !== 'comic') return;
    if (!_progressBusy && !_jobsActive) return;
    _pollTimer = setTimeout(function () { refreshLive(project); }, 3000);
  }

  /** 轻量刷新：只重绘进度 / 章节 / 队列，不重建整页，也不闪 spinner（避免刷新抖动打断正在编辑的输入框）。 */
  function refreshLive(project) {
    if (state.view !== 'project' || !project) return;
    loadProgress(state.progressHost, project);
    if (state.chaptersHost) loadChapters(state.chaptersHost, project, true);
    if (state.jobsHost) loadJobs(state.jobsHost, project.id, true);
  }

  /** 出图体检弹窗：ComfyUI 在线？工作流在不在？节点装没装？队列堵不堵？ */
  function openPrecheck(project) {
    var body = ui().el('div');
    body.appendChild(ui().loadingInline('正在体检…'));
    ui().modal({
      title: '出图体检',
      desc: '检查 ComfyUI 连通性、工作流与节点可用性',
      body: body,
      size: 'lg',
      actions: [{ label: '关闭', variant: 'ghost', onClick: function () { return true; } }]
    });
    api().get('/api/comic/projects/' + project.id + '/precheck').then(function (d) {
      body.innerHTML = '';
      var comfy = d.comfyui || {};
      var wf = d.workflow || {};
      var q = d.queue || {};
      var lines = [
        ['体检结论', d.ready ? '可以出图' : '暂不具备出图条件'],
        ['ComfyUI', (comfy.online ? '在线（' + (comfy.latency_ms || 0) + 'ms）' : '离线') + ' · ' + (comfy.base_url || '')],
        ['工作流', (wf.filename || '未设置') + (wf.exists ? '（存在，' + (wf.node_count || 0) + ' 个节点）' : '（文件缺失）')],
        ['节点校验', wf.nodes_checked
          ? (wf.missing_nodes && wf.missing_nodes.length ? '缺少：' + wf.missing_nodes.join('、') : '全部已安装')
          : '未校验（ComfyUI 离线或未拉取节点清单）'],
        ['队列', '执行中 ' + (q.running === -1 ? '未知' : q.running) + ' / 等待 ' + (q.pending === -1 ? '未知' : q.pending)]
      ];
      lines.forEach(function (kv) {
        var row = ui().el('div', 'control-row');
        row.style.margin = '0 0 6px';
        row.appendChild(ui().el('span', 'field-label', kv[0]));
        row.appendChild(ui().el('span', 'break-any', kv[1]));
        body.appendChild(row);
      });
      var hintTitle = ui().el('p', 'text-section');
      hintTitle.style.margin = '12px 0 6px';
      hintTitle.textContent = '建议';
      body.appendChild(hintTitle);
      (d.hints || []).forEach(function (h) {
        var li = ui().el('p', 'entry-text');
        li.style.margin = '0 0 4px';
        li.textContent = '· ' + h;
        body.appendChild(li);
      });
    }, function (err) {
      body.innerHTML = '';
      body.appendChild(ui().errorState(ui().errorText(err, '体检失败'), function () { openPrecheck(project); }));
    });
  }

  /* ---------------------------------------------------------- 角色卡（人物一致性） */

  function loadCharacters(hostNode, project) {
    hostNode.innerHTML = '';
    hostNode.appendChild(ui().loadingInline('加载角色卡…'));
    api().get('/api/comic/projects/' + project.id + '/characters').then(function (data) {
      renderCharacterList(hostNode, project, (data && data.items) || []);
    }, function (err) {
      hostNode.innerHTML = '';
      hostNode.appendChild(ui().errorState(ui().errorText(err, '加载角色卡失败'), function () { loadCharacters(hostNode, project); }));
    });
  }

  function renderCharacterList(hostNode, project, items) {
    hostNode.innerHTML = '';
    var bar = ui().el('div', 'page-toolbar');
    bar.appendChild(btn('primary', 'plus', '新建角色', function () { openCreateCharacter(project); }));
    bar.appendChild(btn('primary', 'image', '导入角色', function () { openImportCharacter(project); }));
    bar.appendChild(btn('primary', 'users', '从演员库导入', function () {
      if (AIBAR.actors && AIBAR.actors.openImportFromActor) AIBAR.actors.openImportFromActor(project);
    }));
    bar.appendChild(btn('secondary', 'sparkles', '从剧本抽取', function () { extractCharacters(project); }));
    bar.appendChild(ui().el('span', 'grow'));
    bar.appendChild(btn('ghost', 'refresh', '重刷提示词', function () { refreshPrompts(project); }));
    bar.appendChild(ui().el('span', 'badge', '角色锚点会逐字注入到每一页提示词'));
    hostNode.appendChild(bar);

    if (!items.length) {
      hostNode.appendChild(ui().emptyState({
        icon: 'users',
        title: '还没有角色卡',
        desc: '点「从剧本抽取」自动识别世界观 / 剧情里的人物，或手动新建。填好外貌 / 服装 / 配色后，人物跨页会更稳定。',
        actions: [{ label: '从剧本抽取', variant: 'primary', onClick: function () { extractCharacters(project); } }]
      }));
      return;
    }
    items.forEach(function (c) { hostNode.appendChild(renderCharacterCard(project, c)); });
  }

  function renderCharacterCard(project, c) {
    var card = ui().el('article', 'entry-card');
    if (c.image_url) {
      // 左图右文：样板图占固定画框完整显示，右侧放文字与操作
      card.classList.add('entry-card--split');
      var sample = ui().el('img', 'entry-thumb');
      sample.src = c.image_url;
      sample.alt = (c.name || '角色') + ' 样板图';
      sample.loading = 'lazy';
      card.appendChild(sample);
    }
    var body = ui().el('div', 'entry-body');
    var head = ui().el('div', 'entry-head');
    head.appendChild(ui().el('h4', 'entry-title', c.name || '未命名角色'));
    // 主角：锚点会注入到该项目**每一页**（哪怕这一页没提到他的名字），保证整本长相一致
    if (charIsMain(c)) head.appendChild(ui().el('span', 'badge badge-brand', '主角 · 贯穿全本'));
    if (c.appearance || c.outfit || c.palette) {
      head.appendChild(ui().el('span', 'badge badge-success', '锚点已完善'));
    } else {
      head.appendChild(ui().el('span', 'badge badge-warning', '待补充外貌'));
    }
    body.appendChild(head);

    var parts = [];
    if (c.aliases) parts.push('别名：' + c.aliases);
    if (c.appearance) parts.push('外貌：' + c.appearance);
    if (c.outfit) parts.push('服装：' + c.outfit);
    if (c.palette) parts.push('配色：' + c.palette);
    if (c.negative) parts.push('排除：' + c.negative);
    if (!parts.length) parts.push('（暂无描述，锚点只含角色名）');
    parts.forEach(function (t) {
      body.appendChild(ui().el('p', 'entry-text clamp-2', t));
    });
    if (c.anchor) body.appendChild(ui().el('p', 'entry-text clamp-1 text-tertiary', '锚点：' + c.anchor));

    var foot = ui().el('div', 'entry-foot');
    foot.appendChild(metaBadge('种子偏移：' + (c.seed_offset || 0)));
    body.appendChild(foot);

    var actions = ui().el('div', 'entry-actions');
    actions.appendChild(btn('ghost', 'edit', '编辑', function () { openEditCharacter(project, c); }));
    actions.appendChild(btn(
      charIsMain(c) ? 'ghost' : 'secondary',
      charIsMain(c) ? 'star' : 'star',
      charIsMain(c) ? '取消主角' : '设为主角',
      function () { toggleMain(project, c); }
    ));
    actions.appendChild(btn('danger', 'trash', '删除', function () { deleteCharacter(project, c); }));
    body.appendChild(actions);
    card.appendChild(body);
    return card;
  }

  /** 角色是否为主角：后端存 0/1，历史数据可能是 "1"/"true"，这里宽容解析。 */
  function charIsMain(c) {
    if (!c) return false;
    var v = c.is_main;
    if (v === true || v === 1) return true;
    return String(v) === '1' || String(v) === 'true';
  }

  function toggleMain(project, c) {
    var next = !charIsMain(c);
    api().patch('/api/comic/characters/' + c.id, { is_main: next }).then(function () {
      ui().toastSuccess(next ? '已设为主角：锚点会注入到每一页' : '已取消主角：改为按剧本提及注入');
      loadCharacters(state.charactersHost, project);
    }, function (err) { ui().toastError(ui().errorText(err, '设置失败')); });
  }

  var _CHAR_FIELDS = [
    { key: 'name', label: '角色名', required: true },
    { key: 'aliases', label: '别名（逗号分隔，用于匹配剧本里的称呼）' },
    { key: 'appearance', label: '外貌（发色/发型/五官/体型）', type: 'textarea', rows: 2 },
    { key: 'outfit', label: '服装（常穿的衣物/配饰）', type: 'textarea', rows: 2 },
    { key: 'palette', label: '配色（主色调，如「靛蓝与银白」）' },
    { key: 'negative', label: '排除项（如「不要改变发色」）' },
    { key: 'seed_offset', label: '种子偏移（0-9999）', type: 'number' }
  ];

  function openCreateCharacter(project) {
    openForm({
      title: '新建角色卡',
      desc: '外貌 / 服装 / 配色会拼成固定锚点，注入到出现该角色的每一页提示词里。',
      fields: _CHAR_FIELDS,
      values: { seed_offset: 0 },
      onSubmit: function (values) {
        api().post('/api/comic/projects/' + project.id + '/characters', values).then(function () {
          ui().toastSuccess('角色已创建');
          loadCharacters(state.charactersHost, project);
        }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
      }
    });
  }

  function openEditCharacter(project, c) {
    var prepend = c && c.image_url ? _buildSamplePrepend(project, c) : null;
    openForm({
      title: '编辑角色卡',
      desc: '修改后，下一次生成分镜会用新锚点（已生成页面的提示词不会自动改写）。',
      fields: _CHAR_FIELDS,
      values: c,
      prepend: prepend,
      onSubmit: function (values) {
        api().patch('/api/comic/characters/' + c.id, values).then(function () {
          ui().toastSuccess('角色已更新');
          loadCharacters(state.charactersHost, project);
        }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
      }
    });
  }

  /** 编辑框顶部外显角色样板图（来自图库），并提供重新选择 / 移除入口。 */
  function _buildSamplePrepend(project, c) {
    var box = ui().el('div', 'character-sample');
    var img = ui().el('img', 'sample-img');
    img.src = c.image_url || '';
    img.alt = (c.name || '角色') + ' 样板图';
    box.appendChild(img);
    var meta = ui().el('div', 'sample-meta');
    meta.appendChild(ui().el('div', 'field-label', '样板图（来自图库）'));
    if (c.sample_prompt) {
      meta.appendChild(ui().el('p', 'entry-text clamp-3 text-tertiary', '出图提示词：' + c.sample_prompt));
    }
    var actions = ui().el('div', 'sample-actions');
    actions.appendChild(btn('ghost', 'image', '重新选择样板图', function () { openImportCharacter(project, c); }));
    actions.appendChild(btn('ghost', 'trash', '移除样板图', function () {
      api().patch('/api/comic/characters/' + c.id, { image_id: '' }).then(function () {
        ui().toastSuccess('已移除样板图');
        loadCharacters(state.charactersHost, project);
      }, function (err) { ui().toastError(ui().errorText(err, '移除失败')); });
    }));
    meta.appendChild(actions);
    box.appendChild(meta);
    return box;
  }

  /** 从图库图片导入为角色（或给已有角色更换样板图）。 */
  function openImportCharacter(project, existing) {
    var body = ui().el('div', 'import-character');

    var nameWrap = ui().el('label', 'field');
    nameWrap.appendChild(ui().el('span', 'field-label', '角色名'));
    var nameInput = ui().el('input', 'input');
    nameInput.type = 'text';
    nameInput.placeholder = '例如 阿岚';
    nameInput.value = existing ? (existing.name || '') : '';
    nameWrap.appendChild(nameInput);
    body.appendChild(nameWrap);

    var grid = ui().el('div', 'image-grid');
    body.appendChild(grid);

    var selectedId = existing && existing.image_id ? existing.image_id : null;
    var promptInput = ui().el('textarea', 'textarea');
    promptInput.rows = 3;
    promptInput.value = existing && existing.sample_prompt ? existing.sample_prompt : '';
    var promptWrap = ui().el('label', 'field');
    promptWrap.appendChild(ui().el('span', 'field-label', '角色卡提示词（自动带出图提示词，可编辑）'));
    promptWrap.appendChild(promptInput);
    body.appendChild(promptWrap);

    function selectImage(item) {
      selectedId = item.id;
      promptInput.value = item.prompt || '';
      Array.prototype.forEach.call(grid.children, function (cell) {
        cell.classList.toggle('selected', cell.getAttribute('data-id') === String(item.id));
      });
    }

    function loadGrid() {
      grid.innerHTML = '';
      grid.appendChild(ui().loadingInline('加载图库…'));
      api().get('/api/gallery?page=1&page_size=60').then(function (data) {
        grid.innerHTML = '';
        var items = (data && data.items) || [];
        if (!items.length) {
          grid.appendChild(ui().emptyState({
            icon: 'image', title: '图库为空',
            desc: '先去生成或导入一些图片，再回来作为角色样板。'
          }));
          return;
        }
        items.forEach(function (item) {
          var cell = ui().el('div', 'image-cell' + (selectedId && String(item.id) === String(selectedId) ? ' selected' : ''));
          cell.setAttribute('data-id', String(item.id));
          var im = ui().el('img', 'thumb');
          im.src = item.url || '';
          im.alt = item.prompt || '图库图片';
          im.loading = 'lazy';
          cell.appendChild(im);
          cell.addEventListener('click', function () { selectImage(item); });
          grid.appendChild(cell);
        });
      }, function () {
        grid.innerHTML = '';
        grid.appendChild(ui().errorState(ui().errorText(null, '加载图库失败'), function () { loadGrid(); }));
      });
    }
    loadGrid();

    ui().modal({
      title: existing ? ('更换样板图 · ' + (existing.name || '角色')) : '导入角色（从图库图片）',
      desc: '选一张图库图片作为角色样板，系统会自动把它的出图提示词填入角色卡提示词；编辑框会外显这张样板图。',
      body: body,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: existing ? '保存样板图' : '导入为角色',
          variant: 'primary',
          onClick: function () {
            var nm = String(nameInput.value || '').trim();
            if (!nm) { ui().toastError('请填写角色名'); return false; }
            if (!selectedId) { ui().toastError('请选择一张图库图片'); return false; }
            var payload = { name: nm, image_id: selectedId, appearance: String(promptInput.value || '') };
            if (existing) {
              api().patch('/api/comic/characters/' + existing.id, payload).then(function () {
                ui().toastSuccess('样板图已更新');
                loadCharacters(state.charactersHost, project);
              }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
            } else {
              api().post('/api/comic/projects/' + project.id + '/characters', payload).then(function () {
                ui().toastSuccess('角色已导入');
                loadCharacters(state.charactersHost, project);
              }, function (err) { ui().toastError(ui().errorText(err, '导入失败')); });
            }
            return true;
          }
        }
      ]
    });
  }

  function deleteCharacter(project, c) {
    ui().confirm({
      title: '删除角色卡',
      desc: '确定删除「' + (c.name || '未命名角色') + '」？已生成页面的提示词不受影响。',
      confirmLabel: '删除',
      danger: true,
      onConfirm: function () {
        api().del('/api/comic/characters/' + c.id).then(function () {
          ui().toastSuccess('角色已删除');
          loadCharacters(state.charactersHost, project);
        }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
      }
    });
  }

  function extractCharacters(project) {
    api().post('/api/comic/projects/' + project.id + '/characters/extract', {}).then(function (data) {
      var created = (data && data.created) || 0;
      ui().toastSuccess(created ? ('已抽取并新建 ' + created + ' 张角色卡') : '没有发现新角色');
      loadCharacters(state.charactersHost, project);
    }, function (err) {
      ui().toastError(ui().errorText(err, '抽取失败'));
    });
  }

  function loadChapters(hostNode, project, silent) {
    if (!silent) {
      hostNode.innerHTML = '';
      hostNode.appendChild(ui().loadingInline('加载章节…'));
    }
    api().get('/api/comic/projects/' + project.id + '/chapters').then(function (data) {
      var items = (data && data.items) || [];
      renderChapterList(hostNode, project, items);
    }, function (err) {
      hostNode.innerHTML = '';
      hostNode.appendChild(ui().errorState(ui().errorText(err, '加载章节失败'), function () { loadChapters(hostNode, project); }));
    });
  }

  function renderChapterList(hostNode, project, items) {
    hostNode.innerHTML = '';
    if (!items.length) {
      hostNode.appendChild(ui().emptyState({
        icon: 'layers',
        title: '还没有章节',
        desc: '新建第一个章节，再往里添加分镜（预制提示词 + 出图工作流）。',
        actions: [{ label: '新建章节', variant: 'primary', onClick: function () { openCreateChapter(project); } }]
      }));
      return;
    }
    items.forEach(function (c) {
      hostNode.appendChild(renderChapterCard(project, c));
    });
  }

  function renderChapterCard(project, c) {
    var card = ui().el('article', 'entry-card');
    card.tabIndex = 0;
    card.style.cursor = 'pointer';
    card.setAttribute('aria-label', '打开章节：' + (c.title || ''));

    var head = ui().el('div', 'entry-head');
    var t = ui().el('h4', 'entry-title', (c.order_idx ? (c.order_idx + '. ') : '') + (c.title || '未命名章节'));
    head.appendChild(t);
    head.appendChild(statusBadge('done')); // 占位，真正状态由分镜聚合
    card.appendChild(head);

    if (c.summary) card.appendChild(ui().el('p', 'entry-text clamp-2', c.summary));

    var foot = ui().el('div', 'entry-foot');
    foot.appendChild(metaBadge('分镜 ' + (c.page_count || 0) + ' / 完成 ' + (c.done_count || 0)));
    card.appendChild(foot);

    var actions = ui().el('div', 'entry-actions');
    actions.appendChild(btn('primary', 'arrowRight', '打开', function (ev) { ev.stopPropagation(); openChapter(c.id); }));
    actions.appendChild(btn('secondary', 'play', '出图本章', function (ev) { ev.stopPropagation(); generateChapter(c, ev.currentTarget); }));
    actions.appendChild(btn('ghost', 'edit', '编辑', function (ev) { ev.stopPropagation(); openEditChapter(project, c); }));
    actions.appendChild(btn('danger', 'trash', '删除', function (ev) { ev.stopPropagation(); deleteChapter(c, ev.currentTarget); }));
    card.appendChild(actions);

    card.addEventListener('click', function () { openChapter(c.id); });
    card.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openChapter(c.id); }
    });
    return card;
  }

  /* ---------------------------------------------------------- 分镜工作流面板 */

  function renderStoryboardPanel(project) {
    var panel = ui().el('div', 'callout callout-storyboard');
    panel.style.display = 'block';
    panel.style.margin = '0 0 16px';
    panel.style.borderColor = 'rgba(108, 114, 255, .35)';
    panel.style.background = 'var(--color-brand-soft)';

    var head = ui().el('div', 'control-row');
    head.style.margin = '0 0 10px';
    var t = ui().el('h3', 'text-section');
    t.style.margin = '0';
    t.appendChild(icon('film', 18));
    t.appendChild(ui().el('span', null, '分镜工作流'));
    head.appendChild(t);
    head.appendChild(ui().el('span', 'grow'));
    head.appendChild(ui().el('span', 'badge', '世界观 + 剧情摘要 → 自动分集出图'));
    panel.appendChild(head);

    var desc = ui().el('p', 'text-tertiary');
    desc.style.margin = '0 0 12px';
    desc.textContent = '填写世界观与剧情摘要，保存后可一键拆分成 N 集、为每集生成并扩写图片提示词，自动入队出图；每集对应一个章节，点击章节即可查看产出图片。';
    panel.appendChild(desc);

    var grid = ui().el('div', 'form-grid');
    grid.style.display = 'grid';
    grid.style.gridTemplateColumns = '1fr 1fr';
    grid.style.gap = '14px';

    var wv = ui().el('textarea', 'textarea');
    wv.rows = 6;
    wv.placeholder = '世界观设定：时代背景、主要势力、核心设定、视觉基调……';
    wv.value = project.worldview || '';
    var wvWrap = ui().el('label', 'field');
    wvWrap.appendChild(ui().el('span', 'field-label', '世界观'));
    wvWrap.appendChild(wv);
    grid.appendChild(wvWrap);

    var ps = ui().el('textarea', 'textarea');
    ps.rows = 6;
    ps.placeholder = '剧情摘要：按集描述故事，可用「第1集 / 第2集」或「1. 2.」分段；留空则整体作为一集。';
    ps.value = project.plot_summary || '';
    var psWrap = ui().el('label', 'field');
    psWrap.appendChild(ui().el('span', 'field-label', '剧情摘要'));
    psWrap.appendChild(ps);
    grid.appendChild(psWrap);

    panel.appendChild(grid);

    // 出图数量（每章页数） + 出图风格：控制每章拆出的漫画页数与统一画面风格
    var optRow = ui().el('div');
    optRow.style.display = 'flex';
    optRow.style.flexWrap = 'wrap';
    optRow.style.gap = '16px';
    optRow.style.marginTop = '12px';

    var ppcWrap = ui().el('label', 'field');
    ppcWrap.style.maxWidth = '240px';
    ppcWrap.style.flex = '0 0 auto';
    ppcWrap.appendChild(ui().el('span', 'field-label', '出图数量（每章页数）'));
    var ppc = ui().el('input', 'input');
    ppc.type = 'number';
    ppc.min = '1';
    ppc.max = '12';
    ppc.step = '1';
    ppc.value = project.pages_per_chapter || 1;
    ppcWrap.appendChild(ppc);
    optRow.appendChild(ppcWrap);

    // 出图风格：统一规范每一集、每一页的画面风格，保证整部漫画风格一致
    var styleWrap = ui().el('label', 'field');
    styleWrap.style.maxWidth = '320px';
    styleWrap.style.flex = '0 0 auto';
    styleWrap.appendChild(ui().el('span', 'field-label', '出图风格'));
    var styleSel = ui().el('select', 'select');
    _fillStyleOptions(styleSel, _FALLBACK_STYLES);
    styleSel.value = project.style_preset || 'none';
    if (styleSel.value !== (project.style_preset || 'none')) styleSel.value = 'none';
    styleSel.title = '每一集、每一页都会套用同一套风格描述词，保证风格统一';
    styleWrap.appendChild(styleSel);
    optRow.appendChild(styleWrap);

    // 异步拉取服务端风格清单（失败则用内置兜底清单，不阻断面板渲染）
    api().get('/api/comic/style-presets').then(function (data) {
      var items = (data && data.items) || null;
      if (!items || !items.length) return;
      _STYLE_CACHE = items;
      var cur = styleSel.value;
      _fillStyleOptions(styleSel, items);
      _setStyleValue(styleSel, cur);
    }, function () { /* 忽略：使用内置兜底清单 */ });

    optRow.appendChild(ui().el('span', 'grow'));
    var hint = ui().el('span', 'badge', '同一风格的每一页提示词会追加统一风格描述词');
    hint.style.alignSelf = 'flex-end';
    optRow.appendChild(hint);
    panel.appendChild(optRow);

    var actions = ui().el('div', 'result-actions');
    actions.style.marginTop = '12px';
    actions.appendChild(btn('ghost', 'edit', '保存设定', function () {
      saveStoryboardSettings(
        project, wv.value, ps.value, _clampPpc(ppc.value), styleSel.value
      );
    }));
    actions.appendChild(btn('primary', 'sparkles', '自动生成分镜出图', function () {
      generateStoryboard(
        project, wv.value, ps.value, _clampPpc(ppc.value), styleSel.value
      );
    }));
    panel.appendChild(actions);

    return panel;
  }

  // 出图风格：内置兜底清单（服务端 /api/comic/style-presets 可用时会被覆盖）
  var _STYLE_CACHE = null;
  var _FALLBACK_STYLES = [
    { key: 'none', label: '不限制（按剧情自由发挥）' },
    { key: 'japanese_manga', label: '日式黑白漫画' },
    { key: 'shinkai', label: '新海诚动画电影风' },
    { key: 'american_comic', label: '美式漫画（厚描边平涂）' },
    { key: 'ink_wash', label: '国风水墨' },
    { key: 'watercolor', label: '水彩绘本' },
    { key: 'cyberpunk', label: '赛博朋克' },
    { key: 'pixel_art', label: '像素游戏风' },
    { key: 'realistic', label: '写实厚涂插画' }
  ];

  function _fillStyleOptions(sel, items) {
    if (!sel) return;
    sel.innerHTML = '';
    (items || []).forEach(function (o) {
      var op = ui().el('option');
      op.value = o.key;
      op.textContent = o.label;
      if (o.desc) op.title = o.desc;
      sel.appendChild(op);
    });
  }

  function _setStyleValue(sel, value) {
    if (!sel) return;
    sel.value = value || 'none';
    if (sel.value !== (value || 'none')) sel.value = 'none';
  }

  function _styleLabel(key) {
    var list = _STYLE_CACHE || _FALLBACK_STYLES;
    for (var i = 0; i < list.length; i++) {
      if (list[i].key === key) return list[i].label;
    }
    return key || 'none';
  }

  function saveStoryboardSettings(project, worldview, plotSummary, pagesPerChapter, style) {
    api().patch('/api/comic/projects/' + project.id, {
      worldview: worldview || '',
      plot_summary: plotSummary || '',
      pages_per_chapter: pagesPerChapter || 1,
      style_preset: style || 'none'
    }).then(function () {
      ui().toastSuccess('已保存世界观 / 剧情摘要 / 出图数量 / 出图风格');
    }, function (err) {
      ui().toastError(ui().errorText(err, '保存失败'));
    });
  }

  /**
   * 用「项目已保存的设定」重新跑一遍分镜（工具栏「自动分镜」）。
   * 会重新拉一次项目，保证用的是存库的世界观 / 剧情 / 页数 / 风格，而不是面板里的临时值。
   */
  function generateStoryboardSaved(project) {
    var body = ui().el('div', 'form-stack');
    var modeWrap = ui().el('label', 'field');
    modeWrap.appendChild(ui().el('span', 'field-label', '生成模式'));
    var modeSel = ui().el('select', 'select');
    [
      { value: 'append', label: '追加：保留已有章节，新分集排在后面' },
      { value: 'rebuild', label: '重建：先清空已有章节再生成（改完剧情用它，不会留下重复分集）' }
    ].forEach(function (o) {
      var op = ui().el('option');
      op.value = o.value;
      op.textContent = o.label;
      modeSel.appendChild(op);
    });
    modeWrap.appendChild(modeSel);

    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.style.margin = '8px 0 0';
    tip.id = 'aibar-sb-mode-tip';
    tip.textContent = '追加模式：已有章节与分镜页全部保留，新分集追加在后面。';
    modeSel.addEventListener('change', function () {
      tip.textContent = modeSel.value === 'rebuild'
        ? '重建模式：会先删除该项目所有已有章节（连带分镜页与队列任务），再重新拆集出图。'
        : '追加模式：已有章节与分镜页全部保留，新分集追加在后面。';
    });
    body.appendChild(modeWrap);
    body.appendChild(tip);

    ui().modal({
      title: '按保存的设定重新生成分镜',
      desc: '将按项目已保存的世界观 / 剧情摘要 / 出图数量 / 出图风格重新拆集并生成每页提示词，然后重新入队出图。',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '开始生成',
          variant: 'primary',
          onClick: function () {
            var mode = modeSel.value;
            api().get('/api/comic/projects/' + project.id).then(function (p) {
              generateStoryboard(
                p, p.worldview, p.plot_summary, _clampPpc(p.pages_per_chapter),
                p.style_preset, mode
              );
            }, function (err) {
              ui().toastError(ui().errorText(err, '读取项目设定失败'));
            });
            return true;
          }
        }
      ]
    });
  }

  function generateStoryboard(project, worldview, plotSummary, pagesPerChapter, style, mode) {
    var payload = {
      worldview: worldview || '',
      plot_summary: plotSummary || '',
      pages_per_chapter: pagesPerChapter || 1,
      style: style || 'none',
      mode: mode || 'append'
    };
    api().post('/api/comic/projects/' + project.id + '/storyboard', payload, { timeout: api().LONG_TIMEOUT }).then(function (res) {
      var styleText = res.style_label || _styleLabel(res.style);
      var removed = (res.mode === 'rebuild' && res.removed_chapters)
        ? '，已清理旧章节 ' + res.removed_chapters + ' 个' : '';
      var msg = '已生成 ' + (res.episodes || 0) + ' 集 / ' + (res.chapters || 0) +
        ' 章 / 每章 ' + (res.pages_per_chapter || 1) + ' 页，共 ' + (res.pages || 0) +
        ' 张分镜，入队 ' + (res.enqueued || 0) + ' 张' +
        '（风格：' + styleText + '；引擎：' + (res.provider || 'rules') + removed + '）';
      ui().toastSuccess(msg);
      renderProject();
    }, function (err) {
      ui().toastError(ui().errorText(err, '生成分镜失败'));
    });
  }

  /**
   * 重刷提示词：改完角色卡外貌 / 换了风格之后，用每一页保存的原始扩写结果
   * 重新套一遍「角色锚点 + 风格词 + 种子」，不必重新拆分剧情。
   */
  function refreshPrompts(project) {
    var body = ui().el('div', 'form-stack');

    var styleWrap = ui().el('label', 'field');
    styleWrap.appendChild(ui().el('span', 'field-label', '出图风格'));
    var styleSel = ui().el('select', 'select');
    _fillStyleOptions(styleSel, _STYLE_CACHE || _FALLBACK_STYLES);
    // 「沿用当前风格」放第一个且默认选中：换风格是顺手操作，不该成为默认行为
    var keepOp = ui().el('option');
    keepOp.value = '';
    keepOp.textContent = '沿用当前风格（' + _styleLabel(project.style_preset) + '）';
    styleSel.insertBefore(keepOp, styleSel.firstChild);
    styleSel.value = '';
    styleWrap.appendChild(styleSel);
    body.appendChild(styleWrap);

    // 服务端风格清单还没拉过时补拉一次（失败就用内置兜底清单）
    if (!_STYLE_CACHE) {
      api().get('/api/comic/style-presets').then(function (data) {
        var items = (data && data.items) || null;
        if (!items || !items.length) return;
        _STYLE_CACHE = items;
        _fillStyleOptions(styleSel, items);
        styleSel.insertBefore(keepOp, styleSel.firstChild);
        styleSel.value = '';
      }, function () { /* 忽略：使用内置兜底清单 */ });
    }

    var reqWrap = ui().el('label', 'field');
    reqWrap.appendChild(ui().el('span', 'field-label', '重算后自动出图'));
    var reqSel = ui().el('select', 'select');
    [
      { value: 'failed', label: '只重跑失败的页（推荐）' },
      { value: 'all', label: '全部页重新出图' },
      { value: 'none', label: '只改提示词，先不出图' }
    ].forEach(function (o) {
      var op = ui().el('option');
      op.value = o.value;
      op.textContent = o.label;
      reqSel.appendChild(op);
    });
    reqWrap.appendChild(reqSel);
    body.appendChild(reqWrap);

    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.style.margin = '8px 0 0';
    tip.textContent = '只会重做「注入角色锚点 + 合并排除项 + 套用风格 + 派生种子」这一步，' +
      '剧情拆分与提示词扩写结果保持不变，因此不会把已有分镜打乱。';
    body.appendChild(tip);

    ui().modal({
      title: '重刷全部页提示词',
      desc: '按当前角色卡 / 世界观 / 出图风格，重算这个项目所有分镜页的提示词与种子。',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '开始重刷',
          variant: 'primary',
          onClick: function () {
            var payload = { requeue: reqSel.value || 'failed' };
            if (styleSel.value) payload.style = styleSel.value;
            api().post('/api/comic/projects/' + project.id + '/refresh-prompts', payload, { timeout: api().LONG_TIMEOUT })
              .then(function (res) {
                var msg = '已更新 ' + (res.updated || 0) + ' 页提示词，其中 ' +
                  (res.changed || 0) + ' 页内容有变化' +
                  (res.enqueued ? '，重新入队 ' + res.enqueued + ' 页' : '') +
                  '（风格：' + (res.style_label || _styleLabel(res.style)) + '）';
                ui().toastSuccess(msg);
                renderProject();
              }, function (err) {
                ui().toastError(ui().errorText(err, '重刷提示词失败'));
              });
            return true;
          }
        }
      ]
    });
  }

  function _clampPpc(v) {
    var n = parseInt(v, 10);
    if (isNaN(n)) n = 1;
    return Math.max(1, Math.min(12, n));
  }

  /* ---------------------------------------------------------- 章节详情（分镜） */

  function openChapter(id) {
    state.view = 'chapter';
    state.chapterId = id;
    renderChapter();
  }

  function renderChapter() {
    clearPoll();
    var h = host();
    if (!h) return;
    h.innerHTML = '';

    api().get('/api/comic/chapters/' + state.chapterId).then(function (chapter) {
      api().get('/api/comic/projects/' + chapter.project_id).then(function (project) {
        renderChapterShell(h, project, chapter);
      }, function () { renderChapterShell(h, null, chapter); });
    }, function (err) {
      h.appendChild(ui().errorState(ui().errorText(err, '加载章节失败'), function () { renderChapter(); }));
    });
  }

  function renderChapterShell(h, project, chapter) {
    h.innerHTML = '';
    var crumbs = [{ label: '漫画概览', onClick: renderOverview }];
    if (project) crumbs.push({ label: project.name || '未命名漫画', onClick: function () { openProject(project.id); } });
    crumbs.push({ label: chapter.title || '未命名章节' });
    h.appendChild(breadcrumb(crumbs));

    if (chapter.summary) {
      var meta = ui().el('div', 'callout callout-info');
      meta.style.margin = '0 0 14px';
      meta.appendChild(ui().el('div', 'break-any', chapter.summary));
      h.appendChild(meta);
    }

    var toolbar = ui().el('div', 'page-toolbar');
    toolbar.appendChild(btn('primary', 'plus', '新建分镜', function () { openCreatePage(chapter); }));
    toolbar.appendChild(btn('secondary', 'play', '出图本章', function (ev) { generateChapter(chapter, ev.currentTarget); }));
    toolbar.appendChild(btn('ghost', 'edit', '编辑章节', function () { openEditChapter(project, chapter); }));
    toolbar.appendChild(btn('danger', 'trash', '删除章节', function (ev) { deleteChapter(chapter, ev.currentTarget); }));
    toolbar.appendChild(ui().el('span', 'grow'));
    h.appendChild(toolbar);

    var pageHost = ui().el('div', 'entry-list');
    h.appendChild(sectionTitle('分镜（预制提示词 + 出图工作流）'));
    h.appendChild(pageHost);
    loadPages(pageHost, chapter);
  }

  function loadPages(hostNode, chapter) {
    hostNode.innerHTML = '';
    hostNode.appendChild(ui().loadingInline('加载分镜…'));
    api().get('/api/comic/chapters/' + chapter.id + '/pages').then(function (data) {
      var items = (data && data.items) || [];
      renderPageList(hostNode, chapter, items);
    }, function (err) {
      hostNode.innerHTML = '';
      hostNode.appendChild(ui().errorState(ui().errorText(err, '加载分镜失败'), function () { loadPages(hostNode, chapter); }));
    });
  }

  function renderPageList(hostNode, chapter, items) {
    hostNode.innerHTML = '';
    if (!items.length) {
      hostNode.appendChild(ui().emptyState({
        icon: 'image',
        title: '还没有分镜',
        desc: '为这一章添加分镜，填写预制提示词与出图工作流，再一键入队出图。',
        actions: [{ label: '新建分镜', variant: 'primary', onClick: function () { openCreatePage(chapter); } }]
      }));
      return;
    }
    items.forEach(function (pg) {
      hostNode.appendChild(renderPageCard(chapter, pg));
    });
  }

  function renderPageCard(chapter, pg) {
    var card = ui().el('article', 'entry-card');

    if (pg.image_path) {
      var img = doc.createElement('img');
      img.className = 'gal-thumb';
      img.loading = 'lazy';
      img.alt = pg.title || '分镜预览';
      img.src = imageUrl(pg.image_path);
      card.appendChild(img);
    }

    var head = ui().el('div', 'entry-head');
    var t = ui().el('h4', 'entry-title', (pg.order_idx ? (pg.order_idx + '. ') : '') + (pg.title || '未命名分镜'));
    head.appendChild(t);
    head.appendChild(statusBadge(pg.status));
    card.appendChild(head);

    if (pg.prompt_text) card.appendChild(ui().el('p', 'entry-text clamp-2', pg.prompt_text));
    if (pg.negative_text) {
      var neg = ui().el('p', 'entry-text clamp-1 text-tertiary', '负向：' + pg.negative_text);
      card.appendChild(neg);
    }

    // 失败原因：优先展示后端翻译好的中文文案，其次退回错误码
    if (pg.status === 'failed' && (pg.error_message || pg.error_code)) {
      var fail = ui().el('p', 'entry-text', '失败：' + errorLabel(pg.error_code, pg.error_message));
      fail.style.color = 'var(--color-state-error)';
      card.appendChild(fail);
    }

    var foot = ui().el('div', 'entry-foot');
    if (pg.shot_note) foot.appendChild(metaBadge('景别：' + String(pg.shot_note).replace(/^[（(]/, '').replace(/[)）]$/, '')));
    if (pg.workflow_filename) foot.appendChild(metaBadge('工作流：' + pg.workflow_filename));
    if (pg.seed !== null && pg.seed !== undefined && pg.seed !== '') foot.appendChild(metaBadge('种子：' + pg.seed));
    if (pg.character_names) foot.appendChild(metaBadge('人物：' + pg.character_names));
    card.appendChild(foot);

    var actions = ui().el('div', 'entry-actions');
    if (pg.status === 'generating') {
      actions.appendChild(btn('ghost', 'refresh', '出图中…', null));
    } else if (pg.status === 'done') {
      actions.appendChild(btn('secondary', 'refresh', '重新生成', function (ev) { regeneratePage(pg, ev.currentTarget); }));
    } else {
      actions.appendChild(btn('primary', 'play', '出图', function (ev) { generatePage(pg, ev.currentTarget); }));
    }
    actions.appendChild(btn('ghost', 'external', '在 ComfyUI 打开', function () { openPageInComfyUI(pg); }));
    actions.appendChild(btn('ghost', 'sparkles', '重扩写', function (ev) { reexpandPage(pg, ev.currentTarget); }));
    // 产出图回流图库后即可对它做提示词反推：把这张分镜送进反推工作台
    if (pg.status === 'done') {
      actions.appendChild(btn('ghost', 'image', '反推提示词', function (ev) {
        reversePageImage(pg, ev.currentTarget);
      }));
    }
    actions.appendChild(btn('ghost', 'edit', '编辑', function () { openEditPage(chapter, pg); }));
    actions.appendChild(btn('danger', 'trash', '删除', function (ev) { deletePage(pg, ev.currentTarget); }));
    card.appendChild(actions);
    return card;
  }

  /** 把该分镜的产出图送进提示词反推工作台。

      图库 id 是后加的字段，改造前出好的图没有它 —— 这里先补登记一次再跳转，
      免得用户面对「历史项目一律点不动」的死按钮。
  */
  function reversePageImage(pg, node) {
    function go(imageId) {
      if (!AIBAR.studio || !AIBAR.studio.enterReverseWithImage) {
        ui().toastError('反推工作台未就绪，请刷新页面后重试');
        return;
      }
      AIBAR.studio.enterReverseWithImage(imageId);
    }
    if (pg.image_id) { go(pg.image_id); return; }
    // 补登记是全项目扫描，返回值不带单页 id，这里补一次单页查询取回新 id
    var req = api().post('/api/comic/projects/' + pg.project_id + '/sync-gallery', {})
      .then(function () { return api().get('/api/comic/pages/' + pg.id); })
      .then(function (row) {
        if (!row || !row.image_id) throw new Error('该分镜还没有产出图，请先出图');
        pg.image_id = row.image_id;
        return row.image_id;
      });
    ui().withBusy(node, '登记中…', req).then(function (imageId) {
      if (imageId) go(imageId);
    }, function (err) {
      ui().toastError(ui().errorText(err, '登记图库失败'));
    });
  }

  /** 单页重扩写：重跑一次扩写模型，再按当前角色卡与风格重算该页提示词。

      与整本「重刷提示词」的关系：整本是「改了角色外貌/风格后全本同步」，
      单页是「这一页扩写结果不满意」，代价只是一次模型调用而不是重拆全本。
  */
  function reexpandPage(pg, node) {
    var body = { requeue: false };
    var req = api().post('/api/comic/pages/' + pg.id + '/reexpand', body, { timeout: api().LONG_TIMEOUT });
    ui().withBusy(node, '扩写中…', req).then(function (d) {
      if (!d) return;
      var msg = d.reexpanded ? '已重新扩写' : '已按当前角色卡与风格重算';
      if (!d.changed) msg += '（提示词无变化）';
      ui().toastSuccess(msg);
      (d.warnings || []).forEach(function (w) { ui().toastError(w); });
      refreshCurrent();
    }, function (err) {
      ui().toastError(ui().errorText(err, '重扩写失败'));
    });
  }

  /** 用该页的提示词 + 种子在 ComfyUI 里打开对应工作流，方便手工微调后直接跑。 */
  function openPageInComfyUI(pg) {
    api().get('/api/comic/pages/' + pg.id + '/editor-link').then(function (d) {
      if (!d || !d.url) {
        ui().toastError('未能生成 ComfyUI 链接');
        return;
      }
      var w = window.open(d.url, '_blank');
      if (!w) ui().toastError('浏览器拦截了新窗口，请允许弹出窗口后重试');
      else ui().toastSuccess('已载入工作流：' + (d.workflow_name || d.workflow || ''));
    }, function (err) {
      ui().toastError(ui().errorText(err, '生成 ComfyUI 链接失败'));
    });
  }

  /* ---------------------------------------------------------- 队列 */

  function loadJobs(hostNode, projectId, silent) {
    if (!silent) {
      hostNode.innerHTML = '';
      hostNode.appendChild(ui().loadingInline('加载队列…'));
    }
    api().get('/api/comic/jobs', { project_id: projectId }).then(function (data) {
      var items = (data && data.items) || [];
      state.jobs = items;
      renderJobs(hostNode, items, projectId);
      _jobsActive = items.some(function (j) {
        return j.status === 'queued' || j.status === 'running';
      });
      scheduleLive(state.project);
    }, function (err) {
      hostNode.innerHTML = '';
      hostNode.appendChild(ui().errorState(ui().errorText(err, '加载队列失败'), function () { loadJobs(hostNode, projectId); }));
    });
  }

  function renderJobs(hostNode, items, projectId) {
    hostNode.innerHTML = '';
    if (!items.length) {
      hostNode.appendChild(ui().el('p', 'text-tertiary', '暂无出图任务。'));
      return;
    }
    var table = ui().el('table', 'data');
    var thead = ui().el('thead');
    var htr = ui().el('tr');
    ['ID', '分镜', '状态', '阶段', '错误', '创建', '完成', '操作'].forEach(function (c) {
      var th = ui().el('th', null, c);
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    table.appendChild(thead);

    var tbody = ui().el('tbody');
    items.slice(0, 50).forEach(function (j) {
      var tr = ui().el('tr');
      tr.appendChild(ui().el('td', null, String(j.id)));
      tr.appendChild(ui().el('td', null, '#' + (j.page_id)));
      var stTd = ui().el('td');
      stTd.appendChild(statusBadge(j.status));
      tr.appendChild(stTd);
      tr.appendChild(ui().el('td', null, j.stage || '—'));
      tr.appendChild(ui().el('td', null, errorLabel(j.error_code, j.error_message) || '—'));
      tr.appendChild(ui().el('td', 'text-tertiary', ui().formatTime(j.created_at)));
      tr.appendChild(ui().el('td', 'text-tertiary', ui().formatTime(j.finished_at)));
      var opTd = ui().el('td');
      if (j.status === 'queued' || j.status === 'running') {
        opTd.appendChild(btn('ghost', 'close', '取消', function () { cancelJob(j); }));
      }
      tr.appendChild(opTd);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    hostNode.appendChild(table);
  }

  function clearPoll() {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  }

  /* ---------------------------------------------------------- 操作：入队 / 取消 */

  function generatePage(pg, node) {
    var req = api().post('/api/comic/pages/' + pg.id + '/generate', {});
    return ui().withBusy(node, '入队中…', req).then(function (data) {
      ui().toastSuccess('已入队出图（任务 #' + (data && data.job_id) + '）');
      refreshCurrent();
    }, function (err) {
      ui().toastError(ui().errorText(err, '入队失败'));
    });
  }

  function regeneratePage(pg, node) {
    var req = api().post('/api/comic/pages/' + pg.id + '/regenerate', {});
    return ui().withBusy(node, '入队中…', req).then(function (data) {
      ui().toastSuccess('已重新入队出图（任务 #' + (data && data.job_id) + '）');
      refreshCurrent();
    }, function (err) {
      ui().toastError(ui().errorText(err, '重新出图失败'));
    });
  }

  function generateChapter(c, node) {
    var req = api().post('/api/comic/chapters/' + c.id + '/generate', {}, { timeout: api().EXTRA_LONG_TIMEOUT });
    return ui().withBusy(node, '入队中…', req).then(function (data) {
      ui().toastSuccess('本章已入队 ' + ((data && data.enqueued) || 0) + ' 张');
      refreshCurrent();
    }, function (err) {
      ui().toastError(ui().errorText(err, '入队失败'));
    });
  }

  function generateProject(p, node) {
    var req = api().post('/api/comic/projects/' + p.id + '/generate', {}, { timeout: api().EXTRA_LONG_TIMEOUT });
    return ui().withBusy(node, '入队中…', req).then(function (data) {
      var n = (data && data.enqueued) || 0;
      if (!n) {
        ui().toastSuccess('没有需要出图的页（可能都还在出图中）');
        return;
      }
      ui().toastSuccess('全本已入队 ' + n + ' 张');
      refreshCurrent();
    }, function (err) {
      ui().toastError(ui().errorText(err, '入队失败'));
    });
  }

  /**
   * 按条件批量重出图：整本重跑太浪费，通常只想重跑「失败的」或「某个景别的」。
   */
  function generateProjectFiltered(p, node) {
    api().get('/api/comic/projects/' + p.id + '/pages').then(function (data) {
      // api() 已解包信封，data 即 {items:[...]}，别把整个对象当数组去 forEach
      openGenerateFilterModal(p, (data && data.items) || [], node);
    }, function () {
      // 取不到分页信息就退回「整本出图」，不让用户卡在这儿
      generateProject(p, node);
    });
  }

  function openGenerateFilterModal(p, pages, node) {
    var shots = [];
    var chars = [];
    (pages || []).forEach(function (pg) {
      var shot = (pg.shot_note || '').replace(/^[（(]/, '').replace(/[)）]$/, '').trim();
      if (shot && shots.indexOf(shot) < 0) shots.push(shot);
      String(pg.character_names || '').split(/[,，、]/).forEach(function (n) {
        n = n.trim();
        if (n && chars.indexOf(n) < 0) chars.push(n);
      });
    });

    var failed = (pages || []).filter(function (pg) { return pg.status === 'failed'; }).length;
    var pending = (pages || []).filter(function (pg) { return pg.status === 'pending'; }).length;

    var scopeSel = ui().el('select', 'input');
    [
      { value: 'all', label: '全部页（' + (pages || []).length + ' 张）' },
      { value: 'failed', label: '只重跑失败的（' + failed + ' 张）' },
      { value: 'pending', label: '只跑还没出图的（' + pending + ' 张）' }
    ].forEach(function (o) {
      var op = ui().el('option', null, o.label);
      op.value = o.value;
      scopeSel.appendChild(op);
    });

    var shotSel = ui().el('select', 'input');
    var shotNone = ui().el('option', null, '不限景别');
    shotNone.value = '';
    shotSel.appendChild(shotNone);
    shots.forEach(function (s) {
      var op = ui().el('option', null, s);
      op.value = s;
      shotSel.appendChild(op);
    });

    var charSel = ui().el('select', 'input');
    var charNone = ui().el('option', null, '不限角色');
    charNone.value = '';
    charSel.appendChild(charNone);
    chars.forEach(function (c) {
      var op = ui().el('option', null, c);
      op.value = c;
      charSel.appendChild(op);
    });

    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '只重跑需要重跑的页，已经出好的图不会被覆盖，能省下大量出图时间。';

    var body = ui().el('div', 'form-grid');
    [[scopeSel, '出图范围'], [shotSel, '景别'], [charSel, '角色']].forEach(function (pair) {
      var wrap = ui().el('label', 'field');
      wrap.appendChild(ui().el('span', 'field-label', pair[1]));
      wrap.appendChild(pair[0]);
      body.appendChild(wrap);
    });
    body.appendChild(tip);

    ui().modal({
      title: '按条件重出图',
      desc: '选择要重跑的分镜页范围',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost' },
        {
          label: '开始出图',
          variant: 'primary',
          onClick: function () {
            var payload = {};
            var scope = scopeSel.value;
            if (scope === 'failed') payload.statuses = ['failed'];
            else if (scope === 'pending') payload.statuses = ['pending'];
            if (shotSel.value) payload.shot = shotSel.value;
            if (charSel.value) payload.character = charSel.value;

            var req = api().post('/api/comic/projects/' + p.id + '/generate', payload,
              { timeout: api().EXTRA_LONG_TIMEOUT });
            return ui().withBusy(node, '入队中…', req).then(function (data) {
              var n = (data && data.enqueued) || 0;
              if (!n) {
                ui().toastSuccess('没有符合条件的页');
                return;
              }
              ui().toastSuccess('已入队 ' + n + ' 张');
              refreshCurrent();
            }, function (err) {
              ui().toastError(ui().errorText(err, '入队失败'));
            });
          }
        }
      ]
    });
  }

  function cancelJob(j) {
    api().post('/api/comic/jobs/' + j.id + '/cancel', {}).then(function () {
      ui().toastSuccess('已取消任务 #' + j.id);
      refreshCurrent();
    }, function (err) {
      ui().toastError(ui().errorText(err, '取消失败'));
    });
  }

  function refreshCurrent() {
    if (state.view === 'chapter') renderChapter();
    else if (state.view === 'project') renderProject();
    else renderOverview();
  }

  /* ---------------------------------------------------------- 操作：增删改（表单） */

  function openForm(opts) {
    var body = ui().el('div', 'form-stack');
    if (opts.prepend) body.appendChild(opts.prepend);
    var controls = {};
    (opts.fields || []).forEach(function (f) {
      var control;
      if (f.type === 'textarea') {
        control = ui().el('textarea', 'textarea');
        control.rows = f.rows || 4;
      } else if (f.type === 'select') {
        control = ui().el('select', 'select');
        (f.options || []).forEach(function (o) {
          var op = ui().el('option');
          op.value = o.value;
          op.textContent = o.label;
          control.appendChild(op);
        });
      } else if (f.type === 'checkbox') {
        control = ui().el('input');
        control.type = 'checkbox';
        control.style.cssText = 'width:16px;height:16px;margin-left:8px;align-self:center';
      } else {
        control = ui().el('input', 'input');
        control.type = f.type === 'number' ? 'number' : 'text';
      }
      var initial = opts.values ? opts.values[f.key] : undefined;
      if (f.type === 'checkbox') {
        control.checked = initial === true || initial === 1 ||
          String(initial) === '1' || String(initial) === 'true';
      } else if (initial !== undefined && initial !== null) {
        control.value = initial;
      }
      if (f.placeholder) control.placeholder = f.placeholder;
      controls[f.key] = control;

      var wrap = ui().el('label', 'field');
      wrap.appendChild(ui().el('span', 'field-label', f.label));
      wrap.appendChild(control);
      body.appendChild(wrap);
    });

    ui().modal({
      title: opts.title,
      desc: opts.desc,
      body: body,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: opts.submitLabel || '保存',
          variant: 'primary',
          onClick: function () {
            for (var i = 0; i < (opts.fields || []).length; i += 1) {
              var f = opts.fields[i];
              if (f.required && !String(controls[f.key].value || '').trim()) {
                ui().toastError('请填写：' + f.label);
                return false;
              }
            }
            var values = {};
            for (var k in controls) {
              if (!Object.prototype.hasOwnProperty.call(controls, k)) continue;
              values[k] = controls[k].type === 'checkbox' ? controls[k].checked : controls[k].value;
            }
            (opts.fields || []).forEach(function (f) {
              if (f.type === 'number') {
                var s = String(values[f.key] || '').trim();
                values[f.key] = s === '' ? null : Number(s);
              }
            });
            opts.onSubmit(values);
            return true;
          }
        }
      ]
    });
  }

  function openCreateProject() {
    openForm({
      title: '新建漫画',
      desc: '填写漫画概览信息，默认工作流会在分镜未单独指定时继承使用。',
      fields: [
        { key: 'name', label: '漫画名称', required: true },
        { key: 'description', label: '描述', type: 'textarea' },
        { key: 'status', label: '状态', type: 'select', options: [
          { value: 'draft', label: '草稿' },
          { value: 'production', label: '制作中' },
          { value: 'done', label: '已完成' }
        ] },
        { key: 'default_workflow', label: '默认工作流文件', placeholder: '例如 tme_comic_v1.json' }
      ],
      values: { status: 'draft' },
      onSubmit: function (values) {
        api().post('/api/comic/projects', values).then(function () {
          ui().toastSuccess('漫画已创建');
          renderOverview();
        }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
      }
    });
  }

  function openImportSkill() {
    openForm({
      title: '导入 Skill 工作流',
      desc: '把任意 SKILL.md 的流程沉淀为漫画工作室大工作流：frontmatter 名称 → 漫画；## 段落 → 章节；编号步骤 → 分镜页（步骤原文作为预制提示词）。未指定工作流时自动写入 FLUX.2 最小化工作流。',
      fields: [
        { key: 'markdown', label: '粘贴 SKILL.md 内容', type: 'textarea', required: true, rows: 10,
          placeholder: '直接粘贴 skill 的 SKILL.md 全文（优先于 path）' },
        { key: 'path', label: '或填写 SKILL.md 路径', placeholder: '例如 ~/.workbuddy/skills/comfyui-flux-storyboard/SKILL.md' },
        { key: 'title', label: '漫画名称（可选，留空用 skill name）', placeholder: '覆盖导入后的漫画名称' },
        { key: 'workflow_filename', label: '出图工作流文件（可选）', placeholder: '留空则自动写入 FLUX.2 工作流' }
      ],
      onSubmit: function (values) {
        var payload = {
          markdown: values.markdown || '',
          path: values.path || '',
          title: values.title || '',
          workflow_filename: values.workflow_filename || ''
        };
        api().post('/api/comic/import-skill', payload, { timeout: api().LONG_TIMEOUT }).then(function (result) {
          ui().toastSuccess('已导入：' + (result.project_name || '') + '（' +
            (result.chapters || 0) + ' 章 / ' + (result.pages || 0) + ' 页）');
          if (result.project_id) openProject(result.project_id);
        }, function (err) { ui().toastError(ui().errorText(err, '导入失败')); });
      }
    });
  }

  function openEditProject(p) {
    openForm({
      title: '编辑漫画',
      fields: [
        { key: 'name', label: '漫画名称', required: true },
        { key: 'description', label: '描述', type: 'textarea' },
        { key: 'status', label: '状态', type: 'select', options: [
          { value: 'draft', label: '草稿' },
          { value: 'production', label: '制作中' },
          { value: 'done', label: '已完成' }
        ] },
        { key: 'default_workflow', label: '默认工作流文件', placeholder: '例如 tme_comic_v1.json' }
      ],
      values: { name: p.name, description: p.description, status: p.status, default_workflow: p.default_workflow },
      submitLabel: '保存',
      onSubmit: function (values) {
        api().patch('/api/comic/projects/' + p.id, values).then(function () {
          ui().toastSuccess('已保存');
          renderOverview();
        }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
      }
    });
  }

  function openCreateChapter(project) {
    openForm({
      title: '新建章节',
      desc: '章节按 order_idx 排序；往章节里添加分镜并维护预制提示词。',
      fields: [
        { key: 'title', label: '章节标题', required: true },
        { key: 'order_idx', label: '排序', type: 'number' },
        { key: 'summary', label: '章节概要', type: 'textarea' }
      ],
      values: { order_idx: 0 },
      onSubmit: function (values) {
        api().post('/api/comic/projects/' + project.id + '/chapters', values).then(function () {
          ui().toastSuccess('章节已创建');
          renderProject();
        }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
      }
    });
  }

  function openEditChapter(project, c) {
    openForm({
      title: '编辑章节',
      fields: [
        { key: 'title', label: '章节标题', required: true },
        { key: 'order_idx', label: '排序', type: 'number' },
        { key: 'summary', label: '章节概要', type: 'textarea' }
      ],
      values: { title: c.title, order_idx: c.order_idx, summary: c.summary },
      submitLabel: '保存',
      onSubmit: function (values) {
        api().patch('/api/comic/chapters/' + c.id, values).then(function () {
          ui().toastSuccess('已保存');
          if (project) renderProject(); else renderChapter();
        }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
      }
    });
  }

  function openCreatePage(chapter) {
    openForm({
      title: '新建分镜',
      desc: '填写预制提示词与出图工作流文件（留空则继承章节所属漫画的默认工作流）。',
      fields: [
        { key: 'title', label: '分镜标题' },
        { key: 'order_idx', label: '排序', type: 'number' },
        { key: 'prompt_text', label: '预制正向提示词', type: 'textarea', rows: 4 },
        { key: 'negative_text', label: '负向提示词', type: 'textarea', rows: 3 },
        { key: 'workflow_filename', label: '出图工作流文件', placeholder: '留空则继承默认工作流' },
        { key: 'seed', label: '种子（可选）', type: 'number' }
      ],
      values: { order_idx: 0 },
      onSubmit: function (values) {
        api().post('/api/comic/chapters/' + chapter.id + '/pages', values).then(function () {
          ui().toastSuccess('分镜已创建');
          renderChapter();
        }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
      }
    });
  }

  function openEditPage(chapter, pg) {
    openForm({
      title: '编辑分镜',
      desc: '修改预制提示词 / 出图工作流后，可对该分镜单独重新生成图片。',
      fields: [
        { key: 'title', label: '分镜标题' },
        { key: 'order_idx', label: '排序', type: 'number' },
        { key: 'prompt_text', label: '预制正向提示词', type: 'textarea', rows: 4 },
        { key: 'negative_text', label: '负向提示词', type: 'textarea', rows: 3 },
        { key: 'workflow_filename', label: '出图工作流文件', placeholder: '留空则继承默认工作流' },
        { key: 'seed', label: '种子（可选）', type: 'number' }
      ],
      values: {
        title: pg.title, order_idx: pg.order_idx, prompt_text: pg.prompt_text,
        negative_text: pg.negative_text, workflow_filename: pg.workflow_filename, seed: pg.seed
      },
      submitLabel: '保存',
      onSubmit: function (values) {
        api().patch('/api/comic/pages/' + pg.id, values).then(function () {
          ui().toastSuccess('已保存');
          renderChapter();
        }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
      }
    });
  }

  /* ---------------------------------------------------------- 操作：删除 */

  function deleteProject(p) {
    ui().confirm({
      title: '删除漫画',
      message: '确定删除「' + (p.name || '') + '」及其全部章节、分镜与出图任务？此操作不可恢复。',
      confirmLabel: '删除',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      api().del('/api/comic/projects/' + p.id).then(function () {
        ui().toastSuccess('已删除');
        state.view = 'overview';
        renderOverview();
      }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
    });
  }

  function deleteChapter(c, node) {
    ui().confirm({
      title: '删除章节',
      message: '确定删除该章节及其下所有分镜与出图任务？此操作不可恢复。',
      confirmLabel: '删除',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      var req = api().del('/api/comic/chapters/' + c.id);
      // 章节下分镜与产出图越多，删除越慢（要级联清 pages / jobs 并删磁盘文件）。
      // 期间不给反馈，用户会以为「点了没反应 / 删除无效」——这是本反馈缺失的主要症状。
      var p = node ? ui().withBusy(node, '删除中…', req) : req;
      return p.then(function () {
        ui().toastSuccess('已删除章节《' + (c.title || ('#' + c.id)) + '》');
        // 服务端此刻已经删掉了；若重绘列表抛异常，Promise 会变成没人接的拒绝，
        // 界面停在旧数据上 —— 用户看到的就是「删了但内容还在 / 删除无效」。
        // 所以刷新失败也必须给出提示，而不是静默吞掉。
        safeRender(renderProject, '章节已删除，但列表刷新失败');
      }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
    });
  }

  function deletePage(pg, node) {
    ui().confirm({
      title: '删除分镜',
      message: '确定删除该分镜及其出图任务？此操作不可恢复。',
      confirmLabel: '删除',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      var req = api().del('/api/comic/pages/' + pg.id);
      var p = node ? ui().withBusy(node, '删除中…', req) : req;
      return p.then(function () {
        ui().toastSuccess('已删除分镜《' + (pg.title || ('#' + pg.id)) + '》');
        safeRender(renderChapter, '分镜已删除，但列表刷新失败');
      }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
    });
  }

  /**
   * 重绘列表并兜住异常。
   *
   * 数据已经改成功了、只是重绘失败时，最坏的结果是「界面停在旧数据上且一声不响」——
   * 用户会以为操作无效。这里保证至少有一条可见提示，方便判断是数据问题还是渲染问题。
   */
  function safeRender(render, failureMessage) {
    try {
      render();
    } catch (err) {
      if (window.console && typeof window.console.error === 'function') {
        window.console.error('[comic render failed]', err);
      }
      ui().toastError(failureMessage + '：' + ui().errorText(err, '未知错误'));
    }
  }

  /* ---------------------------------------------------------- 布局组件 */

  function breadcrumb(items) {
    var wrap = ui().el('div', 'control-row');
    wrap.style.margin = '0 0 14px';
    items.forEach(function (item, idx) {
      if (idx > 0) {
        var sep = ui().el('span', 'text-tertiary', ' / ');
        wrap.appendChild(sep);
      }
      if (item.onClick) {
        var b = ui().el('button', 'btn btn-ghost btn-sm', item.label);
        b.type = 'button';
        b.addEventListener('click', item.onClick);
        wrap.appendChild(b);
      } else {
        wrap.appendChild(ui().el('span', 'text-secondary', item.label));
      }
    });
    return wrap;
  }

  function sectionTitle(text) {
    var h = ui().el('h3', 'text-section');
    h.style.margin = '18px 0 10px';
    h.textContent = text;
    return h;
  }

  /* ---------------------------------------------------------- 角色面板刷新（供演员库「从演员库导入」后回调） */

  function refreshCharacters() {
    if (state.charactersHost && state.project) loadCharacters(state.charactersHost, state.project);
  }

  /* ---------------------------------------------------------- 导出 */

  AIBAR.comic = {
    init: init,
    onEnter: onEnter,
    refreshCharacters: refreshCharacters
  };
})(typeof window !== 'undefined' ? window : globalThis);
