/* ============================================================
   AIBAR · 工作流 / 画廊 / 案例库 / 模型 / 同步日志
   本阶段只接入新框架与基础视觉，不重写信息架构（PRD M8.1）。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  // 列表页每页条数：案例库有 2700+ 条，写死取第一页时用户只能看到前 60 条（2%），
  // 且界面没有任何提示。分页控件的实现在 ui.js::pager。
  var PAGE_SIZE = 24;

  var state = {
    wf: { q: '', page: 1, pageSize: PAGE_SIZE, total: 0, items: [], loaded: false },
    gal: { q: '', page: 1, pageSize: 48, total: 0, items: [], loaded: false, index: -1 },
    cases: { style: '', q: '', page: 1, pageSize: PAGE_SIZE, total: 0, items: [], styles: [], loaded: false },
    models: { q: '', items: [], loaded: false },
    logs: { limit: 50, items: [], loaded: false },
    lightbox: null
  };

  var refs = {};

  function ui() { return AIBAR.ui; }
  function api() { return AIBAR.api; }

  function init() {
    refs.wfSearch = doc.getElementById('wf-search');
    refs.wfGrid = doc.getElementById('wf-grid');
    refs.wfPager = doc.getElementById('wf-pager');
    refs.wfPoseBtn = doc.getElementById('btn-wf-pose');
    refs.galSearch = doc.getElementById('gal-search');
    refs.galGrid = doc.getElementById('gal-grid');
    refs.galMore = doc.getElementById('btn-gal-more');
    refs.galTotal = doc.getElementById('gal-total');
    refs.caseStyles = doc.getElementById('case-styles');
    refs.caseGrid = doc.getElementById('case-grid');
    refs.casePager = doc.getElementById('case-pager');
    refs.modelSearch = doc.getElementById('model-search');
    refs.modelBody = doc.getElementById('models-body');
    refs.modelTotal = doc.getElementById('models-total');
    refs.logList = doc.getElementById('log-list');
    refs.logLimit = doc.getElementById('log-limit');

    bindWorkflows();
    bindGallery();
    bindCases();
    bindModels();
    bindLogs();

    // 灯箱 Esc 关闭 + 左右切换
    doc.addEventListener('keydown', function (event) {
      if (!state.lightbox) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        closeLightbox();
      } else if (event.key === 'ArrowRight') {
        stepLightbox(1);
      } else if (event.key === 'ArrowLeft') {
        stepLightbox(-1);
      }
    });
  }

  /* ============================================================
     工作流
     ============================================================ */

  function bindWorkflows() {
    if (refs.wfSearch) {
      var timer = null;
      refs.wfSearch.addEventListener('input', function () {
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () {
          state.wf.q = refs.wfSearch.value.trim();
          loadWorkflows(1);
        }, 260);
      });
    }
    if (refs.wfPoseBtn) {
      refs.wfPoseBtn.addEventListener('click', openPoseWorkflowModal);
    }
  }

  /* ---------------------------------------------------------- 生成姿势工作流

     SDXL + ControlNet-Union openpose 是「真 ControlNet」链路：骨架图作为结构条件
     注入 UNet，换骨架出图差异 MAD 50+；而 FLUX.2 Klein 的 ReferenceLatent 是
     注意力迁移，同一对骨架出图只差 1.97 —— 严格姿势控制必须走这条。
     两个变体：
       consistent = 锁脸 + 控姿（15 节点，带 IPAdapter FaceID 链）
       openpose   = 纯控姿（11 节点，跳过锁脸链）
     ------------------------------------------------------------------ */

  function openPoseWorkflowModal() {
    var wrap = ui().el('div', 'form-grid');
    var loading = ui().loadingInline('正在读取可用素材…');
    wrap.appendChild(loading);

    // 表单值集中放这里：modal 的 actions 不暴露按钮节点，submit 时再读，避免闭包捕获
    var form = { variant: 'consistent', ref: '', pose: '', positive: '', negative: '' };

    // 注意：ui().modal 的 action 不支持 id/disabled，且 onClick 返回非 false 会自动关闭。
    // 所以「生成」必须 close:false + 返回 false，由 submit 自己控制关闭时机。
    var modalRef = ui().modal({
      title: '生成姿势工作流',
      desc: 'SDXL + ControlNet-Union openpose · 真 ControlNet 控姿',
      body: wrap,
      actions: [
        { label: '取消', variant: 'ghost' },
        { label: '生成', variant: 'primary', close: false, onClick: submitPoseWorkflow }
      ],
      size: 'lg'
    });

    var assets = { references: [], poses: [] };

    function submitPoseWorkflow() {
      var btn = modalRef.root.querySelector('.modal-foot .btn-primary');
      var body = {
        variant: form.variant,
        pose_image: form.pose,
        positive: form.positive,
        negative: form.negative
      };
      if (form.variant === 'consistent' && form.ref) body.reference_image = form.ref;

      ui().withBusy(btn, '生成中…', api().post('/api/comic/poses/build-workflow', body)).then(
        function (data) {
          if (!data || !data.filename) {
            ui().toastError('生成失败：ComfyUI 工作流目录不可用');
            return;
          }
          ui().toastSuccess('已生成 ' + data.filename + '（' + data.nodes + ' 节点）');
          modalRef.close();
          // 落盘后必须同步一次，否则列表里看不到
          api().post('/api/sync/now', {}).then(function () { loadWorkflows(1); },
            function () { loadWorkflows(1); });
        },
        function (err) { ui().toastError(ui().errorText(err, '生成姿势工作流失败')); }
      );
      return false; // 保持弹窗打开
    }

    api().get('/api/comic/poses/assets', {}).then(function (data) {
      assets.references = (data && data.references) || [];
      assets.poses = (data && data.poses) || [];
      if (loading.parentNode) wrap.removeChild(loading);
      buildForm();
    }, function (err) {
      if (loading.parentNode) wrap.removeChild(loading);
      var errBox = ui().el('div', 'callout callout-error');
      errBox.textContent = ui().errorText(err, '读取素材失败');
      wrap.appendChild(errBox);
    });

    function buildForm() {
      // ① 变体选择
      var varField = ui().el('div', 'field');
      varField.appendChild(ui().el('label', 'field-label', '工作流变体'));
      var seg = ui().el('div', 'seg');

      var optConsistent = ui().el('button', 'seg-opt is-active', '锁脸 + 控姿（15 节点）');
      optConsistent.type = 'button';
      var optOpenpose = ui().el('button', 'seg-opt', '纯 openpose 控姿（11 节点）');
      optOpenpose.type = 'button';
      seg.appendChild(optConsistent);
      seg.appendChild(optOpenpose);
      varField.appendChild(seg);

      var varHint = ui().el('p', 'field-hint',
        '既要这个人、又要这个姿势。参考图需能检出正脸。');
      varField.appendChild(varHint);
      wrap.appendChild(varField);

      // ② 参考图（仅 consistent）
      var refSelect = null;
      var refField = ui().el('div', 'field');
      refField.appendChild(ui().el('label', 'field-label', '参考图（锁脸）'));
      if (assets.references.length) {
        refSelect = doc.createElement('select');
        refSelect.className = 'input';
        assets.references.forEach(function (r) {
          var o = doc.createElement('option');
          o.value = r.value;
          o.textContent = r.name;
          refSelect.appendChild(o);
        });
        refField.appendChild(refSelect);
      } else {
        var noRef = ui().el('p', 'field-hint',
          'input/aibar_poses/ 下没有参考图（需 actor_* / ref_* / face_* 命名）。留空将由系统自动扫描补齐。');
        refField.appendChild(noRef);
      }
      wrap.appendChild(refField);

      // ③ 骨架图
      var poseField = ui().el('div', 'field');
      poseField.appendChild(ui().el('label', 'field-label', '骨架图（控姿）'));
      var poseSelect = doc.createElement('select');
      poseSelect.className = 'input';
      var poseList = assets.poses || [];
      if (!poseList.length) {
        // 一张骨架都没有：留空会退化成 7 节点裸版（无控姿链），明确告诉用户
        var warn = ui().el('p', 'callout callout-warning',
          'input/aibar_poses/ 下没有骨架图。请先到「动作帧」页面生成预置姿势库，' +
          '否则生成的是 7 节点裸版（无法控姿）。');
        poseField.appendChild(warn);
      }
      poseList.forEach(function (p, i) {
        var o = doc.createElement('option');
        o.value = p.value;
        o.textContent = p.name;
        // 默认选第一张：留空会让纯 openpose 变体退化成没有控姿链的裸版
        if (i === 0) o.selected = true;
        poseSelect.appendChild(o);
      });
      form.pose = poseList.length ? poseList[0].value : '';
      poseField.appendChild(poseSelect);
      poseField.appendChild(ui().el('p', 'field-hint',
        poseList.length
          ? '可用 ' + poseList.length + ' 张骨架，默认选第一张。'
          : '可用 0 张骨架。'));
      wrap.appendChild(poseField);

      // ④ 提示词（可选）
      var posField = ui().el('div', 'field');
      posField.appendChild(ui().el('label', 'field-label', '正向提示词（可留空）'));
      var posInput = doc.createElement('input');
      posInput.className = 'input';
      posInput.type = 'text';
      posInput.placeholder = '1girl, long hair, blue eyes, anime style';
      posField.appendChild(posInput);
      wrap.appendChild(posField);

      var negField = ui().el('div', 'field');
      negField.appendChild(ui().el('label', 'field-label', '负向提示词（可留空）'));
      var negInput = doc.createElement('input');
      negInput.className = 'input';
      negInput.type = 'text';
      negInput.placeholder = 'lowres, bad anatomy, blurry, watermark';
      negField.appendChild(negInput);
      wrap.appendChild(negField);

      // 变体切换：纯 openpose 时隐藏参考图
      function applyVariant() {
        var isConsistent = form.variant === 'consistent';
        optConsistent.className = 'seg-opt' + (isConsistent ? ' is-active' : '');
        optOpenpose.className = 'seg-opt' + (isConsistent ? '' : ' is-active');
        refField.hidden = !isConsistent;
        varHint.textContent = isConsistent
          ? '既要这个人、又要这个姿势。参考图需能检出正脸。'
          : '只要姿势、不锁脸。跳过整条 IPAdapter 链，省一次人脸检测。';
      }
      optConsistent.addEventListener('click', function () { form.variant = 'consistent'; applyVariant(); });
      optOpenpose.addEventListener('click', function () { form.variant = 'openpose'; applyVariant(); });
      applyVariant();

      // 表单值 → form（submit 时统一读取）
      if (refSelect) {
        form.ref = refSelect.value;
        refSelect.addEventListener('change', function () { form.ref = refSelect.value; });
      }
      poseSelect.addEventListener('change', function () { form.pose = poseSelect.value; });
      posInput.addEventListener('input', function () { form.positive = posInput.value.trim(); });
      negInput.addEventListener('input', function () { form.negative = negInput.value.trim(); });
    }
  }

  function loadWorkflows(page) {
    if (!refs.wfGrid) return;
    state.wf.loaded = true;
    state.wf.page = page || state.wf.page || 1;
    refs.wfGrid.innerHTML = '';
    refs.wfGrid.appendChild(ui().skeletonCards(6));

    api().get('/api/workflows', {
      q: state.wf.q, page: state.wf.page, page_size: state.wf.pageSize
    }).then(function (data) {
      state.wf.items = (data && data.items) || [];
      state.wf.total = (data && data.total) || 0;
      // 筛选后总数可能骤减：把页码夹回合法区间，否则会停在空白页
      state.wf.page = ui().pagerModel(state.wf.page, state.wf.total, state.wf.pageSize).page;
      renderWorkflows();
    }, function (err) {
      refs.wfGrid.innerHTML = '';
      refs.wfGrid.appendChild(ui().errorState(ui().errorText(err, '工作流加载失败'), loadWorkflows));
    });
  }

  function renderWfPager() {
    if (!refs.wfPager) return;
    ui().pager(refs.wfPager, {
      page: state.wf.page,
      total: state.wf.total,
      pageSize: state.wf.pageSize,
      unit: '个工作流',
      onJump: function (page) { loadWorkflows(page); }
    });
  }

  function renderWorkflows() {
    refs.wfGrid.innerHTML = '';
    if (!state.wf.items.length) {
      refs.wfGrid.appendChild(ui().emptyState({
        icon: 'workflow',
        title: state.wf.q ? '没有匹配的工作流' : '还没有同步到工作流',
        desc: state.wf.q ? '换个关键词试试，可搜索名称、节点类型或提示词。' : '点击右上角「立即同步」从 ComfyUI 目录同步。'
      }));
      renderWfPager();
      return;
    }
    state.wf.items.forEach(function (item) {
      refs.wfGrid.appendChild(renderWorkflowCard(item));
    });
    renderWfPager();
  }

  function renderWorkflowCard(item) {
    var card = ui().el('article', 'wf-card');
    card.tabIndex = 0;
    card.setAttribute('aria-label', '工作流：' + item.name);

    card.appendChild(ui().el('h3', 'wf-name', item.name));

    var meta = ui().el('div', 'entry-foot');
    meta.appendChild(ui().el('span', 'badge', (item.node_count || 0) + ' 个节点'));
    if (item.synced_at) meta.appendChild(ui().el('span', '', '同步于 ' + ui().formatTime(item.synced_at)));
    card.appendChild(meta);

    var types = (item.node_types || []).slice(0, 5);
    if (types.length) {
      var typeRow = ui().el('div', 'chip-row');
      types.forEach(function (type) {
        typeRow.appendChild(ui().el('span', 'badge', type));
      });
      card.appendChild(typeRow);
    }

    if (item.positive_preview) {
      card.appendChild(ui().el('p', 'wf-prompt clamp-2', '正向：' + item.positive_preview));
    }
    if (item.negative_preview) {
      card.appendChild(ui().el('p', 'wf-prompt clamp-2', '负向：' + item.negative_preview));
    }

    var actions = ui().el('div', 'result-actions');
    // 主入口：把工作流载入 ComfyUI 界面，由用户在 ComfyUI 里点“运行”
    var openBtn = ui().el('button', 'btn btn-primary btn-sm', '用 ComfyUI 出图');
    openBtn.type = 'button';
    openBtn.title = '在 ComfyUI 界面中打开并载入该工作流';
    openBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      openWorkflowInComfy(item);
    });
    actions.appendChild(openBtn);

    // 次入口：不经界面，直接把工作流提交到 ComfyUI 队列
    var genBtn = ui().el('button', 'btn btn-secondary btn-sm', '后台出图');
    genBtn.type = 'button';
    genBtn.title = '不打开界面，直接提交到 ComfyUI 队列生成';
    genBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      generateWorkflow(item);
    });
    actions.appendChild(genBtn);

    var detail = ui().el('button', 'btn btn-secondary btn-sm', '查看详情');
    detail.type = 'button';
    detail.addEventListener('click', function () { openWorkflowDetail(item); });
    actions.appendChild(detail);

    var link = doc.createElement('a');
    link.className = 'btn btn-ghost btn-sm';
    link.href = '/download/workflow/' + encodeURIComponent(item.filename);
    link.setAttribute('download', '');
    link.innerHTML = AIBAR.icons.get('download', 14) + '<span>下载 JSON</span>';
    actions.appendChild(link);
    card.appendChild(actions);
    return card;
  }

  /* ============================================================
     用 ComfyUI 出图（联动 ComfyUI 后端生成）
     ============================================================ */

  function generateWorkflow(item) {
    var body = doc.createElement('div');
    var statusLine = ui().el('div', 'text-meta');
    statusLine.textContent = '准备提交到 ComfyUI…';
    body.appendChild(statusLine);

    var progress = ui().loadingInline('正在提交工作流到 ComfyUI…');
    body.appendChild(progress);

    var grid = ui().el('div', 'card-grid');
    body.appendChild(grid);

    ui().drawer({ title: '用 ComfyUI 出图', desc: item.name, body: body });

    api().post('/api/workflows/' + encodeURIComponent(item.filename) + '/generate', {}).then(
      function (data) {
        if (progress.parentNode) body.removeChild(progress);
        var promptId = data.prompt_id;
        var comfyUrl = data.comfyui_url;
        statusLine.textContent = '已提交（' + (data.node_count || 0) + ' 个节点），ComfyUI 生成中…';
        pollGenerate(promptId, statusLine, grid, comfyUrl);
      },
      function (err) {
        if (progress.parentNode) body.removeChild(progress);
        statusLine.className = 'callout callout-error';
        statusLine.textContent = ui().errorText(err, '出图提交失败');
        attachComfyLink(body);
      }
    );
  }

  function pollGenerate(promptId, statusLine, grid, comfyUrl) {
    var timer = setInterval(function () {
      api().get('/api/workflows/generate/' + encodeURIComponent(promptId), {}).then(
        function (res) {
          if (res.status === 'done') {
            clearInterval(timer);
            statusLine.textContent = '生成完成';
            renderGenerated(grid, res.outputs, comfyUrl);
          } else if (res.status === 'error') {
            clearInterval(timer);
            statusLine.className = 'callout callout-error';
            statusLine.textContent = '生成失败：' + (res.error || '未知错误');
            if (comfyUrl) addEditorLink(grid, comfyUrl);
          } else {
            statusLine.textContent = 'ComfyUI 生成中…（可保持此窗口，完成后自动刷新）';
          }
        },
        function (err) {
          clearInterval(timer);
          statusLine.className = 'callout callout-error';
          statusLine.textContent = '轮询失败：' + ui().errorText(err, '无法获取生成状态');
          if (comfyUrl) addEditorLink(grid, comfyUrl);
        }
      );
    }, 1500);
    // 安全上限：3 分钟后停止轮询，避免标签页长期挂着
    setTimeout(function () { clearInterval(timer); }, 180000);
  }

  function renderGenerated(grid, outputs, comfyUrl) {
    grid.innerHTML = '';
    var items = (outputs || []).filter(function (o) { return o && o.view_url; });
    if (!items.length) {
      grid.appendChild(ui().emptyState({
        icon: 'image',
        title: '没有产出图片',
        desc: '该工作流可能没有 SaveImage 节点，或生成尚未落盘。可在 ComfyUI 编辑器中查看。'
      }));
      if (comfyUrl) addEditorLink(grid, comfyUrl);
      return;
    }
    items.forEach(function (o) {
      var card = ui().el('button', 'gal-card');
      card.type = 'button';
      card.setAttribute('aria-label', '查看生成结果：' + (o.filename || ''));
      var img = doc.createElement('img');
      img.className = 'gal-thumb';
      img.loading = 'lazy';
      img.alt = o.filename || '生成图片';
      img.src = o.view_url;
      card.appendChild(img);
      var info = ui().el('div', 'gal-info');
      info.appendChild(ui().el('div', 'gal-name', o.filename || '未命名'));
      card.appendChild(info);
      card.addEventListener('click', function () {
        ui().copyText(o.view_url);
      });
      grid.appendChild(card);
    });
    if (comfyUrl) addEditorLink(grid, comfyUrl);
  }

  function addEditorLink(host, comfyUrl) {
    var actions = ui().el('div', 'result-actions');
    var editor = ui().el('a', 'btn btn-ghost btn-sm', '在 ComfyUI 编辑器中打开');
    editor.href = comfyUrl;
    editor.target = '_blank';
    editor.rel = 'noopener';
    actions.appendChild(editor);
    host.appendChild(actions);
  }

  function attachComfyLink(host) {
    if (!host) return;
    api().get('/api/comfyui/status', {}).then(function (d) {
      var url = 'http://' + (d.host || '127.0.0.1') + ':' + (d.port || 8188);
      addEditorLink(host, url);
    }, function () {
      addEditorLink(host, 'http://127.0.0.1:8188');
    });
  }

  function openComfyUIEditor() {
    api().get('/api/comfyui/status', {}).then(
      function (d) {
        var url = 'http://' + (d.host || '127.0.0.1') + ':' + (d.port || 8188);
        window.open(url, '_blank');
      },
      function () { window.open('http://127.0.0.1:8188', '_blank'); }
    );
  }

  /* ----------------------------------------------------------
     深链接：打开 ComfyUI 界面并载入工作流（+ 提示词）

     AIBAR 与 ComfyUI 不同源，无法直接往 ComfyUI 页面注入 loadGraphData，
     因此改用「URL 参数协议」：AIBAR 生成带 aibar_wf / aibar_prompt 的链接，
     ComfyUI 侧的 AIBAR-Bridge 扩展在启动时自行拉取并载入画布。
     桥梁未安装时退化为「只打开 ComfyUI 首页」并提示用户。
     ---------------------------------------------------------- */

  function openComfyDeepLink(query, onFailOpenPlain) {
    api().get('/api/comfyui/editor-link', query).then(function (d) {
      var url = (d && d.url) || '';
      if (!url) {
        ui().toast({ message: '无法生成 ComfyUI 链接', type: 'error' });
        return;
      }
      var win = window.open(url, '_blank');
      if (!win) {
        ui().toast({ message: '浏览器拦截了弹出窗口，请允许本站弹出窗口后重试', type: 'error' });
        return;
      }
      if (d && d.bridge_installed === false) {
        ui().toast({
          message: 'ComfyUI 未安装 AIBAR 桥梁扩展，已只打开界面。把 AIBAR-Bridge '
            + '放进 ComfyUI/custom_nodes 并重启 ComfyUI 后即可自动载入工作流。',
          type: 'warning'
        });
      } else if (d && d.workflow) {
        ui().toast({
          message: '正在 ComfyUI 中载入「' + (d.workflow_name || d.workflow) + '」'
            + (d.prompt ? '，并填入该提示词' : ''),
          type: 'success'
        });
      }
    }, function (err) {
      ui().toast({ message: ui().errorText(err, '无法生成 ComfyUI 链接'), type: 'error' });
      if (onFailOpenPlain) onFailOpenPlain();
    });
  }

  function openWorkflowInComfy(item) {
    openComfyDeepLink({ workflow: item.filename }, openComfyUIEditor);
  }

  function openImageInComfy(item) {
    openComfyDeepLink({ image_id: item.id }, openComfyUIEditor);
  }

  function openWorkflowDetail(item) {
    var body = doc.createElement('div');
    body.className = 'filter-grid';
    var loading = ui().loadingInline('正在读取节点清单…');
    body.appendChild(loading);

    ui().drawer({
      title: item.name,
      desc: '节点清单与提示词全文',
      wide: true,
      body: body
    });

    api().get('/api/workflows/' + encodeURIComponent(item.filename) + '/detail', {}).then(function (data) {
      body.innerHTML = '';
      body.appendChild(makeReadonly('文件名', data.filename || item.filename));
      body.appendChild(makeReadonly('节点数量', String(data.node_count || 0)));

      var types = data.node_types || [];
      if (types.length) {
        var wrap = ui().el('div', 'kv');
        wrap.appendChild(ui().el('span', 'kv-key', '节点类型'));
        var chipRow = ui().el('div', 'chip-row');
        types.forEach(function (type) { chipRow.appendChild(ui().el('span', 'badge', type)); });
        wrap.appendChild(chipRow);
        body.appendChild(wrap);
      }

      body.appendChild(makePromptBlock('正向提示词', data.positive_prompt || '（无）'));
      body.appendChild(makePromptBlock('负向提示词', data.negative_prompt || '（无）'));

      var nodes = data.nodes || [];
      var nodeWrap = ui().el('div', 'kv');
      nodeWrap.appendChild(ui().el('span', 'kv-key', '节点清单（' + nodes.length + '）'));
      if (nodes.length) {
        var list = ui().el('div', 'node-list');
        nodes.forEach(function (node) {
          var row = ui().el('div', 'node-row');
          row.appendChild(ui().el('span', 'badge', node.type || '—'));
          row.appendChild(ui().el('span', 'break-any', node.title || node.id || ''));
          list.appendChild(row);
        });
        nodeWrap.appendChild(list);
      } else {
        nodeWrap.appendChild(ui().el('div', 'text-meta text-tertiary', '源文件不可读取时只展示已解析摘要。'));
      }
      body.appendChild(nodeWrap);

      var act = ui().el('div', 'result-actions');
      var openBtn = ui().el('button', 'btn btn-primary btn-sm', '用 ComfyUI 出图');
      openBtn.type = 'button';
      openBtn.title = '在 ComfyUI 界面中打开并载入该工作流';
      openBtn.addEventListener('click', function () { openWorkflowInComfy(item); });
      act.appendChild(openBtn);

      var genBtn = ui().el('button', 'btn btn-secondary btn-sm', '后台出图');
      genBtn.type = 'button';
      genBtn.title = '不打开界面，直接提交到 ComfyUI 队列生成';
      genBtn.addEventListener('click', function () { generateWorkflow(item); });
      act.appendChild(genBtn);

      var editorBtn = ui().el('button', 'btn btn-ghost btn-sm', '只打开 ComfyUI');
      editorBtn.type = 'button';
      editorBtn.title = '不载入工作流，仅打开 ComfyUI 界面';
      editorBtn.addEventListener('click', function () { openComfyUIEditor(); });
      act.appendChild(editorBtn);
      body.appendChild(act);
    }, function (err) {
      body.innerHTML = '';
      body.appendChild(ui().errorState(ui().errorText(err, '详情加载失败')));
    });
  }

  function makeReadonly(label, value) {
    var wrap = ui().el('div', 'kv');
    wrap.appendChild(ui().el('span', 'kv-key', label));
    wrap.appendChild(ui().el('div', 'text-sm break-any', value));
    return wrap;
  }

  function makePromptBlock(label, text) {
    var wrap = ui().el('div', 'kv');
    var head = ui().el('div', 'result-block-head');
    head.appendChild(ui().el('span', '', label));
    head.appendChild(ui().el('span', 'grow'));
    var copy = ui().el('button', 'btn btn-ghost btn-sm');
    copy.type = 'button';
    copy.innerHTML = AIBAR.icons.get('copy', 14) + '<span>复制</span>';
    copy.addEventListener('click', function () { ui().copyText(text); });
    head.appendChild(copy);
    wrap.appendChild(head);
    wrap.appendChild(ui().el('div', 'result-text', text));
    return wrap;
  }

  /* ============================================================
     画廊
     ============================================================ */

  function bindGallery() {
    if (refs.galSearch) {
      var timer = null;
      refs.galSearch.addEventListener('input', function () {
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () {
          state.gal.q = refs.galSearch.value.trim();
          state.gal.page = 1;
          loadGallery(false);
        }, 300);
      });
    }
    if (refs.galMore) {
      refs.galMore.addEventListener('click', function () {
        state.gal.page += 1;
        loadGallery(true);
      });
    }
  }

  function loadGallery(append) {
    if (!refs.galGrid) return;
    state.gal.loaded = true;
    if (!append) {
      refs.galGrid.innerHTML = '';
      refs.galGrid.appendChild(ui().skeletonCards(6));
    }
    api().get('/api/gallery', { page: state.gal.page, page_size: state.gal.pageSize, q: state.gal.q })
      .then(function (data) {
        state.gal.total = (data && data.total) || 0;
        var items = (data && data.items) || [];
        state.gal.items = append ? state.gal.items.concat(items) : items;
        renderGallery(items, append);
      }, function (err) {
        refs.galGrid.innerHTML = '';
        refs.galGrid.appendChild(ui().errorState(ui().errorText(err, '图库加载失败'), function () { loadGallery(false); }));
      });
  }

  function renderGallery(items, append) {
    if (!append) refs.galGrid.innerHTML = '';
    if (refs.galTotal) refs.galTotal.textContent = '共 ' + state.gal.total + ' 张';

    if (!state.gal.items.length) {
      refs.galGrid.appendChild(ui().emptyState({
        icon: 'image',
        title: state.gal.q ? '没有匹配的图片' : '图库还是空的',
        desc: state.gal.q ? '试试其他关键词，可搜索文件名、提示词或关联工作流。' : '点击右上角「立即同步」把 ComfyUI 产出同步进来。'
      }));
      if (refs.galMore) refs.galMore.hidden = true;
      return;
    }

    items.forEach(function (item) {
      refs.galGrid.appendChild(renderGalleryCard(item));
    });
    if (refs.galMore) {
      refs.galMore.hidden = state.gal.items.length >= state.gal.total;
    }
  }

  function renderGalleryCard(item) {
    var card = ui().el('button', 'gal-card');
    card.type = 'button';
    card.setAttribute('aria-label', '查看图片：' + (item.filename || ''));

    var img = doc.createElement('img');
    img.className = 'gal-thumb';
    img.loading = 'lazy';
    img.alt = item.filename || '生成图片';
    img.src = item.url || (item.gallery_path ? '/static/' + item.gallery_path : '');
    card.appendChild(img);

    var info = ui().el('div', 'gal-info');
    info.appendChild(ui().el('div', 'gal-name', item.filename || '未命名'));
    info.appendChild(ui().el('div', 'text-tertiary',
      (item.width && item.height ? item.width + '×' + item.height : '尺寸未知')));
    card.appendChild(info);

    card.addEventListener('click', function () {
      state.gal.index = state.gal.items.indexOf(item);
      openLightbox(item.id);
    });
    return card;
  }

  function openLightbox(imageId) {
    api().get('/api/gallery/' + encodeURIComponent(imageId), {}).then(function (item) {
      closeLightbox();
      var box = ui().el('div', 'lightbox');
      box.id = 'gal-lightbox';
      box.setAttribute('role', 'dialog');
      box.setAttribute('aria-modal', 'true');
      box.setAttribute('aria-label', '图片详情：' + (item.filename || ''));

      var stage = ui().el('div', 'lightbox-stage');
      var img = doc.createElement('img');
      img.src = item.url || (item.gallery_path ? '/static/' + item.gallery_path : '');
      img.alt = item.filename || '生成图片';
      stage.appendChild(img);
      box.appendChild(stage);

      var side = ui().el('div', 'lightbox-side');
      var head = ui().el('div', 'drawer-head');
      var textWrap = ui().el('div');
      textWrap.appendChild(ui().el('h2', 'modal-title', item.filename || '未命名'));
      head.appendChild(textWrap);
      var closeBtn = ui().el('button', 'icon-btn');
      closeBtn.type = 'button';
      closeBtn.setAttribute('aria-label', '关闭图片详情');
      closeBtn.innerHTML = AIBAR.icons.get('close', 18);
      closeBtn.addEventListener('click', closeLightbox);
      head.appendChild(closeBtn);
      side.appendChild(head);

      var body = ui().el('div', 'lightbox-body');
      body.appendChild(makeReadonly('来源文件', item.filename || '—'));
      body.appendChild(makeReadonly('生成时间', ui().formatTime(item.created_at)));
      body.appendChild(makeReadonly('尺寸', (item.width && item.height)
        ? item.width + ' × ' + item.height : '—'));
      body.appendChild(makeReadonly('文件大小', ui().formatBytes(item.size_bytes)));
      body.appendChild(makeReadonly('关联工作流', item.workflow_link || '未识别'));
      body.appendChild(makePromptBlock('内嵌提示词', item.prompt || '（该图片没有内嵌提示词）'));

      var actions = ui().el('div', 'rev-actions');
      var reverseBtn = ui().el('button', 'btn btn-primary btn-sm');
      reverseBtn.type = 'button';
      reverseBtn.innerHTML = AIBAR.icons.get('image', 14) + '<span>反推提示词</span>';
      reverseBtn.addEventListener('click', function () {
        closeLightbox();
        AIBAR.studio.enterReverseWithImage(item.id);
      });
      actions.appendChild(reverseBtn);

      var comfyBtn = ui().el('button', 'btn btn-secondary btn-sm');
      comfyBtn.type = 'button';
      comfyBtn.title = '在 ComfyUI 界面中打开该图片关联的工作流，并填入内嵌提示词';
      comfyBtn.innerHTML = AIBAR.icons.get('external', 14) + '<span>在 ComfyUI 中打开</span>';
      comfyBtn.addEventListener('click', function () { openImageInComfy(item); });
      actions.appendChild(comfyBtn);

      var copyBtn = ui().el('button', 'btn btn-secondary btn-sm', '复制提示词');
      copyBtn.type = 'button';
      copyBtn.addEventListener('click', function () { ui().copyText(item.prompt || ''); });
      actions.appendChild(copyBtn);
      body.appendChild(actions);
      side.appendChild(body);
      box.appendChild(side);

      box.addEventListener('mousedown', function (event) {
        if (event.target === stage) closeLightbox();
      });

      doc.body.appendChild(box);
      state.lightbox = box;
      closeBtn.focus();
    }, function (err) {
      ui().toastError(ui().errorText(err, '无法读取图片详情'));
    });
  }

  function closeLightbox() {
    if (state.lightbox && state.lightbox.parentNode) {
      state.lightbox.parentNode.removeChild(state.lightbox);
    }
    state.lightbox = null;
  }

  function stepLightbox(delta) {
    if (!state.gal.items.length) return;
    var next = state.gal.index + delta;
    if (next < 0 || next >= state.gal.items.length) return;
    state.gal.index = next;
    openLightbox(state.gal.items[next].id);
  }

  /* ============================================================
     案例库（按风格分组）
     ============================================================ */

  function bindCases() {
    /* 分组由接口返回，筛选通过 style 参数 */
  }

  function loadCases(page) {
    if (!refs.caseGrid) return;
    state.cases.loaded = true;
    state.cases.page = page || state.cases.page || 1;
    refs.caseGrid.innerHTML = '';
    refs.caseGrid.appendChild(ui().loadingInline('正在加载案例…'));

    api().get('/api/cases', {
      style: state.cases.style, page: state.cases.page, page_size: state.cases.pageSize
    }).then(function (data) {
      state.cases.items = (data && data.items) || [];
      state.cases.styles = (data && data.styles) || [];
      state.cases.total = (data && data.total) || 0;
      // 切换风格后总数会变，页码要跟着夹回合法区间
      state.cases.page = ui().pagerModel(state.cases.page, state.cases.total, state.cases.pageSize).page;
      renderCases();
    }, function (err) {
      refs.caseGrid.innerHTML = '';
      refs.caseGrid.appendChild(ui().errorState(ui().errorText(err, '案例加载失败'), loadCases));
    });
  }

  function renderCasePager() {
    if (!refs.casePager) return;
    ui().pager(refs.casePager, {
      page: state.cases.page,
      total: state.cases.total,
      pageSize: state.cases.pageSize,
      unit: '个案例',
      onJump: function (page) { loadCases(page); }
    });
  }

  function renderCases() {
    refs.caseGrid.innerHTML = '';
    renderCaseStyles();

    if (!state.cases.items.length) {
      refs.caseGrid.appendChild(ui().emptyState({
        icon: 'layers',
        title: '没有可展示的案例',
        desc: '带内嵌提示词的图片会自动进入案例库，并按关联工作流分组。'
      }));
      renderCasePager();
      return;
    }

    var groups = {};
    var order = [];
    state.cases.items.forEach(function (item) {
      var key = item.style || '未分类';
      if (!groups[key]) {
        groups[key] = [];
        order.push(key);
      }
      groups[key].push(item);
    });

    // 分组标题上的数量取自接口的 styles（该风格在**当前筛选下**的全部条数），
    // 不能用本页卡片数——分页后一页只有 24 张，写「3 个作品」会让人以为就这些。
    var totals = {};
    (state.cases.styles || []).forEach(function (style) {
      totals[style.key] = style.count || 0;
    });

    order.forEach(function (key) {
      var section = ui().el('section', 'case-group');
      var head = ui().el('div', 'case-group-head');
      head.appendChild(ui().el('h3', 'text-section', key));
      head.appendChild(ui().el('span', 'badge', caseGroupLabel(key, groups[key].length, totals[key])));
      section.appendChild(head);

      var grid = ui().el('div', 'card-grid');
      groups[key].forEach(function (item) {
        grid.appendChild(renderCaseCard(item));
      });
      section.appendChild(grid);
      refs.caseGrid.appendChild(section);
    });
    renderCasePager();
  }

  /**
   * 分组标题的数量文案：本页条数与总数不一致时写成「本页 3 / 共 150 个作品」，
   * 让用户知道列表被分页了，而不是以为这个风格只有 3 张。
   */
  function caseGroupLabel(key, shown, total) {
    var count = Number(total || 0);
    if (count && count !== shown) {
      return '本页 ' + shown + ' / 共 ' + count + ' 个作品';
    }
    return shown + ' 个作品';
  }

  function renderCaseCard(item) {
    // 用 article 而不是 button：卡片内还要放「在 ComfyUI 中打开」按钮，
    // 嵌套 button 是非法 HTML，且点击目标会互相吞掉。
    var card = ui().el('article', 'wf-card');
    card.tabIndex = 0;
    card.style.cursor = 'pointer';
    card.setAttribute('aria-label', '查看案例：' + item.title);

    if (item.image_path) {
      var img = doc.createElement('img');
      img.className = 'gal-thumb';
      img.loading = 'lazy';
      img.alt = item.title || '案例图片';
      img.src = item.image_path;
      card.appendChild(img);
    }
    card.appendChild(ui().el('h4', 'wf-name', item.title || '未命名作品'));
    if (item.workflow_name) {
      card.appendChild(ui().el('span', 'badge', item.workflow_name));
    }
    if (item.prompt) {
      card.appendChild(ui().el('p', 'wf-prompt clamp-2', item.prompt));
    }

    var actions = ui().el('div', 'result-actions');
    var openBtn = ui().el('button', 'btn btn-primary btn-sm', '在 ComfyUI 中打开');
    openBtn.type = 'button';
    openBtn.title = '载入该案例关联的工作流，并填入这条提示词';
    openBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      openImageInComfy(item);
    });
    actions.appendChild(openBtn);

    var detailBtn = ui().el('button', 'btn btn-ghost btn-sm', '查看详情');
    detailBtn.type = 'button';
    detailBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      openLightbox(item.id);
    });
    actions.appendChild(detailBtn);
    card.appendChild(actions);

    card.addEventListener('click', function () {
      openLightbox(item.id);
    });
    card.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openLightbox(item.id);
      }
    });
    return card;
  }

  function renderCaseStyles() {
    if (!refs.caseStyles) return;
    refs.caseStyles.innerHTML = '';
    var all = ui().el('button', 'chip');
    all.type = 'button';
    all.appendChild(ui().el('span', 'chip-value', state.cases.style ? '全部风格' : '全部风格'));
    if (!state.cases.style) all.classList.add('badge-brand');
    all.addEventListener('click', function () {
      if (!state.cases.style) return;
      state.cases.style = '';
      loadCases(1);
    });
    refs.caseStyles.appendChild(all);

    state.cases.styles.slice(0, 20).forEach(function (style) {
      var chip = ui().el('button', 'chip');
      chip.type = 'button';
      chip.appendChild(ui().el('span', 'chip-value', style.key));
      chip.appendChild(ui().el('span', 'chip-label', String(style.count || 0)));
      if (state.cases.style === style.key) chip.classList.add('badge-brand');
      chip.addEventListener('click', function () {
        state.cases.style = state.cases.style === style.key ? '' : style.key;
        loadCases(1);
      });
      refs.caseStyles.appendChild(chip);
    });
  }

  /* ============================================================
     模型
     ============================================================ */

  function bindModels() {
    if (refs.modelSearch) {
      var timer = null;
      refs.modelSearch.addEventListener('input', function () {
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () {
          state.models.q = refs.modelSearch.value.trim();
          loadModels(false);
        }, 260);
      });
    }
  }

  function loadModels(refresh) {
    if (!refs.modelBody) return;
    state.models.loaded = true;
    refs.modelBody.innerHTML = '';
    var row = doc.createElement('tr');
    var cell = doc.createElement('td');
    cell.colSpan = 4;
    cell.appendChild(ui().loadingInline('正在扫描模型目录…'));
    row.appendChild(cell);
    refs.modelBody.appendChild(row);

    api().get('/api/models', { q: state.models.q, refresh: refresh ? 1 : '' }).then(function (data) {
      state.models.items = (data && data.items) || [];
      renderModels();
      if (refs.modelTotal) refs.modelTotal.textContent = '共 ' + ((data && data.total) || 0) + ' 个模型';
    }, function (err) {
      refs.modelBody.innerHTML = '';
      var errorRow = doc.createElement('tr');
      var errorCell = doc.createElement('td');
      errorCell.colSpan = 4;
      errorCell.appendChild(ui().errorState(ui().errorText(err, '模型扫描失败'), function () { loadModels(false); }));
      errorRow.appendChild(errorCell);
      refs.modelBody.appendChild(errorRow);
    });
  }

  function renderModels() {
    refs.modelBody.innerHTML = '';
    if (!state.models.items.length) {
      var row = doc.createElement('tr');
      var cell = doc.createElement('td');
      cell.colSpan = 4;
      cell.appendChild(ui().emptyState({
        icon: 'cube',
        title: '没有匹配的模型',
        desc: state.models.q ? '换个关键词试试。' : '配置 ComfyUI 模型目录后即可在这里看到模型清单。'
      }));
      row.appendChild(cell);
      refs.modelBody.appendChild(row);
      return;
    }
    state.models.items.forEach(function (item) {
      var tr = doc.createElement('tr');
      [item.name || '—', item.type || '—', item.path || '—', ui().formatBytes(item.size_bytes)]
        .forEach(function (text, index) {
          var td = doc.createElement('td');
          td.textContent = text;
          if (index === 0) td.style.color = 'var(--color-text-primary)';
          tr.appendChild(td);
        });
      refs.modelBody.appendChild(tr);
    });
  }

  /* ============================================================
     同步日志
     ============================================================ */

  function bindLogs() {
    if (refs.logLimit) {
      refs.logLimit.addEventListener('change', function () {
        state.logs.limit = parseInt(refs.logLimit.value, 10) || 50;
        loadLogs();
      });
    }
  }

  function loadLogs() {
    if (!refs.logList) return;
    state.logs.loaded = true;
    refs.logList.innerHTML = '';
    refs.logList.appendChild(ui().loadingInline('正在加载日志…'));

    api().get('/api/logs', { limit: state.logs.limit }).then(function (data) {
      state.logs.items = (data && data.items) || [];
      renderLogs();
    }, function (err) {
      refs.logList.innerHTML = '';
      refs.logList.appendChild(ui().errorState(ui().errorText(err, '日志加载失败'), loadLogs));
    });
  }

  function logTone(status) {
    var value = String(status || '').toLowerCase();
    if (value === 'error' || value === 'failed') return 'badge-error';
    if (value === 'warn' || value === 'warning') return 'badge-warning';
    if (value === 'ok' || value === 'success') return 'badge-success';
    return 'badge';
  }

  function renderLogs() {
    refs.logList.innerHTML = '';
    if (!state.logs.items.length) {
      refs.logList.appendChild(ui().emptyState({
        icon: 'list',
        title: '暂无同步日志',
        desc: '同步、启动 ComfyUI 和反推任务都会在这里留下记录。'
      }));
      return;
    }
    state.logs.items.forEach(function (item) {
      var row = ui().el('div', 'log-row');
      row.appendChild(ui().el('span', 'log-time', ui().formatTime(item.ts)));
      row.appendChild(ui().el('span', 'log-type', item.type || '—'));
      row.appendChild(ui().el('span', 'badge ' + logTone(item.status), item.status || '—'));
      row.appendChild(ui().el('span', 'log-msg break-any', item.message || ''));
      refs.logList.appendChild(row);
    });
  }

  /* ============================================================
     页面进入时的加载与顶部操作
     ============================================================ */

  function onEnter(tab) {
    if (tab === 'workflows') {
      if (!state.wf.loaded) loadWorkflows();
      setTopbarActions([{
        id: 'btn-wf-refresh',
        label: '刷新',
        icon: 'refresh',
        onClick: function () { loadWorkflows(); }
      }]);
    } else if (tab === 'gallery') {
      if (!state.gal.loaded) loadGallery(false);
      setTopbarActions([{
        id: 'btn-gal-refresh',
        label: '刷新',
        icon: 'refresh',
        onClick: function () { state.gal.page = 1; loadGallery(false); }
      }]);
    } else if (tab === 'cases') {
      if (!state.cases.loaded) loadCases();
      setTopbarActions([{
        id: 'btn-case-refresh',
        label: '刷新',
        icon: 'refresh',
        onClick: function () { loadCases(); }
      }]);
    } else if (tab === 'models') {
      if (!state.models.loaded) loadModels(false);
      setTopbarActions([{
        id: 'btn-model-rescan',
        label: '重新扫描',
        icon: 'refresh',
        onClick: function () { loadModels(true); }
      }]);
    } else if (tab === 'logs') {
      if (!state.logs.loaded) loadLogs();
      setTopbarActions([{
        id: 'btn-log-refresh',
        label: '刷新',
        icon: 'refresh',
        onClick: function () { loadLogs(); }
      }]);
    } else {
      setTopbarActions([]);
    }
  }

  /** 顶部上下文栏的页面级操作（同步按钮始终保留） */
  function setTopbarActions(actions) {
    var host = doc.getElementById('topbar-actions');
    if (!host) return;
    host.innerHTML = '';
    (actions || []).forEach(function (action) {
      var btn = ui().el('button', 'btn btn-secondary btn-sm');
      btn.type = 'button';
      if (action.id) btn.id = action.id;
      btn.innerHTML = AIBAR.icons.get(action.icon || 'refresh', 14) +
        '<span class="btn-label">' + ui().escapeHtml(action.label) + '</span>';
      btn.addEventListener('click', action.onClick);
      host.appendChild(btn);
    });
  }

  /** 同步完成后刷新依赖数据的页面。
      这里一律回到第 1 页：同步进来的是最新内容，停在原页码会让人以为没同步到。 */
  function refreshAll() {
    if (state.wf.loaded) loadWorkflows(1);
    if (state.gal.loaded) {
      state.gal.page = 1;
      loadGallery(false);
    }
    if (state.cases.loaded) loadCases(1);
    if (state.logs.loaded) loadLogs();
  }

  AIBAR.pages = {
    init: init,
    onEnter: onEnter,
    refreshAll: refreshAll
  };
})(typeof window !== 'undefined' ? window : globalThis);
