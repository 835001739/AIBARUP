/* ============================================================
   AIBAR · 视频转帧（M15）
   - 导入视频（上传文件 / 指定本机路径）→ 用 ffprobe 取元信息
   - 抽序列帧：按 fps / 宽度 / 时间区间把视频拆成一批有序 JPG
   - 转 GIF：**从已抽出的帧**合成（不是从原视频重抽），保证预览与产物一致
   - 序列帧连播：复用通用播放器（static/js/player.js），与组图连播同一份代码
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  // variant：每张卡当前选中的帧变体——original（抽帧原图）/ nobg（去背景透明图）。
  // 没去过背景的视频恒为 original，此时卡上也不显示切换控件。
  var state = { keyword: '', variant: {} };

  function api() { return AIBAR.api; }
  function ui() { return AIBAR.ui; }

  // 当前生效的变体：只有确实去过背景、且用户选中透明版时才走 nobg
  function variantOf(c) {
    return (c && c.has_nobg && state.variant[c.id] === 'nobg') ? 'nobg' : 'original';
  }

  // 帧 URL 的变体后缀（原图帧无需带参数，后端默认就是 original）
  function frameSuffix(c) {
    return variantOf(c) === 'nobg' ? '?variant=nobg' : '';
  }
  // doc 在 Node 测试环境下是 null（本文件会被 tests/js 直接 require），
  // 必须空值短路，否则连「加载模块」都会抛异常。
  function host() { return doc ? doc.getElementById('video-root') : null; }
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
    // 只加实际存在的修饰类（components.css 里只有 brand/success/warning/error/count），
    // 拼一个不存在的 `badge-muted` 是无效类名，白白让样式掉底。
    var cls = 'badge';
    if (tone === 'success') cls += ' badge-success';
    else if (tone === 'warning') cls += ' badge-warning';
    else if (tone === 'error') cls += ' badge-error';
    return ui().el('span', cls, text);
  }

  /* ---------------------------------------------------------- 初始化 */

  function init() {
    var h = host();
    if (!h) return;
    if (h.dataset.ready === '1') return;
    h.dataset.ready = '1';
    renderShell(h);
    loadClips();
  }

  function onEnter() {
    var h = host();
    if (!h) return;
    if (h.dataset.ready !== '1') { init(); return; }
    loadClips();
  }

  function renderShell(h) {
    h.innerHTML = '';

    var toolbar = ui().el('div', 'page-toolbar');

    var search = ui().el('div', 'search');
    var input = ui().el('input', 'input');
    input.type = 'search';
    input.placeholder = '搜索视频名称';
    input.value = state.keyword;
    input.addEventListener('input', function () {
      state.keyword = input.value.trim();
      loadClips();
    });
    search.appendChild(input);
    toolbar.appendChild(search);

    toolbar.appendChild(ui().el('span', 'grow'));

    toolbar.appendChild(btn('primary', 'upload', '导入视频', function () { openImport(); }));
    toolbar.appendChild(btn('ghost', 'link', '导入本地路径', function () { openImportPath(); }));
    toolbar.appendChild(btn('ghost', 'refresh', '刷新', function () { loadClips(); }));

    h.appendChild(toolbar);

    var grid = ui().el('div', 'card-grid card-grid--video');
    grid.setAttribute('role', 'list');
    h.appendChild(grid);
  }

  /* ---------------------------------------------------------- 列表 */

  function loadClips() {
    var h = host();
    if (!h) return;
    var grid = h.querySelector('.card-grid');
    if (!grid) return;

    grid.innerHTML = '';
    grid.appendChild(ui().loadingInline('加载视频…'));

    api().get('/api/video/clips?keyword=' + encodeURIComponent(state.keyword) + '&limit=60').then(function (d) {
      var items = (d && d.items) || [];
      renderClips(grid, items);
    }, function (err) {
      grid.innerHTML = '';
      grid.appendChild(ui().errorState
        ? ui().errorState(ui().errorText(err, '加载失败'))
        : ui().el('p', 'entry-text text-tertiary', ui().errorText(err, '加载失败')));
    });
  }

  function renderClips(grid, items) {
    grid.innerHTML = '';
    if (!items.length) {
      grid.appendChild(ui().emptyState
        ? ui().emptyState({
            icon: 'play',
            title: '还没有视频',
            desc: '点「导入视频」上传，或「导入本地路径」登记本机已有的视频'
          })
        : ui().el('p', 'entry-text text-tertiary', '还没有视频'));
      return;
    }
    items.forEach(function (c) { grid.appendChild(renderCard(c)); });
  }

  function renderCard(c) {
    var card = ui().el('div', 'entry-card video-card');
    card.setAttribute('role', 'listitem');

    // 封面：优先第一帧，没抽帧则显示占位。透明版帧用棋盘格背景托底，
    // 否则用户分不清「绿底没抠掉」和「已经抠透明了」。
    var v = variantOf(c);
    var thumb = ui().el('div', 'video-cover' + (v === 'nobg' ? ' video-cover--checker' : ''));
    if (c.frame_count_actual > 0) {
      var img = ui().el('img', 'video-cover-img');
      img.src = '/api/video/clips/' + c.id + '/frame/'
        + (v === 'nobg' ? 'frame_0001.png' : 'frame_0001.jpg') + frameSuffix(c);
      img.alt = c.name || '视频';
      img.loading = 'lazy';
      thumb.appendChild(img);
    } else {
      thumb.appendChild(ui().el('span', 'video-cover-empty', '未抽帧'));
    }
    card.appendChild(thumb);

    var body = ui().el('div', 'entry-body');

    var title = ui().el('h3', 'entry-title clamp-1', c.name || '未命名');
    title.title = c.name || '';
    body.appendChild(title);

    var meta = ui().el('p', 'entry-text text-tertiary clamp-2');
    var dur = c.duration ? c.duration.toFixed(1) + 's' : '—';
    var res = (c.width && c.height) ? (c.width + '×' + c.height) : '—';
    meta.textContent = dur + ' · ' + res + ' · ' + (c.src_fps ? c.src_fps + 'fps' : '—');
    body.appendChild(meta);

    // 复用组图的 shot-card-head（flex + gap），不另造同款样式
    var statusRow = ui().el('div', 'shot-card-head video-status-row');
    statusRow.appendChild(badge(statusText(c.status), statusTone(c.status)));
    if (c.frame_count_actual > 0) {
      statusRow.appendChild(badge(c.frame_count_actual + ' 帧', ''));
    }
    if (c.has_gif) {
      statusRow.appendChild(badge(ui().formatBytes ? ui().formatBytes(c.gif_bytes) : (c.gif_bytes + ' B'), 'success'));
    }
    if (c.has_nobg) {
      statusRow.appendChild(badge('已抠图', 'success'));
    }
    if (c.has_sheet) {
      statusRow.appendChild(badge('已拼图', 'success'));
    }
    body.appendChild(statusRow);

    // 变体切换：只有真去过背景才给切，否则切了是个空清单更让人困惑
    if (c.has_nobg) {
      body.appendChild(renderVariantSwitch(c));
    }

    if (c.last_error) {
      var err = ui().el('p', 'video-error clamp-2', c.last_error);
      err.title = c.last_error;
      body.appendChild(err);
    }

    card.appendChild(body);

    var actions = ui().el('div', 'video-actions');
    actions.appendChild(btn('ghost', 'layers', '抽帧', function () { openExtract(c); }));
    actions.appendChild(btn('ghost', 'sparkles', '转 GIF', function () { doGif(c); }, '需先抽帧'));
    if (c.frame_count_actual > 0) {
      actions.appendChild(btn('secondary', 'play', '播放', function () { openPlayer(c); }, '序列帧连播'));
      actions.appendChild(btn('ghost', 'filter', '去背景', function () { openRemoveBg(c); }, '按颜色抠掉背景'));
      actions.appendChild(btn('ghost', 'image', '拼图', function () { openSheet(c); }, '拼成一张序列帧大图'));
    }
    if (c.has_gif) {
      actions.appendChild(btn('ghost', 'download', 'GIF', function () { downloadGif(c); }));
    }
    if (c.has_sheet) {
      actions.appendChild(btn('ghost', 'download', '大图', function () { downloadSheet(c); }));
    }
    actions.appendChild(btn('ghost', 'trash', '删除', function () { doDelete(c); }));
    card.appendChild(actions);

    return card;
  }

  // 原始 / 透明两套帧的切换。切换后封面、连播、拼图、GIF 全部跟着走。
  function renderVariantSwitch(c) {
    var wrap = ui().el('div', 'video-variant');
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', '帧变体');
    [['original', '原图'], ['nobg', '透明']].forEach(function (pair) {
      var b = ui().el('button', 'video-variant-btn' + (variantOf(c) === pair[0] ? ' is-active' : ''));
      b.type = 'button';
      b.appendChild(ui().el('span', null, pair[1]));
      b.addEventListener('click', function () {
        state.variant[c.id] = pair[0];
        loadClips();
      });
      wrap.appendChild(b);
    });
    return wrap;
  }

  function statusText(s) {
    return { idle: '待抽帧', extracting: '抽帧中', ready: '已抽帧', failed: '失败' }[s] || s || '—';
  }

  function statusTone(s) {
    // 只返回 components.css 里真实存在的修饰类对应的 tone
    return { idle: '', extracting: 'warning', ready: 'success', failed: 'error' }[s] || '';
  }

  /* ---------------------------------------------------------- 导入 */

  function openImport() {
    var body = ui().el('div', 'form-stack');
    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '选择本地视频文件上传。导入后可在卡片上「抽帧」拆成序列帧，'
      + '再「转 GIF」合成动图。支持 mp4 / mov / webm / avi / mkv 等。';
    body.appendChild(tip);

    var fileInput = ui().el('input', 'input');
    fileInput.type = 'file';
    fileInput.accept = 'video/*,.gif';
    body.appendChild(ui().fieldRow ? ui().fieldRow('视频文件', fileInput) : fileInput);

    var nameInput = ui().el('input', 'input');
    nameInput.placeholder = '留空则用文件名';
    body.appendChild(ui().fieldRow ? ui().fieldRow('名称（可选）', nameInput) : nameInput);

    ui().modal({
      title: '导入视频',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '导入',
          variant: 'primary',
          onClick: function () {
            var f = fileInput.files && fileInput.files[0];
            if (!f) { ui().toastError('请先选择视频文件'); return false; }
            var fd = new root.FormData();
            fd.append('file', f);
            if (nameInput.value.trim()) fd.append('name', nameInput.value.trim());
            // 上传用 multipart，api() 的 JSON 封装不适合，这里直接 fetch
            root.fetch('/api/video/clips', { method: 'POST', body: fd }).then(function (r) {
              return r.json();
            }).then(function (res) {
              if (!res || !res.ok) {
                ui().toastError((res && res.error && res.error.message) || '导入失败');
                return;
              }
              ui().toastSuccess('已导入：' + (res.data.name || ''));
              loadClips();
            }, function () { ui().toastError('导入失败：网络错误'); });
            return true;
          }
        }
      ]
    });
  }

  function openImportPath() {
    var body = ui().el('div', 'form-stack');
    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '登记本机已有的视频文件（**不复制**，只记路径）。适合已经躺在磁盘上的大视频。';
    body.appendChild(tip);

    var pathInput = ui().el('input', 'input');
    pathInput.placeholder = '/Users/.../clip.mp4';
    body.appendChild(ui().fieldRow ? ui().fieldRow('绝对路径', pathInput) : pathInput);

    ui().modal({
      title: '导入本地视频路径',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '导入',
          variant: 'primary',
          onClick: function () {
            var p = pathInput.value.trim();
            if (!p) { ui().toastError('请填写路径'); return false; }
            api().post('/api/video/clips/import-path', { path: p }).then(function () {
              ui().toastSuccess('已导入');
              loadClips();
            }, function (err) { ui().toastError(ui().errorText(err, '导入失败')); });
            return true;
          }
        }
      ]
    });
  }

  /* ---------------------------------------------------------- 抽帧 */

  function openExtract(clip) {
    var body = ui().el('div', 'form-stack');
    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '抽帧会先清空该视频已有的帧再重新生成，不会出现新旧参数的文件混在一起。'
      + '帧数越多 GIF 越大，一般 100 帧内比较合适。';
    body.appendChild(tip);

    var fpsInput = ui().el('input', 'input');
    fpsInput.type = 'number';
    fpsInput.min = '1';
    fpsInput.max = '30';
    fpsInput.value = String(clip.fps || 8);
    body.appendChild(ui().fieldRow ? ui().fieldRow('每秒帧数 (fps)', fpsInput, '1–30，GIF 常用 8') : fpsInput);

    var maxInput = ui().el('input', 'input');
    maxInput.type = 'number';
    maxInput.min = '1';
    maxInput.max = '1000';
    maxInput.value = String(clip.max_frames || 300);
    body.appendChild(ui().fieldRow ? ui().fieldRow('最多帧数', maxInput, '硬上限 1000') : maxInput);

    var wInput = ui().el('input', 'input');
    wInput.type = 'number';
    wInput.min = '64';
    wInput.max = '1920';
    wInput.value = String(clip.scale_width || 480);
    body.appendChild(ui().fieldRow ? ui().fieldRow('输出宽度', wInput, '高度按比例') : wInput);

    var grid2 = ui().el('div', 'form-grid-2');
    var startInput = ui().el('input', 'input');
    startInput.type = 'number';
    startInput.step = '0.1';
    startInput.min = '0';
    startInput.placeholder = '0';
    if (clip.start_sec != null) startInput.value = String(clip.start_sec);
    var endInput = ui().el('input', 'input');
    endInput.type = 'number';
    endInput.step = '0.1';
    endInput.min = '0';
    endInput.placeholder = '留空到结尾';
    if (clip.end_sec != null) endInput.value = String(clip.end_sec);
    grid2.appendChild(ui().fieldRow ? ui().fieldRow('起始秒', startInput) : startInput);
    grid2.appendChild(ui().fieldRow ? ui().fieldRow('结束秒', endInput) : endInput);
    body.appendChild(grid2);

    ui().modal({
      title: '抽序列帧 · ' + (clip.name || ''),
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '开始抽帧',
          variant: 'primary',
          onClick: function () {
            var payload = {
              fps: parseInt(fpsInput.value, 10) || undefined,
              max_frames: parseInt(maxInput.value, 10) || undefined,
              scale_width: parseInt(wInput.value, 10) || undefined,
              start_sec: startInput.value === '' ? null : parseFloat(startInput.value),
              end_sec: endInput.value === '' ? null : parseFloat(endInput.value)
            };
            api().post('/api/video/clips/' + clip.id + '/extract', payload).then(function (r) {
              ui().toastSuccess('已抽出 ' + (r.frames || 0) + ' 帧');
              loadClips();
            }, function (err) { ui().toastError(ui().errorText(err, '抽帧失败')); });
            return true;
          }
        }
      ]
    });
  }

  /* ---------------------------------------------------------- 播放 / GIF / 删除 */

  function openPlayer(clip) {
    var v = variantOf(clip);
    // 变体走查询参数；后端没去过背景时会返回空清单，播放器会提示先抽帧
    api().get('/api/video/clips/' + clip.id + '/frames' + (v === 'nobg' ? '?variant=nobg' : '')).then(function (d) {
      var items = (d && d.items) || [];
      AIBAR.player.open({
        title: (d.name || clip.name || '序列帧') + (v === 'nobg' ? '（透明版）' : ''),
        items: items.map(function (it) { return { url: it.url, label: it.label || '' }; }),
        interval: d.interval,
        loop: d.loop !== false,
        emptyHint: '还没有序列帧：先在卡片上「抽帧」'
      });
    }, function (err) { ui().toastError(ui().errorText(err, '加载帧失败')); });
  }

  function doGif(clip) {
    var v = variantOf(clip);
    api().post('/api/video/clips/' + clip.id + '/gif', { variant: v }).then(function (r) {
      var size = ui().formatBytes ? ui().formatBytes(r.bytes) : r.bytes + ' B';
      ui().toastSuccess('GIF 已生成' + (v === 'nobg' ? '（透明版）' : '') + '（' + size + '）');
      loadClips();
    }, function (err) { ui().toastError(ui().errorText(err, '生成 GIF 失败')); });
  }

  function downloadGif(clip) {
    var a = doc.createElement('a');
    a.href = '/api/video/clips/' + clip.id + '/gif';
    a.download = (clip.name || 'clip') + '.gif';
    doc.body.appendChild(a);
    a.click();
    doc.body.removeChild(a);
  }

  function downloadSheet(clip) {
    var a = doc.createElement('a');
    a.href = '/api/video/clips/' + clip.id + '/sheet' + frameSuffix(clip);
    a.download = (clip.name || 'clip') + '-sheet.png';
    doc.body.appendChild(a);
    a.click();
    doc.body.removeChild(a);
  }

  /* ---------------------------------------------------------- 去背景 */

  function openRemoveBg(clip) {
    var body = ui().el('div', 'form-stack');

    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '两种去背景模式：①「按颜色抠（整图）」把接近某颜色的全部像素变透明；'
      + '②「连通抠图」从你**选中的点**出发，只移除与它**连通**的背景——画面里另一块同色但'
      + '被主体隔开的区域会保留。原图不会被改动，随时可「清除」回到原图。';
    body.appendChild(tip);

    // 种子点（连通模式用）：默认左上角 (0,0)，点击图片任意位置可改
    var seedX = 0, seedY = 0;
    if (clip.bg_mode === 'flood' && clip.bg_seed) {
      var sp = String(clip.bg_seed).split(',');
      if (sp.length === 2) { seedX = parseInt(sp[0], 10) || 0; seedY = parseInt(sp[1], 10) || 0; }
    }

    // ── 模式选择：按颜色整图 / 连通抠图 ──
    var modeSelect = ui().select([
      { label: '按颜色抠（整图）', value: 'colorkey' },
      { label: '连通抠图（只抠选点连通背景）', value: 'flood' }
    ], clip.bg_mode === 'flood' ? 'flood' : 'colorkey');
    body.appendChild(ui().fieldRow('模式', modeSelect,
      '连通模式：从选点出发，只移除与其连通的背景；不连通的同色区域保留'));

    // ── 种子点显示（仅连通模式可见）──
    var seedRow = ui().el('div', 'field-row');
    seedRow.appendChild(ui().el('label', 'field-label', '种子点'));
    var seedVal = ui().el('span', 'field-value', '(' + seedX + ', ' + seedY + ')');
    seedRow.appendChild(seedVal);
    var seedReset = ui().el('button', 'video-pick-chip', '重置为左上角');
    seedReset.type = 'button';
    seedReset.addEventListener('click', function () { seedX = 0; seedY = 0; seedVal.textContent = '(0, 0)'; });
    seedRow.appendChild(seedReset);
    body.appendChild(seedRow);

    // ── 双色抠图开关（按照两种背景色一起抠透明，如渐变/双色幕布）──
    var dualMode = !!(clip.bg_color2);
    var pickTarget = 1; // 取色目标：1=色1, 2=色2
    var dualRow = ui().checkbox('双色抠图（按两种背景色一起抠透明）', dualMode);
    body.appendChild(dualRow);

    // ── 图片取色器：显示首帧缩略图，点击即拾取该位置颜色 ──
    var pickerSection = ui().el('div', 'video-picker-section');
    var pickerLabel = ui().el('span', 'field-label', '从图片取色');
    pickerLabel.style.display = 'block';
    pickerLabel.style.marginBottom = '4px';
    var pickerHint = ui().el('span', 'field-hint', '点击图片上任意位置，自动拾取该点的颜色');
    pickerHint.style.display = 'block';
    pickerHint.style.marginBottom = '6px';
    pickerSection.appendChild(pickerLabel);
    pickerSection.appendChild(pickerHint);

    // 取色目标选择（双色模式下出现）：决定这一次点击取到的颜色填到 色1 还是 色2
    var targetRow = ui().el('div', 'video-pick-targets');
    targetRow.style.display = dualMode ? 'flex' : 'none';
    var chip1 = ui().el('button', 'video-pick-chip is-active', '取色到 色1');
    var chip2 = ui().el('button', 'video-pick-chip', '取色到 色2');
    chip1.type = chip2.type = 'button';
    chip1.addEventListener('click', function () {
      pickTarget = 1; chip1.classList.add('is-active'); chip2.classList.remove('is-active');
    });
    chip2.addEventListener('click', function () {
      pickTarget = 2; chip2.classList.add('is-active'); chip1.classList.remove('is-active');
    });
    targetRow.appendChild(chip1);
    targetRow.appendChild(chip2);
    pickerSection.appendChild(targetRow);

    var pickerWrap = ui().el('div', 'video-picker-wrap');
    var pickerImg = ui().el('img', 'video-picker-img');
    // 首帧 URL：用 variant 参数兼容透明变体（nobg 帧是 PNG）
    var v = variantOf(clip);
    var firstFrameUrl = '/api/video/clips/' + clip.id + '/frame/frame_0001' + (v === 'nobg' ? '.png' : '.jpg')
      + '?variant=' + (v || 'original');
    pickerImg.src = firstFrameUrl;
    pickerImg.alt = '点击取色';
    pickerImg.crossOrigin = 'anonymous';

    // 取色十字光标（跟随鼠标/触摸）
    var crosshair = ui().el('div', 'video-picker-crosshair');
    crosshair.style.display = 'none';
    pickerWrap.appendChild(pickerImg);
    pickerWrap.appendChild(crosshair);
    pickerSection.appendChild(pickerWrap);

    // 取色结果预览（小色块 + hex）
    var pickResult = ui().el('div', 'video-pick-result');
    pickResult.style.display = 'none';
    var pickSwatch = ui().el('span', 'video-pick-swatch');
    var pickHex = ui().el('span', 'video-pick-hex');
    pickResult.appendChild(pickSwatch);
    pickResult.appendChild(document.createTextNode('  '));
    pickResult.appendChild(pickHex);
    pickerSection.appendChild(pickResult);

    // 第二把钥匙色的取色结果预览（双色模式才显示）
    var pickResult2 = ui().el('div', 'video-pick-result');
    pickResult2.style.display = 'none';
    var pickSwatch2 = ui().el('span', 'video-pick-swatch');
    var pickHex2 = ui().el('span', 'video-pick-hex');
    pickResult2.appendChild(pickSwatch2);
    pickResult2.appendChild(document.createTextNode('  '));
    pickResult2.appendChild(pickHex2);
    pickerSection.appendChild(pickResult2);

    // 隐藏 canvas 用于读取像素
    var pickCanvas = null;
    function ensureCanvas() {
      if (!pickCanvas) {
        pickCanvas = document.createElement('canvas');
        pickCanvas.width = 1;
        pickCanvas.height = 1;
      }
      return pickCanvas;
    }

    function pickColorAt(img, x, y) {
      var cvs = ensureCanvas();
      cvs.width = img.naturalWidth || img.width;
      cvs.height = img.naturalHeight || img.height;
      var ctx = cvs.getContext('2d');
      ctx.drawImage(img, 0, 0, cvs.width, cvs.height);
      var px = Math.max(0, Math.min(Math.round(x), cvs.width - 1));
      var py = Math.max(0, Math.min(Math.round(y), cvs.height - 1));
      var data = ctx.getImageData(px, py, 1, 1).data;
      // RGB → #RRGGBB
      var hex = '#' + [data[0], data[1], data[2]].map(function (c) {
        var h = c.toString(16);
        return h.length === 1 ? '0' + h : h;
      }).join('');
      return { hex: hex, r: data[0], g: data[1], b: data[2] };
    }

    function applyPickedColor(hex, target) {
      if (target === 2 && color2Input) {
        color2Input.value = hex;
        hex2Input.value = hex;
        pickSwatch2.style.background = hex;
        pickHex2.textContent = hex.toUpperCase();
        pickResult2.style.display = '';
      } else {
        colorInput.value = hex;
        hexInput.value = hex;
        pickSwatch.style.background = hex;
        pickHex.textContent = hex.toUpperCase();
        pickResult.style.display = '';
      }
    }

    pickerImg.addEventListener('load', function () {
      pickerWrap.classList.add('is-ready');
    });
    pickerImg.addEventListener('error', function () {
      pickerWrap.classList.add('is-error');
    });

    function handlePick(e) {
      var rect = pickerImg.getBoundingClientRect();
      var scaleX = (pickerImg.naturalWidth || pickerImg.width) / rect.width;
      var scaleY = (pickerImg.naturalHeight || pickerImg.height) / rect.height;
      var x = (e.clientX - rect.left) * scaleX;
      var y = (e.clientY - rect.top) * scaleY;

      // 显示十字光标
      crosshair.style.left = (e.clientX - rect.left) + 'px';
      crosshair.style.top = (e.clientY - rect.top) + 'px';
      crosshair.style.display = '';

      var c = pickColorAt(pickerImg, x, y);
      applyPickedColor(c.hex, pickTarget);
      // 连通模式：点击图片即把该点设为种子点（display 坐标 → source 帧像素坐标）
      if (modeSelect.value === 'flood') {
        seedX = Math.max(0, Math.round(x));
        seedY = Math.max(0, Math.round(y));
        seedVal.textContent = '(' + seedX + ', ' + seedY + ')';
      }
    }

    pickerImg.addEventListener('click', handlePick);
    pickerImg.addEventListener('mousemove', function (e) {
      if (e.buttons === 1) handlePick(e); // 拖动时也持续取色
    });

    body.appendChild(pickerSection);

    // ── 取色器好点选，hex 输入框好精确填写/复制——两者双向同步 ──
    var colorRow = ui().el('div', 'video-color-row');
    var colorInput = ui().el('input', 'video-color-input');
    colorInput.type = 'color';
    colorInput.value = clip.bg_color || '#00FF00';
    var hexInput = ui().el('input', 'input');
    hexInput.value = clip.bg_color || '#00FF00';
    hexInput.placeholder = '#00FF00';

    function syncHex() { hexInput.value = colorInput.value; }
    function syncColor() {
      var v = hexInput.value.trim().replace(/^#?/, '#');
      if (/^#[0-9a-fA-F]{6}$/.test(v)) colorInput.value = v;
    }
    colorInput.addEventListener('input', syncHex);
    hexInput.addEventListener('input', syncColor);
    colorRow.appendChild(colorInput);
    colorRow.appendChild(hexInput);
    var color1Field = ui().fieldRow('背景颜色 ①', colorRow, '系统取色器或手动输入 #RRGGBB');
    body.appendChild(color1Field);

    // 第二把钥匙色（双色模式才显示）
    var colorRow2 = ui().el('div', 'video-color-row');
    var color2Input = ui().el('input', 'video-color-input');
    color2Input.type = 'color';
    color2Input.value = clip.bg_color2 || '#0000FF';
    var hex2Input = ui().el('input', 'input');
    hex2Input.value = clip.bg_color2 || '#0000FF';
    hex2Input.placeholder = '#0000FF';

    function syncHex2() { hex2Input.value = color2Input.value; }
    function syncColor2() {
      var v = hex2Input.value.trim().replace(/^#?/, '#');
      if (/^#[0-9a-fA-F]{6}$/.test(v)) color2Input.value = v;
    }
    color2Input.addEventListener('input', syncHex2);
    hex2Input.addEventListener('input', syncColor2);
    colorRow2.appendChild(color2Input);
    colorRow2.appendChild(hex2Input);
    var color2Field = ui().fieldRow('背景颜色 ②', colorRow2, '第二把要抠掉的颜色（如另一种背景色）');
    color2Field.style.display = dualMode ? '' : 'none';
    body.appendChild(color2Field);

    // 切换双色：显隐「取色目标」与「色②」行
    dualRow.checkbox.addEventListener('change', function () {
      dualMode = dualRow.checkbox.checked;
      targetRow.style.display = dualMode ? 'flex' : 'none';
      color2Field.style.display = dualMode ? '' : 'none';
      if (!dualMode) {
        chip1.classList.add('is-active'); chip2.classList.remove('is-active');
        pickTarget = 1;
      }
    });

    // 模式切换：连通模式强制单色 + 显示种子点；整图模式显示颜色行与双色开关
    function applyModeUI() {
      var flood = modeSelect.value === 'flood';
      seedRow.style.display = flood ? '' : 'none';
      if (flood) {
        dualMode = false;
        dualRow.checkbox.checked = false;
        color1Field.style.display = 'none';
        targetRow.style.display = 'none';
        color2Field.style.display = 'none';
      } else {
        color1Field.style.display = '';
        targetRow.style.display = dualMode ? 'flex' : 'none';
        color2Field.style.display = dualMode ? '' : 'none';
      }
    }
    modeSelect.addEventListener('change', applyModeUI);
    applyModeUI();

    var simInput = ui().el('input', 'input');
    simInput.type = 'number';
    simInput.min = '0.01';
    simInput.max = '1';
    simInput.step = '0.01';
    simInput.value = String(clip.bg_similarity != null ? clip.bg_similarity : 0.3);
    body.appendChild(ui().fieldRow('容差', simInput, '0.01–1，越大抠得越多；抠不干净就调大'));

    var blendInput = ui().el('input', 'input');
    blendInput.type = 'number';
    blendInput.min = '0';
    blendInput.max = '1';
    blendInput.step = '0.01';
    blendInput.value = String(clip.bg_blend != null ? clip.bg_blend : 0.05);
    body.appendChild(ui().fieldRow('边缘羽化', blendInput, '0–1，给边缘一点过渡，减少锯齿'));

    var actions = [
      { label: '取消', variant: 'ghost', onClick: function () { return true; } },
      {
        label: '开始去背景',
        variant: 'primary',
        onClick: function () {
          var payload = {
            color: hexInput.value.trim() || '#00FF00',
            similarity: parseFloat(simInput.value) || undefined,
            blend: blendInput.value === '' ? undefined : parseFloat(blendInput.value)
          };
          var flood = modeSelect.value === 'flood';
          if (flood) {
            payload.mode = 'flood';
            payload.seed = seedX + ',' + seedY;
          } else if (dualMode && hex2Input.value.trim()) {
            payload.color2 = hex2Input.value.trim();
            payload.similarity2 = payload.similarity;
            payload.blend2 = payload.blend;
          }
          api().post('/api/video/clips/' + clip.id + '/remove-bg', payload
          ).then(function (r) {
            ui().toastSuccess('已生成 ' + (r.frames || 0) + ' 张透明帧'
              + (flood ? '（连通抠图，种子 ' + (r.seed || '') + '）' : (dualMode ? '（双色）' : '')));
            // 抠完直接切到透明版，省得用户还要再点一次切换
            state.variant[clip.id] = 'nobg';
            loadClips();
          }, function (err) { ui().toastError(ui().errorText(err, '去背景失败')); });
          return true;
        }
      }
    ];

    if (clip.has_nobg) {
      actions.push({
        label: '清除去背景',
        variant: 'ghost',
        onClick: function () {
          api().post('/api/video/clips/' + clip.id + '/clear-bg', {}).then(function () {
            ui().toastSuccess('已清除，回到原图');
            state.variant[clip.id] = 'original';
            loadClips();
          }, function (err) { ui().toastError(ui().errorText(err, '清除失败')); });
          return true;
        }
      });
    }

    ui().modal({ title: '去背景 · ' + (clip.name || ''), body: body, actions: actions });
  }

  /* ---------------------------------------------------------- 序列帧拼图 */

  function openSheet(clip) {
    var v = variantOf(clip);
    var body = ui().el('div', 'form-stack');

    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '把序列帧按网格拼成一张大图（sprite sheet），方便做素材总览或导出。'
      + '当前拼的是「' + (v === 'nobg' ? '透明' : '原图') + '」这套帧'
      + '（在卡片上切换「原图 / 透明」可换）。';
    body.appendChild(tip);

    var colsInput = ui().el('input', 'input');
    colsInput.type = 'number';
    colsInput.min = '1';
    colsInput.max = '50';
    colsInput.value = String(clip.sheet_cols || 5);
    body.appendChild(ui().fieldRow
      ? ui().fieldRow('每行帧数（列数）', colsInput, '行数按帧数自动算')
      : colsInput);

    var padInput = ui().el('input', 'input');
    padInput.type = 'number';
    padInput.min = '0';
    padInput.max = '50';
    padInput.value = '0';
    body.appendChild(ui().fieldRow
      ? ui().fieldRow('帧间留白 (px)', padInput, '默认 0，紧密排列')
      : padInput);

    ui().modal({
      title: '生成序列帧拼图 · ' + (clip.name || ''),
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '生成拼图',
          variant: 'primary',
          onClick: function () {
            api().post('/api/video/clips/' + clip.id + '/sheet', {
              cols: parseInt(colsInput.value, 10) || 5,
              padding: parseInt(padInput.value, 10) || 0,
              variant: v
            }).then(function (r) {
              ui().toastSuccess('已拼 ' + (r.cols || '?') + '×' + (r.rows || '?')
                + ' 网格，共 ' + (r.frames || 0) + ' 帧');
              loadClips();
            }, function (err) { ui().toastError(ui().errorText(err, '拼图失败')); });
            return true;
          }
        }
      ]
    });
  }

  function doDelete(clip) {
    ui().confirm({
      title: '删除视频',
      message: '将删除「' + (clip.name || '') + '」及其全部序列帧与 GIF。'
        + '（若视频是从本机路径导入的，原始文件不会被删除；上传的会一并清除。）',
      confirmLabel: '删除',
      danger: true
    }).then(function (yes) {
      if (!yes) return;
      api().del('/api/video/clips/' + clip.id).then(function () {
        ui().toastSuccess('已删除');
        loadClips();
      }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
    });
  }

  /* ---------------------------------------------------------- 导出 */

  AIBAR.video = {
    init: init,
    onEnter: onEnter,
    refresh: function () { loadClips(); },
    openPlayer: openPlayer,
    openRemoveBg: openRemoveBg,
    openSheet: openSheet,
    variantOf: variantOf
  };

  // 双环境导出：浏览器挂 window.AIBAR，Node 下供 tests/js 断言模块契约
  if (typeof module !== 'undefined' && module.exports) module.exports = AIBAR.video;
})(typeof window !== 'undefined' ? window : globalThis);
