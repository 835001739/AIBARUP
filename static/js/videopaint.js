/* ============================================================
   AIBAR · 视频转绘（M16）
   - 选源视频 + 参考图 + 提示词与生成参数，新建一个「视频转绘」任务
   - 流水线四步：①抽帧准备(prepare) ②动作拆解(pose，提骨架) ③逐帧重绘(generate) ④去背景(nobg) ⑤拼组图(sheet)
   - 逐帧预览：源帧 → 骨架 → 生成图 → 去背景图 并排对比
   - 生成的图直接写回「组图管理」（后端已做），这里提供「查看组图」一键跳转与「连播生成序列」
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  var state = { keyword: '', currentJobId: null, clips: [] };

  function api() { return AIBAR.api; }
  function ui() { return AIBAR.ui; }
  function host() { return doc ? doc.getElementById('vp-root') : null; }
  function icon(name, size) { return AIBAR.icons ? AIBAR.icons.get(name, size || 14) : ''; }

  function btn(variant, iconName, label, onClick, title) {
    var b = ui().el('button', 'btn btn-' + variant + ' btn-sm');
    b.type = 'button';
    if (iconName) {
      var ic = ui().el('span', 'btn-icon');
      ic.innerHTML = icon(iconName, 14);
      b.appendChild(ic);
    }
    b.appendChild(ui().el('span', null, label));
    if (title) b.title = title;
    b.addEventListener('click', onClick);
    return b;
  }

  function badge(text, tone) {
    var cls = 'badge';
    if (tone === 'success') cls += ' badge-success';
    else if (tone === 'warning') cls += ' badge-warning';
    else if (tone === 'error') cls += ' badge-error';
    return ui().el('span', cls, text);
  }

  function statusText(s) {
    return {
      idle: '待准备', prepared: '已抽帧', posed: '已拆解',
      done: '已完成', partial: '部分完成', failed: '失败'
    }[s] || s || '—';
  }

  function statusTone(s) {
    return {
      idle: '', prepared: 'warning', posed: 'warning',
      done: 'success', partial: 'warning', failed: 'error'
    }[s] || '';
  }

  function frameStatusText(s) {
    return {
      pending: '待处理', posed: '已拆解', generating: '生成中',
      done: '完成', failed: '失败'
    }[s] || s || '—';
  }

  function frameStatusTone(s) {
    return {
      pending: '', posed: 'warning', generating: 'warning',
      done: 'success', failed: 'error'
    }[s] || '';
  }

  function intVal(input, fallback) {
    if (!input || !input.value.trim()) return undefined;
    var n = parseInt(input.value, 10);
    return isNaN(n) ? fallback : n;
  }

  function floatVal(input, fallback) {
    if (!input || !input.value.trim()) return undefined;
    var n = parseFloat(input.value);
    return isNaN(n) ? fallback : n;
  }

  /* ---------------------------------------------------------- 初始化 */

  function init() {
    var h = host();
    if (!h) return;
    if (h.dataset.ready === '1') return;
    h.dataset.ready = '1';
    renderShell(h);
    loadJobs();
  }

  function onEnter() {
    var h = host();
    if (!h) return;
    if (h.dataset.ready !== '1') { init(); return; }
    loadJobs();
  }

  function renderShell(h) {
    h.innerHTML = '';
    var toolbar = ui().el('div', 'page-toolbar');

    var search = ui().el('div', 'search');
    var input = ui().el('input', 'input');
    input.type = 'search';
    input.placeholder = '搜索任务名称';
    input.value = state.keyword;
    input.addEventListener('input', function () {
      state.keyword = input.value.trim();
      loadJobs();
    });
    search.appendChild(input);
    toolbar.appendChild(search);

    toolbar.appendChild(ui().el('span', 'grow'));

    toolbar.appendChild(btn('ghost', 'refresh', '刷新', function () { loadJobs(); }));
    toolbar.appendChild(btn('primary', 'sparkles', '新建视频转绘', function () { openCreate(); }));

    h.appendChild(toolbar);

    var grid = ui().el('div', 'card-grid');
    grid.setAttribute('role', 'list');
    grid.id = 'vp-jobs-grid';
    h.appendChild(grid);
  }

  /* ---------------------------------------------------------- 任务列表 */

  function loadJobs() {
    var h = host();
    if (!h) return;
    var grid = h.querySelector('#vp-jobs-grid');
    if (!grid) return;
    grid.innerHTML = '';
    grid.appendChild(ui().loadingInline('加载任务…'));

    api().get('/api/videopaint/jobs?keyword=' + encodeURIComponent(state.keyword)).then(function (d) {
      renderJobs(grid, (d && d.items) || []);
    }, function (err) {
      grid.innerHTML = '';
      grid.appendChild(ui().errorState
        ? ui().errorState(ui().errorText(err, '加载失败'), loadJobs)
        : ui().el('p', 'entry-text text-tertiary', ui().errorText(err, '加载失败')));
    });
  }

  function renderJobs(grid, items) {
    grid.innerHTML = '';
    if (!items.length) {
      grid.appendChild(ui().emptyState
        ? ui().emptyState({
            icon: 'film',
            title: '还没有视频转绘任务',
            desc: '点「新建视频转绘」选一个已抽帧的视频，配上参考图与提示词，逐帧重绘成漫画风序列。',
            actions: [{ label: '新建任务', variant: 'primary', onClick: function () { openCreate(); } }]
          })
        : ui().el('p', 'entry-text text-tertiary', '还没有视频转绘任务'));
      return;
    }
    items.forEach(function (j) { grid.appendChild(renderJobCard(j)); });
  }

  function renderJobCard(j) {
    var card = ui().el('div', 'entry-card vp-job-card');
    card.setAttribute('role', 'listitem');

    var body = ui().el('div', 'entry-body');
    var title = ui().el('h3', 'entry-title clamp-1', j.name || '未命名');
    title.title = j.name || '';
    body.appendChild(title);

    var meta = ui().el('p', 'entry-text text-tertiary clamp-1');
    meta.textContent = '源视频 #' + (j.clip_id || '?') + ' · ' + (j.frame_count || 0) + ' 帧 · 骨架 ' + (j.pose_count || 0) + ' · 生成 ' + (j.done_count || 0);
    body.appendChild(meta);

    var statusRow = ui().el('div', 'shot-card-head vp-status-row');
    statusRow.appendChild(badge(statusText(j.status), statusTone(j.status)));
    if (j.has_nobg) statusRow.appendChild(badge('已去背景', 'success'));
    body.appendChild(statusRow);

    if (j.last_error) {
      var err = ui().el('p', 'video-error clamp-2', j.last_error);
      err.title = j.last_error;
      body.appendChild(err);
    }
    card.appendChild(body);

    var actions = ui().el('div', 'video-actions');
    actions.appendChild(btn('secondary', 'film', '打开', function () { openDetail(j.id); }));
    actions.appendChild(btn('ghost', 'trash', '删除', function () { doDelete(j); }));
    card.appendChild(actions);
    return card;
  }

  /* ---------------------------------------------------------- 新建任务 */

  function openCreate() {
    api().get('/api/videopaint/clips?keyword=').then(function (d) {
      state.clips = (d && d.items) || [];
      showCreateModal();
    }, function () {
      state.clips = [];
      showCreateModal();
    });
  }

  function showCreateModal() {
    var body = ui().el('div', 'form-stack vp-create');

    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '把一个已抽帧的视频拆成逐帧骨架，再用参考图锁脸 + 骨架控姿势，逐帧重绘成统一风格。';
    body.appendChild(tip);

    // ── 源视频 ──
    var clipOptions = [{ label: '（请选择源视频）', value: '' }].concat(
      state.clips.map(function (c) { return { label: (c.name || ('# ' + c.id)) + (c.frame_count_actual ? '（' + c.frame_count_actual + ' 帧）' : ''), value: String(c.id) }; })
    );
    if (!clipOptions.length) clipOptions = [{ label: '（暂无可用的源视频，请先到「视频转帧」导入并抽帧）', value: '' }];
    var clipSelect = ui().select(clipOptions, '');
    body.appendChild(ui().fieldRow('源视频', clipSelect, '建议先到「视频转帧」抽好帧，这里直接选对应的视频'));

    var nameInput = ui().el('input', 'input');
    nameInput.placeholder = '留空则自动命名';
    body.appendChild(ui().fieldRow('任务名称', nameInput));

    // ── 参考图 ──
    var fileInput = ui().el('input', 'input');
    fileInput.type = 'file';
    fileInput.accept = 'image/png,image/jpeg,image/webp';
    body.appendChild(ui().fieldRow('参考图（上传）', fileInput, '用于锁脸 / 控风格，上传后会落到服务端'));

    var pathInput = ui().el('input', 'input');
    pathInput.placeholder = '/Users/.../ref.png（也可填本机已存在的图片绝对路径）';
    body.appendChild(ui().fieldRow('参考图（本地路径）', pathInput, '与上传二选一；ComfyUI 已存在的文件名可填到下方「参考图文件名」'));

    var refNameInput = ui().el('input', 'input');
    refNameInput.placeholder = '如 ComfyUI_00641_.png（可选）';
    body.appendChild(ui().fieldRow('参考图文件名（已存 ComfyUI input）', refNameInput));

    // ── 提示词 ──
    var promptArea = ui().el('textarea', 'textarea');
    promptArea.rows = 3;
    promptArea.placeholder = '正向提示词，如：1girl, blue coat, cinematic lighting, anime style';
    body.appendChild(ui().fieldRow('正向提示词', promptArea));

    var negArea = ui().el('textarea', 'textarea');
    negArea.rows = 2;
    negArea.placeholder = '负向提示词（可选）';
    body.appendChild(ui().fieldRow('负向提示词', negArea));

    // ── 动作拆解 / 生成参数 ──
    var poseMode = ui().select([
      { label: 'DWPose（推荐，全身+手+脸）', value: 'dwpose' },
      { label: 'OpenPose', value: 'openpose' }
    ], 'dwpose');
    body.appendChild(ui().fieldRow('骨架模型', poseMode));

    var grid2 = ui().el('div', 'form-grid-2');
    grid2.appendChild(numField('分辨率(pose)', '512', 'pose_resolution'));
    grid2.appendChild(numField('步数 steps', '28', 'steps'));
    grid2.appendChild(numField('CFG', '6.5', 'cfg'));
    grid2.appendChild(numField('控制强度', '1.15', 'controlnet_strength'));
    grid2.appendChild(numField('IPAdapter 权重', '0.85', 'ipadapter_weight'));
    grid2.appendChild(numField('FaceID 权重', '0.85', 'faceidv2_weight'));
    grid2.appendChild(numField('宽', '896', 'width'));
    grid2.appendChild(numField('高', '1152', 'height'));
    grid2.appendChild(numField('基础种子', '0', 'base_seed'));
    grid2.appendChild(numField('种子步进', '0', 'seed_step'));
    body.appendChild(grid2);

    // ── 抽帧参数 ──
    var grid3 = ui().el('div', 'form-grid-2');
    grid3.appendChild(numField('每秒帧数 fps', '2', 'fps'));
    grid3.appendChild(numField('最多帧数', '4', 'max_frames'));
    grid3.appendChild(numField('输出宽度', '480', 'scale_width'));
    body.appendChild(grid3);

    ui().modal({
      title: '新建视频转绘',
      body: body,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '创建',
          variant: 'primary',
          onClick: function () {
            var clipId = clipSelect.value;
            if (!clipId) { ui().toastError('请选择源视频'); return false; }
            var f = fileInput.files && fileInput.files[0];
            var payload = {
              clip_id: parseInt(clipId, 10),
              name: nameInput.value.trim(),
              reference_name: refNameInput.value.trim(),
              prompt: promptArea.value.trim(),
              negative: negArea.value.trim(),
              pose_mode: poseMode.value,
              pose_resolution: intVal(body.querySelector('[data-k="pose_resolution"]')),
              steps: intVal(body.querySelector('[data-k="steps"]')),
              cfg: floatVal(body.querySelector('[data-k="cfg"]')),
              controlnet_strength: floatVal(body.querySelector('[data-k="controlnet_strength"]')),
              ipadapter_weight: floatVal(body.querySelector('[data-k="ipadapter_weight"]')),
              faceidv2_weight: floatVal(body.querySelector('[data-k="faceidv2_weight"]')),
              width: intVal(body.querySelector('[data-k="width"]')),
              height: intVal(body.querySelector('[data-k="height"]')),
              base_seed: intVal(body.querySelector('[data-k="base_seed"]')),
              seed_step: intVal(body.querySelector('[data-k="seed_step"]')),
              fps: intVal(body.querySelector('[data-k="fps"]')),
              max_frames: intVal(body.querySelector('[data-k="max_frames"]')),
              scale_width: intVal(body.querySelector('[data-k="scale_width"]'))
            };
            var doCreate = function (referencePath) {
              if (referencePath) payload.reference_path = referencePath;
              api().post('/api/videopaint/jobs', payload).then(function (r) {
                ui().toastSuccess('已创建任务：' + ((r && r.job && r.job.name) || ''));
                loadJobs();
              }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
            };
            if (f) {
              var fd = new root.FormData();
              fd.append('file', f);
              root.fetch('/api/videopaint/reference-upload', { method: 'POST', body: fd }).then(function (r) {
                return r.json();
              }).then(function (res) {
                if (!res || !res.ok) { ui().toastError((res && res.error && res.error.message) || '参考图上传失败'); return; }
                doCreate(res.data.path);
              }, function () { ui().toastError('参考图上传失败：网络错误'); });
              return true;
            }
            if (pathInput.value.trim()) payload.reference_path = pathInput.value.trim();
            doCreate();
            return true;
          }
        }
      ]
    });
  }

  // 数字字段：在 input 上挂 data-k，提交时按 key 取数
  function numField(label, def, key) {
    var input = ui().el('input', 'input');
    input.type = 'number';
    input.value = def;
    input.setAttribute('data-k', key);
    return ui().fieldRow(label, input);
  }

  /* ---------------------------------------------------------- 任务详情 */

  function openDetail(jobId) {
    state.currentJobId = jobId;
    var h = host();
    if (!h) return;
    h.innerHTML = '';
    h.appendChild(ui().loadingInline('加载任务详情…'));
    renderDetail(jobId);
  }

  function renderDetail(jobId) {
    var h = host();
    if (!h) return;
    api().get('/api/videopaint/jobs/' + jobId).then(function (jd) {
      var job = jd.job;
      api().get('/api/videopaint/jobs/' + jobId + '/frames').then(function (fd) {
        paintDetail(h, job, (fd && fd.items) || []);
      }, function (err) {
        paintDetail(h, job, []);
        ui().toastError(ui().errorText(err, '帧清单加载失败'));
      });
    }, function (err) {
      h.innerHTML = '';
      h.appendChild(ui().errorState
        ? ui().errorState(ui().errorText(err, '加载任务失败'), function () { renderDetail(jobId); })
        : ui().el('p', 'entry-text text-tertiary', ui().errorText(err, '加载任务失败')));
    });
  }

  function paintDetail(h, job, frames) {
    h.innerHTML = '';

    // 头部
    var head = ui().el('div', 'vp-detail-head');
    head.appendChild(btn('ghost', 'arrowLeft', '返回', function () {
      state.currentJobId = null;
      renderShell(h);
      loadJobs();
    }));
    var title = ui().el('h2', 'vp-detail-title', job.name || '未命名');
    title.title = job.name || '';
    head.appendChild(title);
    head.appendChild(badge(statusText(job.status), statusTone(job.status)));
    if (job.has_nobg) head.appendChild(badge('已去背景', 'success'));
    head.appendChild(ui().el('span', 'grow'));
    head.appendChild(btn('ghost', 'trash', '删除', function () { doDelete(job, true); }));
    h.appendChild(head);

    // 配置概览
    var cfg = ui().el('div', 'vp-config');
    if (job.reference_url) {
      var refWrap = ui().el('div', 'vp-ref');
      var refImg = ui().el('img', 'vp-ref-img');
      refImg.src = job.reference_url;
      refImg.alt = '参考图';
      refWrap.appendChild(refImg);
      refWrap.appendChild(ui().el('span', 'vp-ref-label', '参考图'));
      cfg.appendChild(refWrap);
    }
    var cfgText = ui().el('div', 'vp-config-text');
    cfgText.appendChild(ui().el('div', 'clamp-3', job.prompt || '（无正向提示词）'));
    if (job.negative) cfgText.appendChild(ui().el('div', 'text-tertiary clamp-2', '负向：' + job.negative));
    var paramLine = ui().el('div', 'text-tertiary vp-param-line');
    var configuredControl = parseFloat(job.controlnet_strength);
    var effectiveControl = isNaN(configuredControl) ? 1.15 : Math.max(1.15, configuredControl);
    paramLine.textContent = '骨架 ' + (job.pose_mode || 'dwpose') + ' · ' + (job.width || '?') + '×' + (job.height || '?')
      + ' · steps ' + (job.steps || '?') + ' · cfg ' + (job.cfg || '?')
      + ' · 控制 ' + effectiveControl + '（严格模式） · IP ' + (job.ipadapter_weight || '?')
      + ' · 抽帧 fps ' + (job.fps || '?') + ' / 最多 ' + (job.max_frames || '?') + ' 帧';
    cfgText.appendChild(paramLine);
    cfg.appendChild(cfgText);
    h.appendChild(cfg);

    // 流水线
    var pipeline = ui().el('div', 'vp-pipeline');
    var hasFrames = (job.frame_count || 0) > 0;
    var hasPose = hasFrames && (job.pose_count || 0) > 0;
    var hasGen = (job.done_count || 0) > 0;

    pipeline.appendChild(stageBtn(job, 'prepare', '① 抽帧准备', '从视频抽帧并建组图', true));
    pipeline.appendChild(stageBtn(job, 'pose', '② 动作拆解', '逐帧提骨架（动作拆解）', hasFrames));
    pipeline.appendChild(stageBtn(job, 'generate', '③ 逐帧重绘', '参考图锁脸 + 骨架控姿', hasPose));
    pipeline.appendChild(stageBtn(job, 'nobg', '④ 去背景', '生成透明序列帧', hasGen));
    pipeline.appendChild(stageBtn(job, 'sheet', '⑤ 拼组图', '拼成一张序列帧大图', hasGen));
    h.appendChild(pipeline);

    // 操作
    var actions = ui().el('div', 'vp-actions');
    actions.appendChild(btn('secondary', 'play', '连播生成序列', function () { playGenerated(job, frames); }, hasGen ? '逐帧生成图连播' : '需先逐帧重绘'));
    if (job.group_id) {
      actions.appendChild(btn('secondary', 'film', '查看组图', function () { gotoGroups(job); }, '跳转到组图管理播放'));
    }
    // 工作流按钮：打开 ComfyUI 并载入某帧姿态工作流（默认取第一个已拆解的帧）
    var firstPosed = null;
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].pose_path_url) { firstPosed = frames[i]; break; }
    }
    if (frames.length) {
      var wfOrder = firstPosed ? firstPosed.order_idx : frames[0].order_idx;
      actions.appendChild(btn('secondary', 'workflow', '打开工作流', function () { openFrameWorkflow(job.id, wfOrder); }, '在 ComfyUI 中打开某帧工作流（SDXL + ControlNet + IPAdapter）'));
      actions.appendChild(btn('secondary', 'sparkles', '打开 FLUX.2 工作流', function () { openFrameFlux2Workflow(job.id, wfOrder); }, '用 FLUX.2 Klein 打开某帧工作流（单模型 + 双 ReferenceLatent，4 步蒸馏）'));
    }
    h.appendChild(actions);

    if (job.last_error) {
      h.appendChild(ui().el('p', 'video-error clamp-3', '最近错误：' + job.last_error));
    }

    // 逐帧预览
    var framesTitle = ui().el('div', 'vp-frames-title', '逐帧预览（源帧 → 骨架 → 生成 → 去背景）');
    h.appendChild(framesTitle);
    var framesWrap = ui().el('div', 'vp-frames');
    if (!frames.length) {
      framesWrap.appendChild(ui().el('p', 'entry-text text-tertiary', '还没有帧，先点「① 抽帧准备」。'));
    } else {
      frames.forEach(function (f) { framesWrap.appendChild(renderFrameCard(f)); });
    }
    h.appendChild(framesWrap);
  }

  // 流水线步骤按钮：点击即触发对应后端动作，处理期间进入忙态
  function stageBtn(job, stage, label, desc, enabled) {
    var b = ui().el('button', 'btn btn-secondary btn-sm vp-stage');
    b.type = 'button';
    b.appendChild(ui().el('span', 'vp-stage-label', label));
    b.appendChild(ui().el('span', 'vp-stage-desc', desc));
    if (!enabled) {
      b.disabled = true;
      b.title = '前置步骤未完成';
    } else {
      b.addEventListener('click', function () { runStage(job, stage, b); });
    }
    return b;
  }

  function runStage(job, stage, button) {
    ui().setBusy(button, true);
    var endpoint = '/api/videopaint/jobs/' + job.id + '/' + stage;
    // 抽帧 / 拆解 / 重绘都是长任务，用长超时；去背景 / 拼图相对快
    var longTask = (stage === 'prepare' || stage === 'pose' || stage === 'generate');
    var payload = (stage === 'generate' && (job.status === 'done' || job.status === 'partial' || job.status === 'failed'))
      ? { force: true } : {};
    // 严格骨架模式会在出图后复检姿态，低分帧最多增强控制重试一次。
    var timeout = stage === 'generate' ? 3600000 : (longTask ? 600000 : undefined);
    api().post(endpoint, payload, timeout ? { timeout: timeout } : {}).then(function (r) {
      ui().setBusy(button, false);
      var msg = stageDoneMessage(stage, r);
      if (stage === 'generate' && r && r.job && r.job.status === 'failed') {
        ui().toastError('逐帧重绘失败：' + ((r.job && r.job.last_error) || '未知错误'));
      } else if (stage === 'generate' && r && r.job && r.job.status === 'partial') {
        ui().toast({ message: msg + '（部分帧失败，详见帧预览）', type: 'warning' });
      } else {
        ui().toastSuccess(msg);
      }
      renderDetail(job.id);
    }, function (err) {
      ui().setBusy(button, false);
      ui().toastError(ui().errorText(err, stage + ' 失败'));
      renderDetail(job.id);
    });
  }

  function stageDoneMessage(stage, r) {
    var j = (r && r.job) || {};
    if (stage === 'prepare') return '已抽帧并建组图，共 ' + (j.frame_count || 0) + ' 帧';
    if (stage === 'pose') return '动作拆解完成，共 ' + (j.pose_count || 0) + ' 帧骨架';
    if (stage === 'generate') return '逐帧重绘完成，成功 ' + (j.done_count || 0) + ' 帧';
    if (stage === 'nobg') return '已去背景，共 ' + (j.done_count || 0) + ' 帧透明图';
    if (stage === 'sheet') return '已拼组图';
    return '完成';
  }

  function renderFrameCard(f) {
    var card = ui().el('div', 'entry-card vp-frame-card');
    var head = ui().el('div', 'vp-frame-head');
    head.appendChild(ui().el('span', 'vp-frame-idx', '帧 ' + (f.order_idx + 1)));
    head.appendChild(badge(frameStatusText(f.status), frameStatusTone(f.status)));
    card.appendChild(head);

    var imgs = ui().el('div', 'vp-frame-imgs');
    imgs.appendChild(thumb(f.frame_url, '源帧'));
    imgs.appendChild(thumb(f.pose_path_url, '骨架'));
    imgs.appendChild(thumb(f.image_path_url, '生成'));
    imgs.appendChild(thumb(f.nobg_path_url, '去背景'));
    card.appendChild(imgs);

    if (typeof f.pose_score === 'number') {
      var score = Math.max(0, Math.min(100, Math.round(f.pose_score * 100)));
      var qa = ui().el('div', 'text-tertiary vp-pose-score', '动作匹配 ' + score + '%');
      if (f.generation_attempts > 1) qa.textContent += ' · 已增强控制重试';
      card.appendChild(qa);
    }

    // 工作流按钮：SDXL 姿态链路 与 FLUX.2 Klein 链路 并列，同一帧二选一打开
    var fActions = ui().el('div', 'vp-frame-actions');
    fActions.appendChild(frameWorkflowBtn(f));
    fActions.appendChild(frameFlux2Btn(f));
    card.appendChild(fActions);

    if (f.error_message) {
      var err = ui().el('p', 'video-error clamp-2', f.error_message);
      err.title = f.error_message;
      card.appendChild(err);
    }
    return card;
  }

  // 每帧「工作流」按钮（M14 SDXL 链路）：在 ComfyUI 编辑器中打开此帧的姿态重绘工作流
  function frameWorkflowBtn(f) {
    var hasPose = !!f.pose_path_url;
    var b = btn(
      'ghost', 'workflow', '工作流',
      function () { openFrameWorkflow(f.job_id, f.order_idx); },
      hasPose ? '在 ComfyUI 中打开此帧工作流（SDXL + ControlNet + IPAdapter，可调参 / 重新指定帧多次出图）'
        : '请先执行 pose / generate 生成骨架'
    );
    if (!hasPose) b.disabled = true;
    return b;
  }

  // 每帧「FLUX.2」按钮（M17 Klein 链路）：与上面「工作流」按钮并列
  // 同一帧可二选一：SDXL 姿态链路 或 FLUX.2 Klein 结构控制链路（单模型 + 双 ReferenceLatent）
  function frameFlux2Btn(f) {
    var hasPose = !!f.pose_path_url;
    var b = btn(
      'ghost', 'sparkles', 'FLUX.2',
      function () { openFrameFlux2Workflow(f.job_id, f.order_idx); },
      hasPose ? '用 FLUX.2 Klein 打开此帧工作流：参考图锁外观、骨架图锁结构，4 步蒸馏出图（无需 ControlNet / IPAdapter 权重）'
        : '请先执行 pose / generate 生成骨架'
    );
    if (!hasPose) b.disabled = true;
    return b;
  }

  // 调后端 editor-link 路由，拿到 ComfyUI 深链接后新开标签页（姿态工作流）
  function openFrameWorkflow(jobId, orderIdx) {
    openWorkflowLink('/api/videopaint/jobs/' + jobId + '/frames/' + orderIdx + '/editor-link', '打开工作流失败');
  }

  // 同上，但走 FLUX.2 Klein 工作流（M17）
  function openFrameFlux2Workflow(jobId, orderIdx) {
    openWorkflowLink('/api/videopaint/jobs/' + jobId + '/frames/' + orderIdx + '/flux2-editor-link', '打开 FLUX.2 工作流失败');
  }

  // 深链接公共逻辑：GET 后端 → 取 url → 新开标签页（桥接未装时给 warning 而非拦下来）
  function openWorkflowLink(url, errText) {
    ui().setBusy(null, true);
    api().get(url).then(function (r) {
      ui().setBusy(null, false);
      if (!r || !r.url) {
        ui().toastError('无法生成工作流链接');
        return;
      }
      if (r.bridge_installed === false) {
        ui().toast({ message: '未检测到 AIBAR-Bridge 扩展，工作流可能不会自动载入（已打开 ComfyUI）', type: 'warning' });
      }
      window.open(r.url, '_blank');
    }, function (err) {
      ui().setBusy(null, false);
      ui().toastError(ui().errorText(err, errText));
    });
  }

  // 单张缩略图：有 url 显示图片，无则占位（仍占一格，保持并排对齐）
  function thumb(url, label) {
    var wrap = ui().el('div', 'vp-thumb');
    if (url) {
      var img = ui().el('img', 'vp-thumb-img');
      img.src = url;
      img.alt = label;
      img.loading = 'lazy';
      wrap.appendChild(img);
    } else {
      wrap.appendChild(ui().el('div', 'vp-thumb-empty', '—'));
    }
    wrap.appendChild(ui().el('span', 'vp-thumb-label', label));
    return wrap;
  }

  /* ---------------------------------------------------------- 连播 / 跳转 / 删除 */

  function playGenerated(job, frames) {
    var items = frames.filter(function (f) { return f.image_path_url; })
      .map(function (f) { return { url: f.image_path_url, label: '帧 ' + (f.order_idx + 1) }; });
    if (!items.length) { ui().toastError('还没有生成的帧，请先「③ 逐帧重绘」'); return; }
    if (AIBAR.player && AIBAR.player.open) {
      AIBAR.player.open({
        title: (job.name || '视频转绘') + ' · 生成序列',
        items: items,
        interval: 220,
        loop: true
      });
    }
  }

  function gotoGroups(job) {
    if (AIBAR.app && AIBAR.app.navigate) AIBAR.app.navigate('groups');
    ui().toast({ message: '对应组图已在「组图」中生成，可前往连播（组图 #' + (job.group_id || '?') + '）', type: 'info' });
  }

  function doDelete(job, fromDetail) {
    ui().confirm({
      title: '删除视频转绘任务',
      message: '将删除「' + (job.name || '') + '」及其全部帧、生成图与组图。此操作不可撤销。',
      confirmLabel: '删除',
      danger: true
    }).then(function (yes) {
      if (!yes) return;
      api().del('/api/videopaint/jobs/' + job.id).then(function () {
        ui().toastSuccess('已删除');
        if (fromDetail) {
          state.currentJobId = null;
          var h = host();
          if (h) { renderShell(h); }
          loadJobs();
        } else {
          loadJobs();
        }
      }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
    });
  }

  /* ---------------------------------------------------------- 导出 */

  AIBAR.videopaint = {
    init: init,
    onEnter: onEnter,
    refresh: function () { loadJobs(); },
    openDetail: openDetail
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = AIBAR.videopaint;
})(typeof window !== 'undefined' ? window : globalThis);
