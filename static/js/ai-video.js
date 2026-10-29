/* ============================================================
   AIBAR · AI 视频工作台
   项目、L0-L5 阶段、资产、场景与镜头的一站式管理。
   原生 JavaScript / IIFE；纯函数可在 node:test 中直接加载。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;
  var STAGES = ['L0', 'L1', 'L2', 'L3', 'L4', 'L5'];
  var STAGE_LABELS = ['剧本分镜', '资产层', '生成层', '组装层', '质检回流', '工程记录'];
  var STATUS_LABELS = {
    pending: '待处理', ready: '已就绪', generating: '生成中', generated: '已生成',
    lip_sync: '口型中', review: '待质检', approved: '已通过', failed: '失败'
  };
  var ASSET_TYPE_LABELS = {
    character: '角色', scene: '场景', prop: '道具', wardrobe: '造型',
    voice: '音色', style: '风格', music: '音乐', sfx: '音效'
  };
  var ASSET_STATUS_LABELS = {
    draft: '草稿', generating: '生成中', ready: '已就绪',
    locked: '已锁定', failed: '生成失败', archived: '已归档'
  };
  var SHOT_TYPE_LABELS = {
    insert_prop: '道具空镜', reaction_cu: '反应特写', ots: '过肩镜头',
    speak_cu: '正脸说话', establishing: '定场镜头', silhouette: '侧影/背影',
    two_shot: '双人同框', tracking_back: '跟拍背影', mirror: '镜面镜头',
    empty: '环境空镜', wide: '大全景', other: '其他'
  };
  var SHOT_STATUS_LABELS = {
    pending: '待处理', tts_ready: '配音就绪', frame_draft: '首帧草稿',
    frame_approved: '首帧定稿', video_draft: '视频草稿', auto_qc: '自动质检',
    human_review: '人工复核', approved: '已通过', hq_render: '高清重渲',
    done: '已完成', rejected: '已拒绝', needs_human: '需人工'
  };
  var PROJECT_STATUS_LABELS = {
    planning: '规划中', asset_building: '建资产', production: '制作中',
    review: '审片', done: '已完成', paused: '已暂停'
  };
  var PROJECT_FORMAT_LABELS = {
    talking_head: '口播', short_drama: '短剧', product_ad: '产品广告', batch_content: '批量素材'
  };
  var VISUAL_STYLE_LABELS = {
    realistic: '真人写实', anime: '动漫', '3d': '3D', illustration: '插画'
  };

  var state = {
    ready: false,
    meta: {},
    projects: [],
    projectId: null,
    dashboard: null,
    workflows: [],
    collapsedScenes: {},
    viewer: null,
    frameTimers: {},
    statusFilter: 'all',
    lipsyncFilter: 'all',
    assetTimer: null,
    busy: false
  };

  function api() { return AIBAR.api; }
  function ui() { return AIBAR.ui; }
  function host() { return doc ? doc.getElementById('ai-video-root') : null; }

  function normalizeShotStatus(value) {
    var key = String(value === null || value === undefined ? '' : value)
      .trim().toLowerCase().replace(/[\s-]+/g, '_');
    var aliases = {
      '': 'pending', todo: 'pending', draft: 'pending', queued: 'pending',
      complete: 'generated', completed: 'generated', done: 'generated', success: 'generated',
      running: 'generating', processing: 'generating', render: 'generating', rendering: 'generating',
      lipsync: 'lip_sync', lip_syncing: 'lip_sync', syncing: 'lip_sync',
      tts_ready: 'ready', frame_draft: 'generating', frame_approved: 'ready',
      video_draft: 'generated', auto_qc: 'review', human_review: 'review', needs_human: 'review',
      hq_render: 'approved', rejected: 'failed',
      qa: 'review', qc: 'review', reviewing: 'review',
      pass: 'approved', passed: 'approved', accepted: 'approved',
      error: 'failed'
    };
    return aliases[key] || (STATUS_LABELS[key] ? key : 'pending');
  }

  function truthy(value) {
    return value === true || value === 1 || value === '1' || value === 'true' || value === 'yes';
  }

  function deriveProjectStage(project, scenes, shots, assets) {
    var list = Array.isArray(shots) ? shots : [];
    var assetList = Array.isArray(assets) ? assets : [];
    if (!Array.isArray(scenes) || !scenes.length) return 0;
    if (String(project && project.status) === 'done') return 5;
    if (String(project && project.status) === 'review') return 4;
    var readyAssets = assetList.filter(function (item) {
      return item.status === 'ready' || item.status === 'locked';
    }).length;
    if (!assetList.length || readyAssets < assetList.length) return 1;
    if (!list.length) return 2;
    var done = list.filter(function (item) { return item.status === 'done'; }).length;
    if (done === list.length) return 5;
    var approved = list.filter(function (item) {
      return ['approved', 'hq_render', 'done'].indexOf(item.status) >= 0;
    }).length;
    if (approved === list.length) return 4;
    if (approved > 0) return 3;
    return 2;
  }

  function filterShots(shots, filters) {
    var opts = filters || {};
    var wantedStatus = opts.status || 'all';
    var wantedLip = opts.lipsync || opts.lipSync || 'all';
    return (Array.isArray(shots) ? shots : []).filter(function (shot) {
      if (wantedStatus !== 'all') {
        var shotStatus = shot && shot.status;
        if (opts.exact ? String(shotStatus) !== String(wantedStatus) : normalizeShotStatus(shotStatus) !== wantedStatus) return false;
      }
      var lip = truthy(shot && (shot.lip_sync_required !== undefined ? shot.lip_sync_required : shot.lipsync));
      if (wantedLip === 'yes' && !lip) return false;
      if (wantedLip === 'no' && lip) return false;
      return true;
    });
  }

  function progressModel(input) {
    var data = input || {};
    var raw = data.stage !== undefined ? data.stage : (data.current_stage !== undefined ? data.current_stage : data.progress_stage);
    var index = 0;
    if (typeof raw === 'number' && isFinite(raw)) index = Math.max(0, Math.min(5, Math.floor(raw)));
    else {
      var match = String(raw || 'L0').toUpperCase().match(/L?([0-5])/);
      index = match ? Number(match[1]) : 0;
    }
    var shots = Array.isArray(data.shots) ? data.shots : [];
    var approved = shots.filter(function (shot) { return normalizeShotStatus(shot.status) === 'approved'; }).length;
    var percent = data.progress_percent !== undefined ? Number(data.progress_percent) : (shots.length ? Math.round(approved / shots.length * 100) : index * 20);
    if (!isFinite(percent)) percent = 0;
    percent = Math.max(0, Math.min(100, Math.round(percent)));
    return {
      current: 'L' + index,
      index: index,
      percent: percent,
      steps: STAGES.map(function (key, i) {
        return { key: key, label: STAGE_LABELS[i], state: i < index ? 'done' : (i === index ? 'active' : 'todo') };
      })
    };
  }

  function listOf(data, keys) {
    var source = data || {};
    for (var i = 0; i < keys.length; i += 1) {
      if (Array.isArray(source[keys[i]])) return source[keys[i]];
    }
    return Array.isArray(data) ? data : [];
  }

  function assetTypeLabel(value) {
    return ASSET_TYPE_LABELS[value] || value || '资产';
  }

  function assetStatusLabel(value) {
    return ASSET_STATUS_LABELS[value] || value || '草稿';
  }

  function projectStatusLabel(value) {
    return PROJECT_STATUS_LABELS[value] || value || '进行中';
  }

  function shotTypeLabel(value) {
    return SHOT_TYPE_LABELS[value] || value || '镜头';
  }

  function shotStatusLabel(value) {
    return SHOT_STATUS_LABELS[value] || value || '待处理';
  }

  function workflowName(filename) {
    var key = String(filename || '');
    for (var i = 0; i < state.workflows.length; i += 1) {
      if (String(state.workflows[i].filename) === key) return state.workflows[i].name || key;
    }
    return key;
  }

  function workflowChoices(selected) {
    var choices = [['', '沿用项目默认']];
    var ready = state.workflows.filter(function (item) { return item.character_ready !== false; });
    ready.forEach(function (item) {
      choices.push([item.filename, (item.name || item.filename) + (item.node_count ? ' · ' + item.node_count + ' 节点' : '')]);
    });
    if (selected && !ready.some(function (item) { return String(item.filename) === String(selected); })) {
      choices.push([selected, selected + '（当前，不可直接用于角色）']);
    }
    return choices;
  }

  function e(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function button(label, cls, handler) {
    var node = e('button', 'btn btn-sm ' + (cls || 'btn-secondary'), label);
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }

  function inputField(label, name, value, type) {
    var wrap = e('label', 'ai-video-field');
    wrap.appendChild(e('span', 'field-label', label));
    var control;
    if (type === 'textarea') {
      control = e('textarea', 'textarea ai-video-textarea');
      control.rows = 3;
    } else {
      control = e('input', 'input');
      control.type = type || 'text';
    }
    control.name = name;
    if (type === 'checkbox') control.checked = !!value;
    else control.value = value === undefined || value === null ? '' : String(value);
    wrap.appendChild(control);
    return wrap;
  }

  function selectField(label, name, value, choices) {
    var wrap = e('label', 'ai-video-field');
    wrap.appendChild(e('span', 'field-label', label));
    var select = e('select', 'select');
    select.name = name;
    (choices || []).forEach(function (choice) {
      var pair = typeof choice === 'string' ? [choice, choice] : choice;
      var option = e('option', null, pair[1]);
      option.value = pair[0];
      option.selected = String(pair[0]) === String(value === undefined || value === null ? '' : value);
      select.appendChild(option);
    });
    wrap.appendChild(select);
    return wrap;
  }

  function toastError(err, fallback) {
    ui().toastError(ui().errorText(err, fallback));
  }

  function setBusy(busy) {
    state.busy = !!busy;
    var root_ = host();
    if (root_) root_.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  function init() {
    if (!host()) return;
    state.ready = true;
  }

  function onEnter(tab) {
    if (tab !== 'ai-video' || !host()) return;
    if (!host().dataset.ready) {
      host().dataset.ready = '1';
      renderShell();
      loadMetaAndProjects();
    } else {
      refresh();
    }
  }

  function refresh() {
    if (!host()) return Promise.resolve();
    return loadProjects(state.projectId).then(function () {
      if (state.projectId) return loadDashboard(state.projectId);
      render();
    });
  }

  function renderShell() {
    var root_ = host();
    root_.innerHTML = '';
    var shell = e('div', 'ai-video-workbench');
    var sidebar = e('aside', 'ai-video-projects');
    sidebar.id = 'ai-video-projects';
    var main = e('div', 'ai-video-main');
    main.id = 'ai-video-main';
    shell.appendChild(sidebar);
    shell.appendChild(main);
    root_.appendChild(shell);
  }

  function loadMetaAndProjects() {
    setBusy(true);
    return Promise.all([
      api().get('/api/ai-video/meta', {}).catch(function () { return {}; }),
      api().get('/api/ai-video/projects', {}),
      api().get('/api/ai-video/workflows', {}).catch(function () { return { items: [] }; })
    ]).then(function (values) {
      state.meta = values[0] || {};
      state.projects = listOf(values[1], ['items', 'projects']);
      state.workflows = listOf(values[2], ['items']);
      if (!state.projectId && state.projects.length) state.projectId = state.projects[0].id;
      setBusy(false);
      render();
      if (state.projectId) return loadDashboard(state.projectId);
    }, function (err) {
      setBusy(false);
      renderError(err, '加载 AI 视频项目失败');
    });
  }

  function loadProjects(preferredId) {
    return api().get('/api/ai-video/projects', {}).then(function (data) {
      state.projects = listOf(data, ['items', 'projects']);
      var exists = state.projects.some(function (project) { return String(project.id) === String(preferredId); });
      state.projectId = exists ? preferredId : (state.projects[0] ? state.projects[0].id : null);
      renderProjectRail();
    });
  }

  function closeEditors() {
    var main = doc.getElementById('ai-video-main');
    if (main) {
      Array.prototype.forEach.call(main.querySelectorAll('.ai-video-editor'), function (node) {
        node.remove();
      });
    }
    closeAssetViewer();
  }

  function loadDashboard(projectId) {
    if (!projectId) return Promise.resolve();
    // 切换项目时才关闭详情；同一项目的后台刷新不能把用户正在编辑的抽屉带走。
    if (state.projectId && String(state.projectId) !== String(projectId)) closeEditors();
    setBusy(true);
    return api().get('/api/ai-video/projects/' + encodeURIComponent(projectId) + '/dashboard', {}).then(function (data) {
      state.dashboard = data || {};
      state.projectId = projectId;
      setBusy(false);
      render();
    }, function (err) {
      setBusy(false);
      renderMainError(err, '加载项目看板失败');
    });
  }

  function renderError(err, fallback) {
    renderProjectRail();
    renderMainError(err, fallback);
  }

  function renderMainError(err, fallback) {
    var main = doc.getElementById('ai-video-main');
    if (!main) return;
    main.innerHTML = '';
    main.appendChild(ui().errorState(ui().errorText(err, fallback), function () { refresh(); }));
  }

  function render() {
    renderProjectRail();
    renderMain();
  }

  function renderProjectRail() {
    var rail = doc.getElementById('ai-video-projects');
    if (!rail) return;
    rail.innerHTML = '';
    var head = e('div', 'ai-video-section-head');
    var heading = e('div');
    heading.appendChild(e('h2', 'ai-video-section-title', '项目'));
    heading.appendChild(e('div', 'text-tertiary', state.projects.length + ' 个项目'));
    head.appendChild(heading);
    head.appendChild(button('新建', 'btn-primary', openCreateProject));
    rail.appendChild(head);

    var list = e('div', 'ai-video-project-list');
    state.projects.forEach(function (project) {
      var item = e('button', 'ai-video-project-item' + (String(project.id) === String(state.projectId) ? ' is-active' : ''));
      item.type = 'button';
      item.appendChild(e('strong', null, project.name || ('项目 #' + project.id)));
      item.appendChild(e('span', null, (project.automation_level || 'L1') + ' 自动化 · ' + projectStatusLabel(project.status)));
      item.addEventListener('click', function () { loadDashboard(project.id); });
      list.appendChild(item);
    });
    if (!state.projects.length) list.appendChild(e('p', 'ai-video-empty-copy', '还没有项目，先创建一个短剧项目。'));
    rail.appendChild(list);
  }

  function renderMain() {
    var main = doc.getElementById('ai-video-main');
    if (!main) return;
    var openEditor = main.querySelector('.ai-video-editor');
    if (openEditor) openEditor.remove();
    main.innerHTML = '';
    if (!state.projectId || !state.dashboard) {
      main.appendChild(ui().emptyState({ icon: 'film', title: '开始第一个 AI 视频项目', desc: '创建项目后可载入 28 镜模板，继续完善资产、场景与镜头。' }));
      if (openEditor) main.appendChild(openEditor);
      return;
    }
    var dashboard = state.dashboard;
    var project = dashboard.project || state.projects.filter(function (p) { return String(p.id) === String(state.projectId); })[0] || {};
    var scenes = listOf(dashboard, ['scenes']);
    var assets = listOf(dashboard, ['assets']);
    var shots = listOf(dashboard, ['shots']);
    if (!shots.length) scenes.forEach(function (scene) { shots = shots.concat(listOf(scene, ['shots'])); });

    main.appendChild(renderHero(project, dashboard, shots));
    main.appendChild(renderStages(project, dashboard, scenes, shots, assets));
    main.appendChild(renderStats(dashboard, scenes, shots, assets));
    main.appendChild(renderAssets(assets));
    main.appendChild(renderShotsSection(scenes, shots));
    if (openEditor) main.appendChild(openEditor);
    shots.forEach(function (shot) {
      ['start', 'end'].forEach(function (kind) {
        if (frameStatus(shot, kind).status === 'generating') startFramePolling(shot.id, kind);
      });
    });
  }

  function renderHero(project) {
    var hero = e('section', 'ai-video-hero');
    var copy = e('div', 'ai-video-hero-copy');
    copy.appendChild(e('div', 'ai-video-kicker', [
      PROJECT_FORMAT_LABELS[project.video_format] || project.video_format || 'AI VIDEO',
      VISUAL_STYLE_LABELS[project.visual_style] || project.visual_style || '真人写实',
      project.aspect_ratio || '9:16'
    ].join(' · ')));
    copy.appendChild(e('h2', 'ai-video-title', project.name || ('项目 #' + state.projectId)));
    copy.appendChild(e('p', 'ai-video-summary', project.notes || '集中管理视频项目的资产、场景、镜头生成和质检。'));
    hero.appendChild(copy);
    var actions = e('div', 'ai-video-hero-actions');
    actions.appendChild(button('载入真人写实短剧 28 镜模板', 'btn-primary', seedTemplate));
    actions.appendChild(button('出图工作流', 'btn-secondary', openWorkflowManager));
    actions.appendChild(button('编辑项目', 'btn-secondary', function () { openEditProject(project); }));
    actions.appendChild(button('删除', 'btn-danger', deleteProject));
    hero.appendChild(actions);
    return hero;
  }

  function renderStages(project, dashboard, scenes, shots, assets) {
    var stats = dashboard.stats || {};
    var progress = Number(stats.progress) || 0;
    var stageIndex = deriveProjectStage(project, scenes, shots, assets);
    var model = progressModel({
      stage: stageIndex,
      progress_percent: progress,
      shots: shots
    });
    var readyAssets = assets.filter(function (item) {
      return item.status === 'ready' || item.status === 'locked';
    }).length;
    var generated = shots.filter(function (item) {
      return ['frame_approved', 'video_draft', 'auto_qc', 'human_review', 'approved', 'hq_render', 'done'].indexOf(item.status) >= 0;
    }).length;
    var approved = shots.filter(function (item) {
      return ['approved', 'hq_render', 'done'].indexOf(item.status) >= 0;
    }).length;
    var doneCount = shots.filter(function (item) { return item.status === 'done'; }).length;
    var details = [
      scenes.length + ' 个场景 · ' + shots.length + ' 个镜头',
      readyAssets + '/' + assets.length + ' 资产就绪',
      generated + '/' + shots.length + ' 镜头已出片',
      approved + '/' + shots.length + ' 镜头已通过',
      (stats.qc_progress || 0) + '% 质检项完成',
      (shots.length && doneCount === shots.length) ? '全部镜头已完成，可复盘' : '待全部镜头完成'
    ];
    var section = e('section', 'ai-video-stage-panel');
    var top = e('div', 'ai-video-stage-top');
    top.appendChild(e('strong', null, '制作阶段'));
    top.appendChild(e('span', 'text-tertiary', '当前 ' + model.steps[stageIndex].key + ' · ' + model.steps[stageIndex].label + ' · 镜头完成 ' + progress + '%'));
    section.appendChild(top);
    var bar = e('div', 'ai-video-progress');
    var fill = e('span');
    fill.style.width = Math.max(2, Math.min(100, progress)) + '%';
    bar.appendChild(fill);
    section.appendChild(bar);
    var stages = e('ol', 'ai-video-stages');
    model.steps.forEach(function (step, index) {
      var state = index < stageIndex ? 'done' : (index === stageIndex ? 'active' : 'todo');
      var item = e('li', 'ai-video-stage is-' + state);
      item.appendChild(e('span', 'ai-video-stage-dot', step.key));
      item.appendChild(e('span', null, step.label));
      item.appendChild(e('small', null, details[index]));
      stages.appendChild(item);
    });
    section.appendChild(stages);
    return section;
  }

  function renderStats(dashboard, scenes, shots, assets) {
    var stats = dashboard.stats || dashboard.statistics || {};
    var approved = shots.filter(function (shot) { return normalizeShotStatus(shot.status) === 'approved'; }).length;
    var lipsync = shots.filter(function (shot) { return truthy(shot.lip_sync_required !== undefined ? shot.lip_sync_required : shot.lipsync); }).length;
    var values = [
      ['场景', stats.scene_count !== undefined ? stats.scene_count : scenes.length],
      ['镜头', stats.shot_count !== undefined ? stats.shot_count : shots.length],
      ['需口型', stats.lipsync_count !== undefined ? stats.lipsync_count : lipsync],
      ['质检通过', stats.approved_count !== undefined ? stats.approved_count : approved],
      ['资产', assets.length]
    ];
    var grid = e('section', 'ai-video-stats');
    values.forEach(function (pair) {
      var card = e('div', 'ai-video-stat');
      card.appendChild(e('strong', null, pair[1]));
      card.appendChild(e('span', null, pair[0]));
      grid.appendChild(card);
    });
    return grid;
  }

  function renderAssets(assets) {
    var section = e('section', 'ai-video-section');
    var ready = assets.filter(function (item) { return item.status === 'ready' || item.status === 'locked'; }).length;
    var missing = assets.filter(function (item) { return !item.image_path; }).length;
    var head = e('div', 'ai-video-section-head');
    var heading = e('div');
    heading.appendChild(e('h3', 'ai-video-section-title', '资产层'));
    heading.appendChild(e('div', 'text-tertiary', assets.length + ' 个资产 · ' + ready + ' 就绪 · ' + missing + ' 缺图'));
    var defaultWorkflow = (state.dashboard && state.dashboard.project && state.dashboard.project.default_workflow) || '';
    if (defaultWorkflow) heading.appendChild(e('div', 'text-tertiary', '默认工作流：' + workflowName(defaultWorkflow)));
    head.appendChild(heading);
    var actions = e('div', 'ai-video-filters');
    actions.appendChild(button('出图工作流', 'btn-ghost', openWorkflowManager));
    actions.appendChild(button('添加资产', 'btn-secondary', openCreateAsset));
    head.appendChild(actions);
    section.appendChild(head);
    var grid = e('div', 'ai-video-assets');
    assets.forEach(function (asset) {
      var generating = asset.status === 'generating';
      var card = e('article', 'ai-video-asset-card');
      var cover = e('div', 'ai-video-asset-cover');
      if (asset.image_url) {
        var img = e('img', 'ai-video-thumb-clickable');
        img.src = asset.image_url;
        img.alt = (asset.name || '资产') + ' 图片';
        img.loading = 'lazy';
        img.title = '点击查看全图';
        img.tabIndex = 0;
        img.setAttribute('role', 'button');
        img.setAttribute('aria-label', '查看 ' + (asset.name || '资产') + ' 全图');
        img.addEventListener('click', function () { openAssetViewerForAsset(asset); });
        img.addEventListener('keydown', function (event) {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openAssetViewerForAsset(asset);
          }
        });
        cover.appendChild(img);
      } else {
        cover.appendChild(e('span', 'ai-video-asset-initial', assetTypeLabel(asset.asset_type).slice(0, 1)));
        cover.appendChild(e('small', null, generating ? '生成中…' : '暂无图片'));
      }
      cover.appendChild(e('span', 'ai-video-asset-status is-' + (asset.status || 'draft'), assetStatusLabel(asset.status)));
      card.appendChild(cover);
      var body = e('div', 'ai-video-asset-body');
      body.appendChild(e('strong', null, asset.name || ('资产 #' + asset.id)));
      body.appendChild(e('span', 'ai-video-asset-type', assetTypeLabel(asset.asset_type) + ' · ' + (asset.asset_key || '')));
      if (asset.description) body.appendChild(e('p', 'ai-video-asset-desc', asset.description));
      var meta = [];
      var setProgress = asset.metadata && asset.metadata.set_progress;
      if (asset.status === 'generating' && setProgress && setProgress.total) {
        meta.push('生成 ' + (setProgress.completed || 0) + '/' + setProgress.total + (setProgress.current_label ? ' · ' + setProgress.current_label : ''));
      }
      if (asset.workflow) meta.push('工作流 ' + workflowName(asset.workflow));
      if (asset.seed !== null && asset.seed !== undefined && asset.seed !== '') meta.push('种子 ' + asset.seed);
      if (asset.metadata && asset.metadata.width) meta.push(asset.metadata.width + '×' + asset.metadata.height);
      if (asset.image_count > 1) meta.push(asset.image_count + ' 图');
      if (meta.length) body.appendChild(e('span', 'ai-video-asset-meta', meta.join(' · ')));
      if (asset.error_message) {
        body.appendChild(e('p', asset.status === 'failed' ? 'ai-video-asset-error' : 'ai-video-asset-warning', asset.error_message));
      }
      card.appendChild(body);
      var cardActions = e('div', 'ai-video-asset-actions');
      var genBtn = button(generating ? '生成中…' : (asset.image_url ? '重新生成' : '生成图片'), generating ? 'btn-secondary' : 'btn-primary', function () {
        if (!generating) quickGenerateAsset(asset);
      });
      genBtn.disabled = generating;
      cardActions.appendChild(genBtn);
      cardActions.appendChild(button('详情', 'btn-ghost', function () { openAssetEditor(asset); }));
      card.appendChild(cardActions);
      grid.appendChild(card);
    });
    if (!assets.length) grid.appendChild(e('p', 'ai-video-empty-copy', '暂无角色、场景、声音或参考资产。'));
    section.appendChild(grid);
    return section;
  }

  function renderShotsSection(scenes, flatShots) {
    var section = e('section', 'ai-video-section');
    var head = e('div', 'ai-video-section-head ai-video-shots-head');
    head.appendChild(e('h3', 'ai-video-section-title', '场景与镜头'));
    var controls = e('div', 'ai-video-filters');
    var statusKeys = listOf(state.meta, ['shot_statuses']);
    if (!statusKeys.length) statusKeys = ['pending', 'tts_ready', 'frame_draft', 'frame_approved', 'video_draft', 'auto_qc', 'human_review', 'approved', 'hq_render', 'done', 'rejected', 'needs_human'];
    var status = selectField('状态', 'status-filter', state.statusFilter, [['all', '全部状态']].concat(statusKeys.map(function (key) { return [key, shotStatusLabel(key)]; })));
    status.querySelector('select').addEventListener('change', function (event) { state.statusFilter = event.target.value; renderMain(); });
    controls.appendChild(status);
    var lip = selectField('口型', 'lip-filter', state.lipsyncFilter, [['all', '全部镜头'], ['yes', '需要口型'], ['no', '无需口型']]);
    lip.querySelector('select').addEventListener('change', function (event) { state.lipsyncFilter = event.target.value; renderMain(); });
    controls.appendChild(lip);
    controls.appendChild(button('全部收起', 'btn-ghost', function () {
      listOf(state.dashboard, ['scenes']).forEach(function (item) { state.collapsedScenes[String(item.id)] = true; });
      renderMain();
    }));
    controls.appendChild(button('全部展开', 'btn-ghost', function () {
      state.collapsedScenes = {};
      renderMain();
    }));
    controls.appendChild(button('新增场景', 'btn-secondary', openCreateScene));
    head.appendChild(controls);
    section.appendChild(head);

    var groups = e('div', 'ai-video-scene-list');
    var sceneList = scenes.slice();
    if (!sceneList.length && flatShots.length) sceneList = [{ id: null, name: '未分组镜头', shots: flatShots }];
    sceneList.forEach(function (scene, sceneIndex) {
      var all = listOf(scene, ['shots']);
      if (!all.length) all = flatShots.filter(function (shot) { return String(shot.scene_id) === String(scene.id); });
      var filtered = filterShots(all, { status: state.statusFilter, lipsync: state.lipsyncFilter, exact: true });
      var group = e('article', 'ai-video-scene');
      var sceneHead = e('header', 'ai-video-scene-head');
      var title = e('div');
      title.appendChild(e('h4', null, (scene.scene_code || scene.order_idx || sceneIndex + 1) + ' · ' + (scene.title || scene.name || '未命名场景')));
      title.appendChild(e('p', 'text-tertiary', [scene.location, scene.time_of_day, scene.axis_rule].filter(Boolean).join(' · ') || (all.length + ' 个镜头')));
      sceneHead.appendChild(title);
      var sceneKey = String(scene.id === null || scene.id === undefined ? ('scene-' + sceneIndex) : scene.id);
      var collapsed = !!state.collapsedScenes[sceneKey];
      var sceneActions = e('div', 'ai-video-scene-actions');
      sceneActions.appendChild(e('span', 'text-tertiary', filtered.length + '/' + all.length + ' 镜'));
      sceneActions.appendChild(button(collapsed ? '展开' : '收起', 'btn-ghost', function () {
        state.collapsedScenes[sceneKey] = !collapsed;
        renderMain();
      }));
      if (scene.id !== null && scene.id !== undefined) {
        sceneActions.appendChild(button('新增镜头', 'btn-ghost', function () { openCreateShot(scene); }));
      }
      sceneHead.appendChild(sceneActions);
      group.appendChild(sceneHead);
      if (!collapsed) {
        var cards = e('div', 'ai-video-shot-grid');
        filtered.forEach(function (shot) { cards.appendChild(renderShotCard(shot)); });
        if (!filtered.length) cards.appendChild(e('p', 'ai-video-empty-copy', all.length ? '当前筛选下没有镜头。' : '此场景还没有镜头。'));
        group.appendChild(cards);
      }
      groups.appendChild(group);
    });
    if (!sceneList.length) groups.appendChild(e('p', 'ai-video-empty-copy', '暂无场景。可新增场景，或一键载入 28 镜模板。'));
    section.appendChild(groups);
    return section;
  }

  function renderShotCard(shot) {
    var status = normalizeShotStatus(shot.status);
    var card = e('article', 'ai-video-shot-card');
    var head = e('div', 'ai-video-shot-head');
    head.appendChild(e('strong', null, (shot.shot_code || shot.code || shot.shot_no || ('#' + shot.id)) + ' · ' + shotTypeLabel(shot.shot_type || shot.type)));
    head.appendChild(e('span', 'ai-video-status is-' + status, shotStatusLabel(shot.status)));
    card.appendChild(head);
    var meta = e('div', 'ai-video-shot-meta');
    meta.appendChild(e('span', null, (shot.duration || shot.duration_seconds || 0) + 's'));
    meta.appendChild(e('span', null, '尝试 ' + (shot.attempts || shot.attempt_count || 0) + ' 次'));
    if (truthy(shot.lipsync !== undefined ? shot.lipsync : shot.lip_sync_required)) meta.appendChild(e('span', 'is-lipsync', '需口型'));
    card.appendChild(meta);
    if (shot.dialogue) card.appendChild(labelledText('台词', shot.dialogue));
    if (shot.prompt_image || shot.visual_prompt) card.appendChild(labelledText('画面', shot.prompt_image || shot.visual_prompt));
    if (shot.prompt_motion || shot.motion_prompt) card.appendChild(labelledText('运动', shot.prompt_motion || shot.motion_prompt));
    var frames = e('div', 'ai-video-frame-row');
    frames.appendChild(framePreview('首帧', shot.first_frame_url, shot, 'start'));
    frames.appendChild(framePreview('尾帧', shot.last_frame_url, shot, 'end'));
    card.appendChild(frames);
    var qcData = shot.qc || {};
    var qc = e('div', 'ai-video-qc');
    [['人脸', qcData.face_consistency], ['肢体', qcData.hands_anatomy], ['口型', qcData.mouth_quality || qcData.audio_sync], ['时长', qcData.duration_match]].forEach(function (pair) {
      var mark = e('span', truthy(pair[1]) ? 'is-pass' : '', (truthy(pair[1]) ? '✓ ' : '○ ') + pair[0]);
      qc.appendChild(mark);
    });
    card.appendChild(qc);
    card.appendChild(button('编辑镜头', 'btn-secondary', function () { openShotEditor(shot); }));
    return card;
  }

  function labelledText(label, value) {
    var block = e('p', 'ai-video-shot-copy');
    block.appendChild(e('b', null, label + '：'));
    block.appendChild(doc.createTextNode(String(value)));
    return block;
  }

  function loadFrameCandidates() {
    var assets = listOf(state.dashboard, ['assets']).filter(function (item) { return item.image_count > 0; });
    return Promise.all(assets.map(function (asset) {
      return api().get('/api/ai-video/assets/' + encodeURIComponent(asset.id) + '/images', {})
        .then(function (data) { return { asset: asset, images: listOf(data, ['items']) }; })
        .catch(function () { return { asset: asset, images: [] }; });
    }));
  }

  function appendFrameGenerate(body, shot, kind, entry) {
    var isStart = kind === 'start';
    var status = frameStatus(shot, kind);
    var section = e('div', 'ai-video-frame-generate');
    if (status.status === 'generating') {
      section.appendChild(e('strong', null, '正在生成' + (isStart ? '首帧' : '尾帧') + '…'));
      section.appendChild(e('p', 'text-tertiary', '已提交 ComfyUI，完成后会自动刷新镜头卡。可以关闭此窗口继续做别的事。'));
      body.appendChild(section);
      return;
    }
    section.appendChild(e('div', 'field-label', '生成' + (isStart ? '首帧' : '尾帧')));
    var prompt = doc.createElement('textarea');
    prompt.className = 'input';
    prompt.rows = 3;
    prompt.placeholder = shot.prompt_image || '留空则按镜头类型、场景、人物和镜头参数自动拼提示词';
    section.appendChild(prompt);
    var actions = e('div', 'result-actions');
    var generate = button('开始生成' + (isStart ? '首帧' : '尾帧'), 'btn-primary', function () {
      generate.disabled = true;
      generate.textContent = '提交中…';
      api().post('/api/ai-video/shots/' + encodeURIComponent(shot.id) + '/frames/' + kind + '/generate', { prompt: prompt.value })
        .then(function () {
          entry.close();
          ui().toastSuccess('已提交生成，完成后自动刷新');
          startFramePolling(shot.id, kind);
          return loadDashboard(state.projectId);
        }, function (err) {
          generate.disabled = false;
          generate.textContent = '开始生成' + (isStart ? '首帧' : '尾帧');
          toastError(err, '提交生成失败');
        });
    });
    actions.appendChild(generate);
    section.appendChild(actions);
    section.appendChild(e('p', 'text-tertiary ai-video-field-span', '使用项目默认出图工作流；尾帧会优先沿用首帧种子，便于保持人物连续性。'));
    body.appendChild(section);
  }

  function startFramePolling(shotId, kind) {
    var key = String(shotId) + '-' + kind;
    if (state.frameTimers[key]) return;
    var started = Date.now();
    state.frameTimers[key] = setInterval(function () {
      if (Date.now() - started > 900000) {
        clearInterval(state.frameTimers[key]);
        delete state.frameTimers[key];
        return;
      }
      api().get('/api/ai-video/shots/' + encodeURIComponent(shotId), {}).then(function (shot) {
        var status = frameStatus(shot, kind);
        if (status.status === 'generating') return;
        clearInterval(state.frameTimers[key]);
        delete state.frameTimers[key];
        loadDashboard(state.projectId);
        if (status.status === 'ready') {
          ui().toastSuccess((kind === 'start' ? '首帧' : '尾帧') + '已生成');
        } else if (status.status === 'failed') {
          ui().toast({ message: (kind === 'start' ? '首帧' : '尾帧') + '生成失败：' + (status.error || '未知原因'), type: 'error' });
        }
      }, function () {});
    }, 2500);
  }

  function openFramePicker(shot, kind) {
    var isStart = kind === 'start';
    var currentRef = isStart ? shot.first_frame : shot.last_frame;
    var currentUrl = isStart ? shot.first_frame_url : shot.last_frame_url;
    var body = e('div', 'ai-video-frame-picker');
    body.appendChild(ui().loadingInline('正在加载项目角色图…'));
    var entry = ui().modal({
      title: (isStart ? '设置首帧' : '设置尾帧') + ' · ' + (shot.shot_code || ('#' + shot.id)),
      desc: '从当前项目的角色/场景图片中选择，或上传本地图片；设置后会显示在镜头卡上。',
      body: body,
      actions: [{ label: '关闭', variant: 'secondary' }]
    });

    loadFrameCandidates().then(function (groups) {
      body.innerHTML = '';
      appendFrameGenerate(body, shot, kind, entry);
      if (currentUrl) {
        var current = e('div', 'ai-video-frame-current');
        var currentImg = e('img', 'ai-video-thumb-clickable');
        currentImg.src = currentUrl;
        currentImg.alt = isStart ? '当前首帧' : '当前尾帧';
        currentImg.title = '点击查看全图';
        currentImg.addEventListener('click', function () {
          openAssetViewer([{ image_url: currentUrl, label: isStart ? '首帧' : '尾帧' }], 0, shot.shot_code || '镜头');
        });
        current.appendChild(currentImg);
        var currentInfo = e('div');
        currentInfo.appendChild(e('strong', null, '当前' + (isStart ? '首帧' : '尾帧')));
        currentInfo.appendChild(e('p', 'text-tertiary', '点击图片查看全图，或重新选择覆盖。'));
        var currentActions = e('div', 'result-actions');
        currentActions.appendChild(button('查看全图', 'btn-ghost', function () {
          openAssetViewer([{ image_url: currentUrl, label: isStart ? '首帧' : '尾帧' }], 0, shot.shot_code || '镜头');
        }));
        currentActions.appendChild(button('清除', 'btn-ghost', function () {
          api().del('/api/ai-video/shots/' + encodeURIComponent(shot.id) + '/frames/' + kind).then(function () {
            entry.close(); ui().toastSuccess('已清除'); return loadDashboard(state.projectId);
          }, function (err) { toastError(err, '清除失败'); });
        }));
        currentInfo.appendChild(currentActions);
        current.appendChild(currentInfo);
        body.appendChild(current);
      }

      var candidateTitle = e('div', 'field-label', '从项目图片选择');
      body.appendChild(candidateTitle);
      var grid = e('div', 'ai-video-frame-candidates');
      groups.forEach(function (group) {
        group.images.forEach(function (image) {
          var card = e('button', 'ai-video-frame-candidate');
          card.type = 'button';
          var img = e('img');
          img.src = image.image_url;
          img.alt = (group.asset.name || '') + ' ' + (image.label || '');
          img.loading = 'lazy';
          card.appendChild(img);
          card.appendChild(e('span', null, (group.asset.name || '') + ' · ' + (image.label || '未命名')));
          card.addEventListener('click', function () {
            api().post('/api/ai-video/shots/' + encodeURIComponent(shot.id) + '/frames/' + kind + '/asset', { image_id: image.id })
              .then(function () {
                entry.close();
                ui().toastSuccess('已设置' + (isStart ? '首帧' : '尾帧'));
                return loadDashboard(state.projectId);
              }, function (err) { toastError(err, '设置失败'); });
          });
          grid.appendChild(card);
        });
      });
      if (!grid.childNodes.length) grid.appendChild(e('p', 'ai-video-empty-copy', '当前项目还没有角色/场景图片。'));
      body.appendChild(grid);

      var uploadWrap = e('div', 'ai-video-frame-upload');
      uploadWrap.appendChild(e('div', 'field-label', '上传本地图片'));
      var file = doc.createElement('input');
      file.type = 'file';
      file.accept = 'image/png,image/jpeg,image/webp';
      file.className = 'input';
      file.addEventListener('change', function () {
        if (!file.files || !file.files.length) return;
        var form = new FormData();
        form.append('file', file.files[0]);
        api().upload('/api/ai-video/shots/' + encodeURIComponent(shot.id) + '/frames/' + kind + '/upload', form, { timeout: 60000 })
          .then(function () {
            entry.close();
            ui().toastSuccess('已上传并设置' + (isStart ? '首帧' : '尾帧'));
            return loadDashboard(state.projectId);
          }, function (err) { toastError(err, '上传失败'); });
      });
      uploadWrap.appendChild(file);
      body.appendChild(uploadWrap);
    }, function (err) {
      body.innerHTML = '';
      body.appendChild(ui().errorState(ui().errorText(err, '加载项目图片失败'), function () { openFramePicker(shot, kind); }));
    });
  }

  function frameStatus(shot, kind) {
    return ((shot && shot.frame_status) || {})[kind] || {};
  }

  function framePreview(label, url, shot, kind) {
    var status = frameStatus(shot, kind);
    var generating = status.status === 'generating';
    var failed = status.status === 'failed';
    var box = e('button', 'ai-video-frame' + (url ? ' has-image' : '') + (generating ? ' is-generating' : '') + (failed ? ' is-failed' : ''));
    box.type = 'button';
    box.title = generating ? '正在生成' + label : (url ? '点击查看/更换' : '点击设置或生成' + label);
    box.appendChild(e('span', null, label));
    if (url) {
      var img = e('img');
      img.src = url;
      img.alt = label;
      img.loading = 'lazy';
      box.appendChild(img);
      box.appendChild(e('small', null, generating ? '生成中…' : '更换'));
    } else if (generating) {
      box.appendChild(e('small', null, '生成中…'));
    } else if (failed) {
      box.appendChild(e('small', null, '生成失败，点击重试'));
    } else {
      box.appendChild(e('small', null, '点击设置 / 生成'));
    }
    box.disabled = generating;
    box.addEventListener('click', function () { openFramePicker(shot, kind); });
    return box;
  }

  function collectForm(form) {
    var values = {};
    Array.prototype.forEach.call(form.elements, function (control) {
      if (!control.name) return;
      values[control.name] = control.type === 'checkbox' ? control.checked : control.value;
    });
    return values;
  }

  function openForm(title, fields, submitLabel, onSubmit, dangerAction, extraActions) {
    var main = doc.getElementById('ai-video-main');
    var old = main.querySelector('.ai-video-editor');
    if (old) old.remove();
    var overlay = e('div', 'ai-video-editor');
    var panel = e('section', 'ai-video-editor-panel');
    var head = e('header', 'ai-video-editor-head');
    head.appendChild(e('h3', null, title));
    head.appendChild(button('关闭', 'btn-ghost', function () { overlay.remove(); }));
    panel.appendChild(head);
    var form = e('form', 'ai-video-form');
    fields.forEach(function (field) { form.appendChild(field); });
    var actions = e('div', 'ai-video-form-actions');
    (extraActions || []).forEach(function (action) {
      var btn = button(action.label, action.variant || 'btn-secondary', function () {
        var result = action.onClick ? action.onClick(collectForm(form), form, overlay) : null;
        if (result && typeof result.then === 'function') {
          ui().setBusy(btn, true);
          result.then(function () { ui().setBusy(btn, false); }, function () { ui().setBusy(btn, false); });
        }
      });
      actions.appendChild(btn);
    });
    if (dangerAction) actions.appendChild(button(dangerAction.label, 'btn-danger', dangerAction.handler));
    actions.appendChild(button('取消', 'btn-secondary', function () { overlay.remove(); }));
    var submit = button(submitLabel, 'btn-primary', function () {});
    submit.type = 'submit';
    actions.appendChild(submit);
    form.appendChild(actions);
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      ui().setBusy(submit, true);
      Promise.resolve(onSubmit(collectForm(form))).then(function () { overlay.remove(); }, function () { ui().setBusy(submit, false); });
    });
    panel.appendChild(form);
    overlay.appendChild(panel);
    main.appendChild(overlay);
    var first = form.querySelector('input, select, textarea');
    if (first) first.focus();
  }

  function openCreateProject() {
    openForm('新建 AI 视频项目', [
      inputField('项目名称', 'name', ''),
      selectField('视频形态', 'video_format', 'short_drama', listOf(state.meta, ['project_formats'])),
      selectField('画幅', 'aspect_ratio', '9:16', listOf(state.meta, ['aspect_ratios'])),
      selectField('视觉风格', 'visual_style', 'realistic', listOf(state.meta, ['visual_styles'])),
      inputField('目标时长（秒）', 'target_duration', 120, 'number'),
      inputField('每周产能', 'weekly_output', 3, 'number'),
      selectField('自动化等级', 'automation_level', 'L2', listOf(state.meta, ['automation_levels'])),
      inputField('备注', 'notes', '', 'textarea')
    ], '创建项目', function (values) {
      values.target_duration = Number(values.target_duration) || 120;
      values.weekly_output = Number(values.weekly_output) || 3;
      return api().post('/api/ai-video/projects', values).then(function (data) {
        state.projectId = data && (data.id || (data.project && data.project.id));
        state.dashboard = null;
        ui().toastSuccess('项目已创建');
        return refresh();
      }, function (err) { toastError(err, '创建项目失败'); throw err; });
    });
  }

  function openEditProject(project) {
    openForm('编辑项目', [
      inputField('项目名称', 'name', project.name || ''),
      selectField('视频形态', 'video_format', project.video_format, listOf(state.meta, ['project_formats'])),
      selectField('画幅', 'aspect_ratio', project.aspect_ratio, listOf(state.meta, ['aspect_ratios'])),
      selectField('视觉风格', 'visual_style', project.visual_style, listOf(state.meta, ['visual_styles'])),
      inputField('目标时长（秒）', 'target_duration', project.target_duration, 'number'),
      inputField('每周产能', 'weekly_output', project.weekly_output, 'number'),
      selectField('自动化等级', 'automation_level', project.automation_level, listOf(state.meta, ['automation_levels'])),
      selectField('项目状态', 'status', project.status, listOf(state.meta, ['project_statuses'])),
      inputField('备注', 'notes', project.notes || '', 'textarea')
    ], '保存', function (values) {
      values.target_duration = Number(values.target_duration) || 120;
      values.weekly_output = Number(values.weekly_output) || 3;
      return api().patch('/api/ai-video/projects/' + encodeURIComponent(state.projectId), values).then(function () {
        ui().toastSuccess('项目已更新'); return refresh();
      }, function (err) { toastError(err, '更新项目失败'); throw err; });
    });
  }

  function deleteProject() {
    if (!root.confirm('确定删除当前 AI 视频项目？此操作不可撤销。')) return;
    api().del('/api/ai-video/projects/' + encodeURIComponent(state.projectId)).then(function () {
      state.projectId = null; state.dashboard = null; ui().toastSuccess('项目已删除'); refresh();
    }, function (err) { toastError(err, '删除项目失败'); });
  }

  function seedTemplate() {
    if (!root.confirm('载入“真人写实短剧 28 镜模板”？模板会为当前项目创建预设场景和镜头。')) return;
    setBusy(true);
    api().post('/api/ai-video/projects/' + encodeURIComponent(state.projectId) + '/seed-template', { template: 'realistic_short_drama_28' }, { timeout: api().LONG_TIMEOUT }).then(function () {
      setBusy(false); ui().toastSuccess('28 镜模板已载入'); loadDashboard(state.projectId);
    }, function (err) { setBusy(false); toastError(err, '载入模板失败'); });
  }

  function openCreateScene() {
    var count = listOf(state.dashboard, ['scenes']).length + 1;
    openForm('新增场景', [
      inputField('场景编号', 'scene_code', 'SC' + String(count).padStart(2, '0')),
      inputField('场景标题', 'title', ''),
      inputField('地点', 'location', ''),
      inputField('时间', 'time_of_day', 'day'),
      inputField('轴线规则', 'axis_rule', '', 'textarea'),
      inputField('光线', 'lighting', '', 'textarea'),
      inputField('排序', 'order_idx', count - 1, 'number')
    ], '新增场景', function (values) {
      values.order_idx = Number(values.order_idx) || 0;
      return api().post('/api/ai-video/projects/' + encodeURIComponent(state.projectId) + '/scenes', values).then(function () {
        ui().toastSuccess('场景已新增'); return loadDashboard(state.projectId);
      }, function (err) { toastError(err, '新增场景失败'); throw err; });
    });
  }

  function openCreateShot(scene) {
    var next = listOf(scene, ['shots']).length + 1;
    var draft = { status: 'pending', duration: 3, shot_code: (scene.scene_code || 'SC') + '_S' + String(next).padStart(2, '0'), order_idx: next - 1 };
    openForm('新增镜头 · ' + (scene.title || scene.name || ''), shotFields(draft), '新增镜头', function (values) {
      values = shotPayload(values);
      return api().post('/api/ai-video/scenes/' + encodeURIComponent(scene.id) + '/shots', values).then(function () {
        ui().toastSuccess('镜头已新增'); return loadDashboard(state.projectId);
      }, function (err) { toastError(err, '新增镜头失败'); throw err; });
    });
  }

  function shotFields(shot) {
    var typeChoices = listOf(state.meta, ['shot_types', 'types']);
    if (!typeChoices.length) typeChoices = ['establishing', 'wide', 'two_shot', 'ots', 'speak_cu', 'reaction_cu', 'insert_prop', 'tracking_back', 'mirror', 'empty', 'other'];
    var statusChoices = listOf(state.meta, ['shot_statuses', 'statuses']);
    if (!statusChoices.length) statusChoices = ['pending', 'tts_ready', 'frame_draft', 'frame_approved', 'video_draft', 'auto_qc', 'human_review', 'approved', 'hq_render', 'done', 'rejected', 'needs_human'];
    var qc = shot.qc || {};
    return [
      inputField('镜头编号', 'shot_code', shot.shot_code || ''),
      inputField('排序', 'order_idx', shot.order_idx || 0, 'number'),
      selectField('镜头类型', 'shot_type', shot.shot_type || typeChoices[0], typeChoices.map(function (key) { return [key, shotTypeLabel(key)]; })),
      inputField('时长（秒）', 'duration', shot.duration || 3, 'number'),
      inputField('焦段', 'lens', shot.lens || ''),
      inputField('机位', 'camera', shot.camera || '', 'textarea'),
      inputField('说话人', 'speaker', shot.speaker || ''),
      inputField('台词', 'dialogue', shot.dialogue || '', 'textarea'),
      inputField('画面提示', 'prompt_image', shot.prompt_image || '', 'textarea'),
      inputField('运动提示', 'prompt_motion', shot.prompt_motion || '', 'textarea'),
      inputField('首帧', 'first_frame', shot.first_frame || ''),
      inputField('尾帧', 'last_frame', shot.last_frame || ''),
      selectField('状态', 'status', shot.status || 'pending', statusChoices.map(function (key) { return [key, shotStatusLabel(key)]; })),
      inputField('尝试次数', 'attempts', shot.attempts || 0, 'number'),
      inputField('最大尝试次数', 'max_attempts', shot.max_attempts || 3, 'number'),
      inputField('需要口型', 'lipsync', truthy(shot.lipsync), 'checkbox'),
      inputField('人脸一致', 'qc_face_consistency', truthy(qc.face_consistency), 'checkbox'),
      inputField('手部结构', 'qc_hands_anatomy', truthy(qc.hands_anatomy), 'checkbox'),
      inputField('无文字伪影', 'qc_text_artifact', truthy(qc.text_artifact), 'checkbox'),
      inputField('轴线连续', 'qc_axis_continuity', truthy(qc.axis_continuity), 'checkbox'),
      inputField('情绪连续', 'qc_emotion_continuity', truthy(qc.emotion_continuity), 'checkbox'),
      inputField('造型连续', 'qc_wardrobe_continuity', truthy(qc.wardrobe_continuity), 'checkbox'),
      inputField('音画同步', 'qc_audio_sync', truthy(qc.audio_sync), 'checkbox'),
      inputField('时长匹配', 'qc_duration_match', truthy(qc.duration_match), 'checkbox'),
      inputField('皮肤质感', 'qc_skin_texture', truthy(qc.skin_texture), 'checkbox'),
      inputField('眼神自然', 'qc_eye_focus', truthy(qc.eye_focus), 'checkbox'),
      inputField('口型质量', 'qc_mouth_quality', truthy(qc.mouth_quality), 'checkbox'),
      inputField('颗粒一致', 'qc_grain_consistency', truthy(qc.grain_consistency), 'checkbox'),
      inputField('AI 标识', 'qc_ai_label', truthy(qc.ai_label), 'checkbox')
    ];
  }

  function shotPayload(values) {
    var payload = Object.assign({}, values);
    payload.order_idx = Number(payload.order_idx) || 0;
    payload.duration = Number(payload.duration) || 3;
    payload.attempts = Number(payload.attempts) || 0;
    payload.max_attempts = Number(payload.max_attempts) || 3;
    payload.qc = {
      face_consistency: !!payload.qc_face_consistency,
      hands_anatomy: !!payload.qc_hands_anatomy,
      text_artifact: !!payload.qc_text_artifact,
      axis_continuity: !!payload.qc_axis_continuity,
      emotion_continuity: !!payload.qc_emotion_continuity,
      wardrobe_continuity: !!payload.qc_wardrobe_continuity,
      audio_sync: !!payload.qc_audio_sync,
      duration_match: !!payload.qc_duration_match,
      skin_texture: !!payload.qc_skin_texture,
      eye_focus: !!payload.qc_eye_focus,
      mouth_quality: !!payload.qc_mouth_quality,
      grain_consistency: !!payload.qc_grain_consistency,
      ai_label: !!payload.qc_ai_label
    };
    Object.keys(payload).forEach(function (key) { if (key.indexOf('qc_') === 0) delete payload[key]; });
    return payload;
  }

  function openShotEditor(shot) {
    openForm('编辑镜头 ' + (shot.shot_code || shot.code || ('#' + shot.id)), shotFields(shot), '保存镜头', function (values) {
      values = shotPayload(values);
      return api().patch('/api/ai-video/shots/' + encodeURIComponent(shot.id), values).then(function () {
        ui().toastSuccess('镜头已保存'); return loadDashboard(state.projectId);
      }, function (err) { toastError(err, '保存镜头失败'); throw err; });
    });
  }

  function assetPreviewField(asset) {
    var wrap = e('div', 'ai-video-asset-editor-preview');
    var cover = e('div', 'ai-video-asset-editor-cover');
    if (asset.image_url) {
      var img = e('img', 'ai-video-thumb-clickable');
      img.src = asset.image_url;
      img.alt = (asset.name || '资产') + ' 图片';
      img.title = '点击查看全图';
      img.tabIndex = 0;
      img.setAttribute('role', 'button');
      img.setAttribute('aria-label', '查看 ' + (asset.name || '资产') + ' 全图');
      img.addEventListener('click', function () { openAssetViewerForAsset(asset); });
      img.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openAssetViewerForAsset(asset); }
      });
      cover.appendChild(img);
    } else {
      cover.appendChild(e('span', 'ai-video-asset-initial', assetTypeLabel(asset.asset_type).slice(0, 1)));
      cover.appendChild(e('small', null, asset.status === 'generating' ? '正在生成…' : '暂无图片'));
    }
    wrap.appendChild(cover);
    var info = e('div', 'ai-video-asset-editor-info');
    info.appendChild(e('strong', null, asset.name || ('资产 #' + asset.id)));
    info.appendChild(e('span', 'text-tertiary', assetTypeLabel(asset.asset_type) + ' · ' + assetStatusLabel(asset.status)));
    if (asset.metadata && asset.metadata.width) {
      info.appendChild(e('span', 'text-tertiary', asset.metadata.width + '×' + asset.metadata.height));
    }
    if (asset.last_generated_at) info.appendChild(e('span', 'text-tertiary', '最近生成 ' + asset.last_generated_at));
    var progress = asset.metadata && asset.metadata.set_progress;
    if (asset.status === 'generating' && progress && progress.total) {
      info.appendChild(e('span', 'text-tertiary', '整组进度 ' + (progress.completed || 0) + '/' + progress.total + (progress.current_label ? ' · ' + progress.current_label : '')));
    }
    if (asset.error_message) info.appendChild(e('p', asset.status === 'failed' ? 'ai-video-asset-error' : 'ai-video-asset-warning', asset.error_message));
    wrap.appendChild(info);
    return wrap;
  }

  function assetFormPayload(values) {
    var payload = {
      name: values.name,
      description: values.description,
      status: values.status,
      prompt: values.prompt,
      negative: values.negative,
      workflow: values.workflow,
      ref_path: values.ref_path
    };
    if (values.seed !== '' && values.seed !== undefined) payload.seed = Number(values.seed);
    else payload.seed = '';
    return payload;
  }

  function assetGalleryField(asset) {
    var wrap = e('div', 'ai-video-asset-gallery-field');
    wrap.appendChild(e('div', 'field-label', '已生成图片'));
    var grid = e('div', 'ai-video-asset-gallery');
    grid.appendChild(ui().loadingInline('正在加载角色图…'));
    wrap.appendChild(grid);
    loadAssetGallery(asset.id, grid, asset.name || '角色图');
    return wrap;
  }

  function loadAssetGallery(assetId, grid, title) {
    api().get('/api/ai-video/assets/' + encodeURIComponent(assetId) + '/images', {}).then(function (data) {
      var items = listOf(data, ['items']);
      grid.innerHTML = '';
      if (!items.length) {
        grid.appendChild(e('p', 'ai-video-empty-copy', '还没有生成图片。可以用下方「重新生成图片 / 生成角度组 / 生成表情组」。'));
        return;
      }
      items.forEach(function (item, index) {
        var card = e('figure', 'ai-video-asset-variant');
        if (item.image_url) {
          var img = e('img', 'ai-video-thumb-clickable');
          img.src = item.image_url;
          img.alt = item.label || '角色图';
          img.loading = 'lazy';
          img.title = '点击查看全图';
          img.tabIndex = 0;
          img.setAttribute('role', 'button');
          img.setAttribute('aria-label', '查看 ' + (item.label || '角色图') + ' 全图');
          img.addEventListener('click', function (event) {
            event.stopPropagation();
            openAssetViewer(items, index, title || '角色图');
          });
          img.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' || event.key === ' ') {
              event.preventDefault();
              openAssetViewer(items, index, title || '角色图');
            }
          });
          card.appendChild(img);
        }
        var cap = e('figcaption', 'ai-video-asset-variant-cap');
        cap.appendChild(e('span', null, item.label || '未命名'));
        card.appendChild(cap);
        var actions = e('div', 'ai-video-asset-variant-actions');
        actions.appendChild(button('设为封面', 'btn-ghost', function () {
          api().post('/api/ai-video/assets/' + encodeURIComponent(assetId) + '/images/' + encodeURIComponent(item.id) + '/cover', {}).then(function () {
            ui().toastSuccess('封面已更新');
            loadAssetGallery(assetId, grid, title);
            return loadDashboard(state.projectId);
          }, function (err) { toastError(err, '设置封面失败'); });
        }));
        actions.appendChild(button('删除', 'btn-ghost', function () {
          if (!root.confirm('删除这张图片？文件会一并移除。')) return;
          api().del('/api/ai-video/assets/' + encodeURIComponent(assetId) + '/images/' + encodeURIComponent(item.id)).then(function () {
            ui().toastSuccess('图片已删除');
            loadAssetGallery(assetId, grid, title);
            return loadDashboard(state.projectId);
          }, function (err) { toastError(err, '删除图片失败'); });
        }));
        card.appendChild(actions);
        grid.appendChild(card);
      });
    }, function (err) {
      grid.innerHTML = '';
      grid.appendChild(e('p', 'ai-video-empty-copy', ui().errorText(err, '加载角色图失败')));
    });
  }

  function openAssetViewerForAsset(asset) {
    if (!asset || !asset.id) return;
    if (asset.image_url) {
      var fallback = [{
        label: '封面',
        image_url: asset.image_url,
        width: asset.metadata && asset.metadata.width,
        height: asset.metadata && asset.metadata.height
      }];
      api().get('/api/ai-video/assets/' + encodeURIComponent(asset.id) + '/images', {}).then(function (data) {
        var items = listOf(data, ['items']);
        if (items.length) {
          var index = 0;
          for (var i = 0; i < items.length; i += 1) {
            if (items[i].image_path && items[i].image_path === asset.image_path) { index = i; break; }
          }
          openAssetViewer(items, index, asset.name || '角色图');
        } else {
          openAssetViewer(fallback, 0, asset.name || '角色图');
        }
      }, function () {
        openAssetViewer(fallback, 0, asset.name || '角色图');
      });
    } else {
      ui().toast({ message: '该资产还没有图片', type: 'info' });
    }
  }

  function openAssetViewer(items, index, title) {
    var list = (items || []).filter(function (item) { return item && item.image_url; });
    if (!list.length) return;
    closeAssetViewer();
    var current = Math.max(0, Math.min(list.length - 1, Number(index) || 0));
    var overlay = e('div', 'lightbox ai-video-lightbox');
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.setAttribute('aria-label', '查看全图：' + (title || ''));
    var stage = e('div', 'lightbox-stage');
    var image = e('img', 'ai-video-viewer-image');
    stage.appendChild(image);
    overlay.appendChild(stage);

    var close = e('button', 'icon-btn lightbox-close ai-video-viewer-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭全图');
    close.innerHTML = AIBAR.icons.get('close', 20);
    overlay.appendChild(close);

    var caption = e('div', 'ai-video-viewer-caption');
    overlay.appendChild(caption);

    var prev = e('button', 'icon-btn ai-video-viewer-nav is-prev');
    prev.type = 'button';
    prev.setAttribute('aria-label', '上一张');
    prev.innerHTML = AIBAR.icons.get('chevronLeft', 24);
    var next = e('button', 'icon-btn ai-video-viewer-nav is-next');
    next.type = 'button';
    next.setAttribute('aria-label', '下一张');
    next.innerHTML = AIBAR.icons.get('chevronRight', 24);
    if (list.length > 1) {
      overlay.appendChild(prev);
      overlay.appendChild(next);
    }

    function render() {
      var item = list[current];
      image.src = item.image_url;
      image.alt = (title || '') + ' ' + (item.label || '') + ' 全图';
      var parts = [];
      if (title) parts.push(title);
      if (item.label) parts.push(item.label);
      parts.push((current + 1) + ' / ' + list.length);
      if (item.width) parts.push(item.width + '×' + item.height);
      caption.textContent = parts.join('  ·  ');
      prev.disabled = current <= 0;
      next.disabled = current >= list.length - 1;
    }

    function onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        closeAssetViewer();
      } else if (event.key === 'ArrowLeft' && list.length > 1) {
        event.preventDefault();
        current = Math.max(0, current - 1);
        render();
      } else if (event.key === 'ArrowRight' && list.length > 1) {
        event.preventDefault();
        current = Math.min(list.length - 1, current + 1);
        render();
      }
    }

    close.addEventListener('click', closeAssetViewer);
    prev.addEventListener('click', function () { current = Math.max(0, current - 1); render(); });
    next.addEventListener('click', function () { current = Math.min(list.length - 1, current + 1); render(); });
    stage.addEventListener('click', function (event) { if (event.target === stage) closeAssetViewer(); });
    doc.addEventListener('keydown', onKey, true);
    doc.body.appendChild(overlay);
    doc.body.classList.add('ai-video-viewer-open');
    state.viewer = { overlay: overlay, onKey: onKey };
    render();
    close.focus();
  }

  function closeAssetViewer() {
    if (state.viewer) {
      doc.removeEventListener('keydown', state.viewer.onKey, true);
      if (state.viewer.overlay && state.viewer.overlay.parentNode) {
        state.viewer.overlay.parentNode.removeChild(state.viewer.overlay);
      }
      state.viewer = null;
    }
    doc.body.classList.remove('ai-video-viewer-open');
  }

  function startAssetSet(asset, preset, values, overlay) {
    var payload = {
      preset: preset,
      workflow: values.workflow,
      prompt: values.prompt,
      negative: values.negative,
      randomize: !!values.randomize
    };
    // “换一个种子”优先于输入框里的旧种子；否则旧种子会让 randomize 永远失效。
    if (!payload.randomize && values.seed !== '' && values.seed !== undefined) payload.seed = Number(values.seed);
    overlay.remove();
    return api().post('/api/ai-video/assets/' + encodeURIComponent(asset.id) + '/generate-set', payload).then(function (data) {
      ui().toastSuccess('已提交 ' + ((data && data.count) || '') + ' 张，完成后自动刷新');
      startAssetPolling();
      return loadDashboard(state.projectId);
    }, function (err) {
      toastError(err, '提交整组生成失败');
      throw err;
    });
  }

  function openAssetEditor(asset) {
    var statusChoices = (listOf(state.meta, ['asset_manual_statuses']) || ['draft', 'ready', 'locked', 'archived']).map(function (key) {
      return [key, assetStatusLabel(key)];
    });
    var workflowField = selectField('出图工作流', 'workflow', asset.workflow || '', workflowChoices(asset.workflow || ''));
    var fields = [
      assetPreviewField(asset),
      assetGalleryField(asset),
      inputField('资产名称', 'name', asset.name || ''),
      selectField('状态', 'status', asset.status === 'generating' || asset.status === 'failed' ? 'draft' : (asset.status || 'draft'), statusChoices),
      inputField('描述 / 角色设定', 'description', asset.description || '', 'textarea'),
      inputField('出图提示词', 'prompt', asset.prompt || '', 'textarea'),
      e('p', 'text-tertiary ai-video-field-span', '留空时按名称、描述与视觉风格自动生成角色设定提示词。'),
      inputField('负向提示词', 'negative', asset.negative || '', 'textarea'),
      workflowField,
      inputField('种子', 'seed', asset.seed === null || asset.seed === undefined ? '' : asset.seed, 'number'),
      inputField('参考图引用', 'ref_path', asset.ref_path || ''),
      inputField('换一个种子重新抽卡', 'randomize', false, 'checkbox')
    ];

    openForm('资产详情 · ' + (asset.name || ('#' + asset.id)), fields, '保存信息', function (values) {
      return api().patch('/api/ai-video/assets/' + encodeURIComponent(asset.id), assetFormPayload(values)).then(function () {
        ui().toastSuccess('资产信息已保存');
        return loadDashboard(state.projectId);
      }, function (err) { toastError(err, '保存资产失败'); throw err; });
    }, {
      label: '移除资产',
      handler: function () { deleteAsset(asset.id); }
    }, [{
      label: '生成角度组',
      variant: 'btn-secondary',
      onClick: function (values, form, overlay) {
        return startAssetSet(asset, 'angles', values, overlay);
      }
    }, {
      label: '生成表情组',
      variant: 'btn-secondary',
      onClick: function (values, form, overlay) {
        return startAssetSet(asset, 'expressions', values, overlay);
      }
    }, {
      label: '重新生成图片',
      variant: 'btn-primary',
      onClick: function (values, form, overlay) {
        var payload = {
          workflow: values.workflow,
          prompt: values.prompt,
          negative: values.negative,
          randomize: !!values.randomize
        };
        if (!payload.randomize && values.seed !== '' && values.seed !== undefined) payload.seed = Number(values.seed);
        overlay.remove();
        return api().post('/api/ai-video/assets/' + encodeURIComponent(asset.id) + '/generate', payload).then(function () {
          ui().toastSuccess('已提交生成，完成后自动刷新');
          startAssetPolling();
          return loadDashboard(state.projectId);
        }, function (err) { toastError(err, '提交生成失败'); throw err; });
      }
    }]);
  }

  function quickGenerateAsset(asset) {
    api().post('/api/ai-video/assets/' + encodeURIComponent(asset.id) + '/generate', {}).then(function () {
      ui().toastSuccess('已提交生成，完成后自动刷新');
      startAssetPolling();
      return loadDashboard(state.projectId);
    }, function (err) { toastError(err, '提交生成失败'); });
  }

  function startAssetPolling() {
    if (state.assetTimer) return;
    var started = Date.now();
    state.assetTimer = setInterval(function () {
      // 表情组 5 张在首次加载模型时可能超过 4 分钟，给足 15 分钟且持续展示进度。
      if (!state.projectId || Date.now() - started > 900000) {
        clearInterval(state.assetTimer);
        state.assetTimer = null;
        ui().toast({ message: '生成仍在后台运行，可稍后刷新查看结果', type: 'warning' });
        return;
      }
      api().get('/api/ai-video/projects/' + encodeURIComponent(state.projectId) + '/dashboard', {}).then(function (data) {
        state.dashboard = data || {};
        render();
        var assets = listOf(state.dashboard, ['assets']);
        if (!assets.some(function (item) { return item.status === 'generating'; })) {
          clearInterval(state.assetTimer);
          state.assetTimer = null;
        }
      }, function () {
        /* 轮询失败不打断用户；下一次再试 */
      });
    }, 2500);
  }

  function openWorkflowManager() {
    var project = (state.dashboard && state.dashboard.project) || {};
    var ready = state.workflows.filter(function (item) { return item.character_ready !== false; });
    var others = state.workflows.filter(function (item) { return item.character_ready === false; });
    var body = e('div', 'ai-video-workflow-manager');
    body.appendChild(e('p', 'text-tertiary', '项目默认工作流用于所有资产；单个角色可在「资产详情」里覆盖。共 ' + state.workflows.length + ' 个已同步工作流，其中 ' + ready.length + ' 个可直接用于角色出图。'));
    var selectOptions = [['', '不指定（自动选最新可出图工作流）']].concat(ready.map(function (item) {
      return [item.filename, (item.name || item.filename) + (item.node_count ? ' · ' + item.node_count + ' 节点' : '')];
    }));
    if (project.default_workflow && !ready.some(function (item) { return item.filename === project.default_workflow; })) {
      selectOptions.push([project.default_workflow, project.default_workflow + '（当前，不可直接用于角色）']);
    }
    var select = selectField('项目默认工作流', 'default_workflow', project.default_workflow || '', selectOptions);
    var selectNode = select.querySelector('select');
    body.appendChild(select);

    if (!state.workflows.length) {
      body.appendChild(e('p', 'ai-video-empty-copy', '还没有同步到 ComfyUI 工作流。请先在「工作流」页同步。'));
    } else {
      var list = e('div', 'ai-video-workflow-list');
      ready.concat(others).slice(0, 12).forEach(function (item) {
        var usable = item.character_ready !== false;
        var row = e('div', 'ai-video-workflow-row' + (usable ? '' : ' is-disabled'));
        var text = e('div');
        var title = e('strong', null, item.name || item.filename);
        title.appendChild(e('span', 'ai-video-workflow-badge' + (usable ? ' is-ok' : ''), usable ? '可出图' : (item.workflow_reason || '不可用')));
        text.appendChild(title);
        text.appendChild(e('span', 'text-tertiary', item.filename + (item.node_count ? ' · ' + item.node_count + ' 节点' : '')));
        row.appendChild(text);
        var pick = button('设为默认', 'btn-ghost', function () {
          selectNode.value = item.filename;
          ui().toast({ message: '已选择「' + (item.name || item.filename) + '」，点保存生效', type: 'info' });
        });
        pick.disabled = !usable;
        row.appendChild(pick);
        list.appendChild(row);
      });
      body.appendChild(list);
    }

    return ui().modal({
      title: '出图工作流',
      desc: '管理 AI 视频角色/场景的出图工作流；工作流本身在「工作流」页同步与维护。',
      body: body,
      actions: [
        {
          label: '保存默认',
          variant: 'primary',
          close: false,
          onClick: function (modalEntry) {
            return api().patch('/api/ai-video/projects/' + encodeURIComponent(state.projectId), {
              default_workflow: selectNode.value
            }).then(function () {
              ui().toastSuccess('项目默认工作流已更新');
              modalEntry.close();
              return loadDashboard(state.projectId);
            }, function (err) { toastError(err, '保存工作流失败'); });
          }
        },
        {
          label: '查看全部工作流',
          variant: 'secondary',
          onClick: function () {
            if (AIBAR.app && AIBAR.app.navigate) AIBAR.app.navigate('workflows');
          }
        },
        { label: '关闭', variant: 'secondary' }
      ]
    });
  }

  function openCreateAsset() {
    var types = listOf(state.meta, ['asset_types']);
    if (!types.length) types = ['character', 'scene', 'prop', 'wardrobe', 'voice', 'style'];
    var typeChoices = types.map(function (key) { return [key, assetTypeLabel(key)]; });
    openForm('添加项目资产', [
      inputField('资产名称', 'name', ''),
      inputField('资产标识', 'asset_key', ''),
      selectField('资产类型', 'asset_type', 'character', typeChoices),
      inputField('描述 / 角色设定', 'description', '', 'textarea'),
      inputField('出图提示词（可留空）', 'prompt', '', 'textarea'),
      selectField('出图工作流', 'workflow', '', workflowChoices('')),
      inputField('种子（可留空）', 'seed', '', 'number'),
      inputField('受控相对引用', 'ref_path', '')
    ], '添加资产', function (values) {
      if (!values.asset_key) values.asset_key = values.name;
      if (values.seed !== '' && values.seed !== undefined) values.seed = Number(values.seed);
      else delete values.seed;
      return api().post('/api/ai-video/projects/' + encodeURIComponent(state.projectId) + '/assets', values).then(function () {
        ui().toastSuccess('资产已添加'); return loadDashboard(state.projectId);
      }, function (err) { toastError(err, '添加资产失败'); throw err; });
    });
  }

  function deleteAsset(assetId) {
    if (!root.confirm('确定移除此资产？')) return;
    api().del('/api/ai-video/assets/' + encodeURIComponent(assetId)).then(function () {
      ui().toastSuccess('资产已移除'); loadDashboard(state.projectId);
    }, function (err) { toastError(err, '移除资产失败'); });
  }

  var exported = {
    init: init,
    onEnter: onEnter,
    refresh: refresh,
    normalizeShotStatus: normalizeShotStatus,
    progressModel: progressModel,
    filterShots: filterShots,
    deriveProjectStage: deriveProjectStage,
    shotTypeLabel: shotTypeLabel,
    shotStatusLabel: shotStatusLabel,
    assetTypeLabel: assetTypeLabel,
    assetStatusLabel: assetStatusLabel,
    workflowChoices: workflowChoices
  };

  AIBAR.aiVideo = exported;
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
