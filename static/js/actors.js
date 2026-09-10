/* ============================================================
   AIBAR · 演员库（M13）前端
   - 演员是跨漫画共享的「人物一致性基准源」：一次定妆，多部漫画复用；
   - 三种来源新建：手填 / 从图库收人 / 从漫画角色提升为全局演员；
   - 查看详情、编辑、删除、关联/取消关联漫画角色、重新生成定妆图。
   全部走 /api/comic/actors（以及 /api/comic/projects/<pid>/characters/from-actor），
   统一用 AIBAR.api 解包，错误用 AIBAR.ui 提示。IIFE 结构与 comic.js 保持一致。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  var LIST_LIMIT = 200; // 演员库是花名册不是图库，一次拉全量足够

  var state = {
    keyword: '',
    poll: {} // actorId -> 是否正在轮询生成结果
  };

  function api() { return AIBAR.api; }
  function ui() { return AIBAR.ui; }
  function host() { return doc.getElementById('actors-root'); }

  /* ---------------------------------------------------------- 小工具（与 comic.js 同款） */

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
      b.disabled = true;
      b.style.opacity = '0.55';
      b.style.cursor = 'default';
    }
    return b;
  }

  function metaBadge(text) { return ui().el('span', 'badge', text); }

  function sourceBadge(type) {
    var map = {
      manual: ['badge', '手填'],
      gallery: ['badge', '图库收人'],
      character: ['badge badge-brand', '角色提升']
    };
    var m = map[type] || ['badge', type || '—'];
    return ui().el('span', m[0], m[1]);
  }

  function statusBadge(status) {
    var map = {
      ready: ['badge badge-success', '已就绪'],
      generating: ['badge badge-warning', '出图中'],
      failed: ['badge badge-error', '生成失败']
    };
    var m = map[status] || ['badge', status || '—'];
    return ui().el('span', m[0], m[1]);
  }

  function clampInt(v, fallback, lo, hi) {
    var n = parseInt(v, 10);
    if (isNaN(n)) n = fallback;
    return Math.max(lo, Math.min(hi, n));
  }

  /* ---------------------------------------------------------- 渲染入口 */

  function init() { /* 演员库无需预加载 */ }

  function onEnter(tab) {
    if (tab !== 'actors') return;
    render();
  }

  function render() {
    var h = host();
    if (!h) return;
    loadActors();
  }

  /* ---------------------------------------------------------- 列表 */

  function loadActors() {
    var h = host();
    if (!h) return;
    h.innerHTML = '';

    var toolbar = ui().el('div', 'page-toolbar');
    var search = ui().el('div', 'search');
    search.appendChild(icon('search', 15));
    var input = ui().el('input', 'input');
    input.type = 'search';
    input.placeholder = '搜索演员名或别名';
    input.setAttribute('aria-label', '搜索演员');
    input.value = state.keyword;
    input.addEventListener('input', function () {
      state.keyword = input.value.trim();
      loadActorGrid(grid);
    });
    search.appendChild(input);
    toolbar.appendChild(search);
    toolbar.appendChild(ui().el('span', 'grow'));
    toolbar.appendChild(btn('primary', 'plus', '新建演员', openCreateActor));
    h.appendChild(toolbar);

    // --wide：演员卡文字比公共卡片更长，用更宽的列显示更多内容
    var grid = ui().el('div', 'card-grid card-grid--wide');
    h.appendChild(grid);
    loadActorGrid(grid);
  }

  function loadActorGrid(grid) {
    grid.innerHTML = '';
    grid.appendChild(ui().loadingInline('加载演员库…'));
    var qs = '?limit=' + LIST_LIMIT + '&offset=0';
    if (state.keyword) qs += '&keyword=' + encodeURIComponent(state.keyword);
    api().get('/api/comic/actors' + qs).then(function (data) {
      renderActorList(grid, (data && data.items) || []);
    }, function (err) {
      grid.innerHTML = '';
      grid.appendChild(ui().errorState(ui().errorText(err, '加载演员库失败'), function () { loadActorGrid(grid); }));
    });
  }

  function renderActorList(grid, items) {
    grid.innerHTML = '';
    if (!items.length) {
      grid.appendChild(ui().emptyState({
        icon: 'users',
        title: '演员库还是空的',
        desc: '把生成的人物收为演员：手填定妆、从图库收人，或直接把某部漫画里的角色提升为全局演员。后续漫画以演员为基准，角色长相一致。',
        actions: [{ label: '新建演员', variant: 'primary', onClick: openCreateActor }]
      }));
      return;
    }
    items.forEach(function (a) { grid.appendChild(renderActorCard(a)); });
    // 续跑上次会话遗留的「生成中」任务，避免页面刷新后永远卡在出图中
    items.forEach(function (a) { if (a.is_generating) startPollingActor(a.id); });
  }

  function renderActorCard(a) {
    var card = ui().el('article', 'entry-card');

    if (a.image_url) {
      // 左图右文：缩略图占固定画框完整显示，右侧放文字与操作
      // --wide 是演员库专用放大版，避免改动公共类波及 comic.js 的分镜卡
      card.classList.add('entry-card--split', 'entry-card--split-wide');
      var sample = ui().el('img', 'entry-thumb entry-thumb--wide');
      sample.src = a.image_url;
      sample.alt = (a.name || '演员') + ' 定妆图';
      sample.loading = 'lazy';
      card.appendChild(sample);
    }
    var body = ui().el('div', 'entry-body');

    var head = ui().el('div', 'entry-head');
    head.appendChild(ui().el('h4', 'entry-title', a.name || '未命名演员'));
    head.appendChild(sourceBadge(a.source_type));
    head.appendChild(statusBadge(a.status));
    if (a.link_count) head.appendChild(metaBadge('出演 ' + a.link_count));
    body.appendChild(head);

    var parts = [];
    if (a.aliases) parts.push('别名：' + a.aliases);
    if (a.appearance) parts.push('外貌：' + a.appearance);
    if (a.outfit) parts.push('服装：' + a.outfit);
    if (a.palette) parts.push('配色：' + a.palette);
    if (a.negative) parts.push('排除：' + a.negative);
    if (a.notes) parts.push('备注：' + a.notes);
    if (!parts.length) parts.push('（暂无定妆描述，锚点只含角色名）');
    parts.forEach(function (t) { body.appendChild(ui().el('p', 'entry-text clamp-2', t)); });
    if (a.anchor) body.appendChild(ui().el('p', 'entry-text clamp-1 text-tertiary', '锚点：' + a.anchor));

    if (a.is_generating) {
      var gen = ui().el('p', 'entry-text');
      gen.style.color = 'var(--color-state-warning)';
      gen.appendChild(icon('refresh', 13));
      gen.appendChild(ui().el('span', null, ' 定妆图生成中…'));
      body.appendChild(gen);
    }
    if (a.status === 'failed' && a.error_message) {
      var err = ui().el('p', 'entry-text');
      err.style.color = 'var(--color-state-error)';
      err.textContent = '失败：' + a.error_message;
      body.appendChild(err);
    }

    var actions = ui().el('div', 'entry-actions');
    actions.appendChild(btn('primary', 'eye', '查看', function () { openActorDetail(a); }));
    actions.appendChild(btn('ghost', 'link', '关联角色', function () { openLinkCharacter(a); }));
    actions.appendChild(btn('secondary', 'refresh', '重新生成', function () { openRegenerate(a); }));
    actions.appendChild(btn('danger', 'trash', '删除', function () { deleteActor(a); }));
    body.appendChild(actions);
    card.appendChild(body);
    return card;
  }

  /* ---------------------------------------------------------- 新建（三来源） */

  function openCreateActor() {
    var body = ui().el('div', 'form-stack');

    var srcWrap = ui().el('label', 'field');
    srcWrap.appendChild(ui().el('span', 'field-label', '演员来源'));
    var srcSel = ui().el('select', 'select');
    [
      ['manual', '手填定妆信息'],
      ['gallery', '从图库收人（已有出图的人物）'],
      ['character', '从漫画角色提升为全局演员']
    ].forEach(function (o) {
      var op = ui().el('option', null, o[1]);
      op.value = o[0];
      srcSel.appendChild(op);
    });
    srcWrap.appendChild(srcSel);
    body.appendChild(srcWrap);

    // 名字：手填 / 图库必填；角色提升时自动带出但允许改
    var nameWrap = ui().el('label', 'field');
    nameWrap.appendChild(ui().el('span', 'field-label', '演员名'));
    var nameInput = ui().el('input', 'input');
    nameInput.type = 'text';
    nameInput.placeholder = '例如 阿岚';
    nameWrap.appendChild(nameInput);
    body.appendChild(nameWrap);

    function row(label, control) {
      var wrap = ui().el('label', 'field');
      wrap.appendChild(ui().el('span', 'field-label', label));
      wrap.appendChild(control);
      return wrap;
    }

    // 手填字段
    var manualBox = ui().el('div');
    var fAliases = ui().el('input', 'input'); fAliases.type = 'text';
    var fAppearance = ui().el('textarea', 'textarea'); fAppearance.rows = 2;
    var fOutfit = ui().el('textarea', 'textarea'); fOutfit.rows = 2;
    var fPalette = ui().el('input', 'input'); fPalette.type = 'text';
    var fNegative = ui().el('textarea', 'textarea'); fNegative.rows = 2;
    var fNotes = ui().el('textarea', 'textarea'); fNotes.rows = 2;
    var fSeed = ui().el('input', 'input'); fSeed.type = 'number'; fSeed.value = '0';
    manualBox.appendChild(row('别名（逗号分隔）', fAliases));
    manualBox.appendChild(row('外貌（发色/发型/五官/体型）', fAppearance));
    manualBox.appendChild(row('服装（常穿衣物/配饰）', fOutfit));
    manualBox.appendChild(row('配色（主色调）', fPalette));
    manualBox.appendChild(row('排除项（如「不要改变发色」）', fNegative));
    manualBox.appendChild(row('备注', fNotes));
    manualBox.appendChild(row('种子偏移（0-9999）', fSeed));
    body.appendChild(manualBox);

    // 图库收人
    var galleryBox = ui().el('div');
    galleryBox.hidden = true;
    var grid = ui().el('div', 'image-grid');
    galleryBox.appendChild(grid);
    var gNotes = ui().el('textarea', 'textarea'); gNotes.rows = 2;
    galleryBox.appendChild(row('备注（可选）', gNotes));
    body.appendChild(galleryBox);
    var selectedImageId = null;

    // 角色提升
    var charBox = ui().el('div');
    charBox.hidden = true;
    var projSel = ui().el('select', 'select');
    var charSel = ui().el('select', 'select');
    var roleNote = ui().el('input', 'input'); roleNote.type = 'text'; roleNote.placeholder = '例如 男一号';
    charBox.appendChild(row('来源漫画', projSel));
    charBox.appendChild(row('角色', charSel));
    charBox.appendChild(row('出演备注（可选）', roleNote));
    body.appendChild(charBox);

    function showSource(src) {
      nameWrap.style.display = (src === 'character') ? 'none' : '';
      manualBox.hidden = src !== 'manual';
      galleryBox.hidden = src !== 'gallery';
      charBox.hidden = src !== 'character';
    }
    srcSel.addEventListener('change', function () { showSource(srcSel.value); });
    showSource('manual');

    function loadGallery() {
      grid.innerHTML = '';
      grid.appendChild(ui().loadingInline('加载图库…'));
      api().get('/api/gallery?page=1&page_size=60').then(function (d) {
        grid.innerHTML = '';
        var items = (d && d.items) || [];
        if (!items.length) {
          grid.appendChild(ui().emptyState({
            icon: 'image', title: '图库为空',
            desc: '先去生成或导入一些图片，再回来收为演员。'
          }));
          return;
        }
        items.forEach(function (item) {
          var cell = ui().el('div', 'image-cell' +
            (selectedImageId && String(item.id) === String(selectedImageId) ? ' selected' : ''));
          cell.setAttribute('data-id', String(item.id));
          var im = ui().el('img', 'thumb');
          im.src = item.url || '';
          im.alt = item.prompt || '图库图片';
          im.loading = 'lazy';
          cell.appendChild(im);
          cell.addEventListener('click', function () {
            selectedImageId = item.id;
            Array.prototype.forEach.call(grid.children, function (c) {
              c.classList.toggle('selected', c.getAttribute('data-id') === String(item.id));
            });
          });
          grid.appendChild(cell);
        });
      }, function () {
        grid.innerHTML = '';
        grid.appendChild(ui().errorState(ui().errorText(null, '加载图库失败'), function () { loadGallery(); }));
      });
    }

    var selectedCharacterId = null;
    function loadProjectsInto(sel, onCharLoad) {
      sel.innerHTML = '';
      var none = ui().el('option', null, '选择漫画'); none.value = '';
      sel.appendChild(none);
      api().get('/api/comic/projects').then(function (d) {
        (d && d.items || []).forEach(function (p) {
          var op = ui().el('option', null, p.name || ('漫画 ' + p.id));
          op.value = String(p.id);
          sel.appendChild(op);
        });
      }, function () { ui().toastError('加载漫画失败'); });
      sel.addEventListener('change', function () {
        selectedCharacterId = null;
        if (onCharLoad) onCharLoad(sel.value);
      });
    }

    function loadCharactersInto(projId, sel) {
      sel.innerHTML = '';
      if (!projId) return;
      var none = ui().el('option', null, '选择角色'); none.value = '';
      sel.appendChild(none);
      api().get('/api/comic/projects/' + projId + '/characters').then(function (d) {
        (d && d.items || []).forEach(function (c) {
          var op = ui().el('option', null, c.name || ('角色 ' + c.id));
          op.value = String(c.id);
          sel.appendChild(op);
        });
      }, function () { ui().toastError('加载角色失败'); });
    }

    // 提前加载，切换来源时即可显示
    loadGallery();
    loadProjectsInto(projSel, function (pid) { loadCharactersInto(pid, charSel); });

    ui().modal({
      title: '新建演员',
      desc: '演员是跨漫画共享的定妆基准：一次定妆，多部漫画复用；改演员即批量校正所有关联角色。',
      body: body,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '创建',
          variant: 'primary',
          onClick: function () {
            var src = srcSel.value;
            var name = String(nameInput.value || '').trim();
            var payload = {};

            if (src === 'manual') {
              if (!name) { ui().toastError('请填写演员名'); return false; }
              payload = {
                name: name,
                aliases: String(fAliases.value || '').trim(),
                appearance: String(fAppearance.value || '').trim(),
                outfit: String(fOutfit.value || '').trim(),
                palette: String(fPalette.value || '').trim(),
                negative: String(fNegative.value || '').trim(),
                notes: String(fNotes.value || '').trim(),
                seed_offset: clampInt(fSeed.value, 0, 0, 9999)
              };
            } else if (src === 'gallery') {
              if (!name) { ui().toastError('请填写演员名'); return false; }
              if (!selectedImageId) { ui().toastError('请选择一张图库图片'); return false; }
              payload = { name: name, image_id: selectedImageId, notes: String(gNotes.value || '').trim() };
            } else {
              var cid = charSel.value;
              if (!cid) { ui().toastError('请选择要提升的角色'); return false; }
              payload = { character_id: Number(cid) };
              if (name) payload.name = name;
              var rn = String(roleNote.value || '').trim();
              if (rn) payload.role_note = rn;
            }

            api().post('/api/comic/actors', payload).then(function (a) {
              ui().toastSuccess('演员「' + ((a && a.name) || name) + '」已创建');
              loadActors();
            }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
            return true;
          }
        }
      ]
    });
  }

  /* ---------------------------------------------------------- 编辑 */

  var _ACTOR_FIELDS = [
    { key: 'name', label: '演员名', required: true },
    { key: 'aliases', label: '别名（逗号分隔）' },
    { key: 'appearance', label: '外貌（发色/发型/五官/体型）', type: 'textarea', rows: 2 },
    { key: 'outfit', label: '服装（常穿衣物/配饰）', type: 'textarea', rows: 2 },
    { key: 'palette', label: '配色（主色调）' },
    { key: 'negative', label: '排除项（如「不要改变发色」）', type: 'textarea', rows: 2 },
    { key: 'notes', label: '备注', type: 'textarea', rows: 2 },
    { key: 'seed_offset', label: '种子偏移（0-9999）', type: 'number' },
    { key: 'apply_to_characters', label: '同步给定妆关联角色（以演员为基准）', type: 'checkbox' }
  ];

  function openEditActor(a) {
    var prepend = a.image_url ? _buildImagePrepend(a) : null;
    var values = {};
    for (var k in a) {
      if (!Object.prototype.hasOwnProperty.call(a, k)) continue;
      values[k] = a[k];
    }
    values.apply_to_characters = true;
    openForm({
      title: '编辑演员 · ' + (a.name || ''),
      desc: '修改定妆信息会同步给全部关联角色（以演员为基准保持一致性）。取消勾选「同步关联角色」可只改演员本身。',
      fields: _ACTOR_FIELDS,
      values: values,
      prepend: prepend,
      onSubmit: function (vals) {
        api().patch('/api/comic/actors/' + a.id, vals).then(function () {
          ui().toastSuccess('演员已更新');
          loadActors();
        }, function (err) { ui().toastError(ui().errorText(err, '保存失败')); });
      }
    });
  }

  function _buildImagePrepend(a) {
    var box = ui().el('div', 'character-sample');
    var img = ui().el('img', 'sample-img');
    img.src = a.image_url || '';
    img.alt = (a.name || '演员') + ' 定妆图';
    box.appendChild(img);
    box.appendChild(ui().el('p', 'entry-text clamp-3 text-tertiary', '定妆图（来自图库或最近一次生成）'));
    return box;
  }

  /* ---------------------------------------------------------- 详情 + 关联列表 */

  function openActorDetail(a) {
    var body = ui().el('div');

    var inner = body; // 无定妆图时正文直接铺满；有图时下文切成右栏
    if (a.image_url) {
      // 左图右文：定妆图占左侧固定画框完整显示，右侧放定妆信息与关联角色
      body.classList.add('entry-split');
      var img = ui().el('img', 'entry-thumb-lg');
      img.src = a.image_url;
      img.alt = (a.name || '演员') + ' 定妆图';
      body.appendChild(img);
      inner = ui().el('div', 'entry-body');
      body.appendChild(inner);
    }

    var head = ui().el('div', 'result-actions');
    head.appendChild(ui().el('h4', null, a.name || '未命名演员'));
    head.appendChild(sourceBadge(a.source_type));
    head.appendChild(statusBadge(a.status));
    if (a.link_count) head.appendChild(metaBadge('出演 ' + a.link_count));
    inner.appendChild(head);

    var parts = [];
    if (a.aliases) parts.push('别名：' + a.aliases);
    if (a.appearance) parts.push('外貌：' + a.appearance);
    if (a.outfit) parts.push('服装：' + a.outfit);
    if (a.palette) parts.push('配色：' + a.palette);
    if (a.negative) parts.push('排除：' + a.negative);
    if (a.notes) parts.push('备注：' + a.notes);
    parts.forEach(function (t) { inner.appendChild(ui().el('p', 'entry-text clamp-3', t)); });
    if (a.anchor) {
      var an = ui().el('p', 'entry-text text-tertiary');
      an.style.marginTop = '6px';
      an.textContent = '锚点：' + a.anchor;
      inner.appendChild(an);
    }

    var meta = ui().el('div', 'result-actions');
    meta.appendChild(metaBadge('种子：' + (a.seed != null ? a.seed : '—')));
    if (a.base_seed) meta.appendChild(metaBadge('基准种子：' + a.base_seed));
    if (a.use_count != null) meta.appendChild(metaBadge('引用次数：' + a.use_count));
    inner.appendChild(meta);

    inner.appendChild(ui().el('p', 'text-section', '关联角色'));
    var linksHost = ui().el('div', 'entry-list');
    renderLinks(linksHost, a);
    inner.appendChild(linksHost);

    ui().modal({
      title: '演员详情 · ' + (a.name || ''),
      desc: '演员是跨漫画共享的定妆基准；下方为该演员出演的漫画角色。',
      body: body,
      size: 'lg',
      actions: [
        { label: '编辑', variant: 'ghost', onClick: function () { openEditActor(a); return true; } },
        { label: '重新生成', variant: 'secondary', onClick: function () { openRegenerate(a); return true; } },
        { label: '删除', variant: 'danger', onClick: function () { deleteActor(a); return true; } },
        { label: '关闭', variant: 'primary', onClick: function () { return true; } }
      ]
    });
  }

  function renderLinks(hostNode, a) {
    hostNode.innerHTML = '';
    var links = a.characters || [];
    if (!links.length) {
      hostNode.appendChild(ui().el('p', 'entry-text text-tertiary',
        '尚未关联任何漫画角色。点「关联角色」把该演员绑定到某部漫画的角色上。'));
      return;
    }
    links.forEach(function (l) {
      var rowEl = ui().el('div', 'control-row');
      rowEl.style.margin = '0 0 8px';
      var info = ui().el('div');
      info.appendChild(ui().el('div', 'entry-text',
        (l.character_name || '角色') + (l.project_name ? (' · 《' + l.project_name + '》') : '')));
      if (l.role_note) {
        info.appendChild(ui().el('div', 'entry-text text-tertiary clamp-1', '出演：' + l.role_note));
      }
      rowEl.appendChild(info);
      rowEl.appendChild(ui().el('span', 'grow'));
      rowEl.appendChild(btn('ghost', 'close', '取消关联', function () { unlinkCharacter(a, l.character_id); }));
      hostNode.appendChild(rowEl);
    });
  }

  function unlinkCharacter(a, characterId) {
    api().del('/api/comic/actors/' + a.id + '/links/' + characterId).then(function () {
      ui().toastSuccess('已取消关联');
      loadActors();
    }, function (err) { ui().toastError(ui().errorText(err, '取消关联失败')); });
  }

  /* ---------------------------------------------------------- 关联角色 */

  function openLinkCharacter(a) {
    var body = ui().el('div', 'form-stack');
    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '把演员「' + (a.name || '') + '」绑定到一个漫画角色：定妆信息会同步给该角色卡，保证出场长相一致。';
    body.appendChild(tip);

    function row(label, control) {
      var wrap = ui().el('label', 'field');
      wrap.appendChild(ui().el('span', 'field-label', label));
      wrap.appendChild(control);
      return wrap;
    }

    var projSel = ui().el('select', 'select');
    var charSel = ui().el('select', 'select');
    var roleInput = ui().el('input', 'input');
    roleInput.type = 'text';
    roleInput.placeholder = '例如 男一号';
    body.appendChild(row('来源漫画', projSel));
    body.appendChild(row('角色', charSel));
    body.appendChild(row('出演备注（可选）', roleInput));

    var applyWrap = ui().el('label', 'field');
    var applyChk = ui().el('input');
    applyChk.type = 'checkbox';
    applyChk.checked = true;
    applyChk.style.cssText = 'width:16px;height:16px;margin-right:8px';
    applyWrap.appendChild(applyChk);
    applyWrap.appendChild(ui().el('span', null, '把演员定妆信息同步给角色（以演员为基准）'));
    body.appendChild(applyWrap);

    function loadCharactersInto(projId) {
      charSel.innerHTML = '';
      if (!projId) return;
      var none = ui().el('option', null, '选择角色'); none.value = '';
      charSel.appendChild(none);
      api().get('/api/comic/projects/' + projId + '/characters').then(function (d) {
        (d && d.items || []).forEach(function (c) {
          var op = ui().el('option', null, c.name || ('角色 ' + c.id));
          op.value = String(c.id);
          charSel.appendChild(op);
        });
      }, function () { ui().toastError('加载角色失败'); });
    }

    projSel.innerHTML = '';
    var none = ui().el('option', null, '选择漫画'); none.value = '';
    projSel.appendChild(none);
    api().get('/api/comic/projects').then(function (d) {
      (d && d.items || []).forEach(function (p) {
        var op = ui().el('option', null, p.name || ('漫画 ' + p.id));
        op.value = String(p.id);
        projSel.appendChild(op);
      });
    }, function () { ui().toastError('加载漫画失败'); });
    projSel.addEventListener('change', function () { loadCharactersInto(projSel.value); });

    ui().modal({
      title: '关联角色 · ' + (a.name || ''),
      desc: '一个角色只能绑一个演员；若已绑别的演员，这里会改绑为当前演员。',
      body: body,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '关联',
          variant: 'primary',
          onClick: function () {
            var cid = charSel.value;
            if (!cid) { ui().toastError('请选择角色'); return false; }
            var payload = { character_id: Number(cid), apply: applyChk.checked };
            var rn = String(roleInput.value || '').trim();
            if (rn) payload.role_note = rn;
            api().post('/api/comic/actors/' + a.id + '/links', payload).then(function () {
              ui().toastSuccess('已关联角色');
              loadActors();
            }, function (err) { ui().toastError(ui().errorText(err, '关联失败')); });
            return true;
          }
        }
      ]
    });
  }

  /* ---------------------------------------------------------- 重新生成定妆图 */

  function openRegenerate(a) {
    var body = ui().el('div', 'form-stack');
    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '重新生成定妆图。默认沿用确定性种子——同一张脸、只提升画质/构图；勾选「换一张脸」才会重新抽种子。';
    body.appendChild(tip);

    var promptWrap = ui().el('label', 'field');
    promptWrap.appendChild(ui().el('span', 'field-label', '追加描述（可选，如「换成雪地背景」）'));
    var promptInput = ui().el('textarea', 'textarea');
    promptInput.rows = 2;
    promptWrap.appendChild(promptInput);
    body.appendChild(promptWrap);

    var randWrap = ui().el('label', 'field');
    var randChk = ui().el('input');
    randChk.type = 'checkbox';
    randChk.style.cssText = 'width:16px;height:16px;margin-right:8px';
    randWrap.appendChild(randChk);
    randWrap.appendChild(ui().el('span', null, '换一张脸（重新抽种子）'));
    body.appendChild(randWrap);

    ui().modal({
      title: '重新生成定妆图 · ' + (a.name || ''),
      desc: '后台出图，完成后会自动刷新。',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '开始生成',
          variant: 'primary',
          onClick: function () {
            var payload = {};
            var p = String(promptInput.value || '').trim();
            if (p) payload.prompt = p;
            if (randChk.checked) payload.randomize = true;
            api().post('/api/comic/actors/' + a.id + '/regenerate', payload).then(function () {
              ui().toastSuccess('已入队重新生成，定妆图出图中…');
              startPollingActor(a.id);
              loadActors(); // 立即刷新出「出图中」状态
            }, function (err) { ui().toastError(ui().errorText(err, '重新生成失败')); });
            return true;
          }
        }
      ]
    });
  }

  /** 轮询单个演员的生成结果；只在状态离开 generating 时才刷新列表与提示，避免空刷。 */
  function startPollingActor(id) {
    if (state.poll[id]) return;
    state.poll[id] = true;
    (function tick() {
      api().get('/api/comic/actors/' + id).then(function (a) {
        if (a && a.status === 'generating') {
          setTimeout(tick, 2500);
        } else {
          state.poll[id] = false;
          if (a && a.status === 'failed') {
            ui().toastError('演员「' + (a.name || '') + '」重新生成失败：' + (a.error_message || ''));
          } else if (a) {
            ui().toastSuccess('演员「' + (a.name || '') + '」定妆图已更新');
          }
          loadActors();
        }
      }, function () {
        state.poll[id] = false;
      });
    })();
  }

  /* ---------------------------------------------------------- 删除 */

  function deleteActor(a) {
    ui().confirm({
      title: '删除演员',
      message: '确定删除演员「' + (a.name || '未命名演员') + '」？会解除其与全部漫画角色的关联（角色本身不会删除）。此操作不可恢复。',
      confirmLabel: '删除',
      danger: true
    }).then(function (ok) {
      if (!ok) return;
      api().del('/api/comic/actors/' + a.id).then(function () {
        ui().toastSuccess('演员已删除');
        loadActors();
      }, function (err) { ui().toastError(ui().errorText(err, '删除失败')); });
    });
  }

  /* ---------------------------------------------------------- 从演员库导入为漫画角色（comic.js 调用） */

  function openImportFromActor(project) {
    var body = ui().el('div');
    var tip = ui().el('p', 'entry-text text-tertiary');
    tip.textContent = '以演员为基准，在当前漫画《' + ((project && project.name) || '') +
      '》里新建角色卡并自动关联：角色的外貌/服装/配色/排除项全部继承演员，保证出场长相一致。';
    body.appendChild(tip);

    var grid = ui().el('div', 'card-grid');
    body.appendChild(grid);

    function loadActorsGrid() {
      grid.innerHTML = '';
      grid.appendChild(ui().loadingInline('加载演员库…'));
      api().get('/api/comic/actors?limit=' + LIST_LIMIT + '&offset=0').then(function (d) {
        grid.innerHTML = '';
        var items = (d && d.items) || [];
        if (!items.length) {
          grid.appendChild(ui().emptyState({
            icon: 'users', title: '演员库为空',
            desc: '先在「演员库」里把人物收为演员，再来这里一键建角色。'
          }));
          return;
        }
        items.forEach(function (a) { grid.appendChild(renderActorMiniCard(a, choose)); });
      }, function (err) {
        grid.innerHTML = '';
        grid.appendChild(ui().errorState(ui().errorText(err, '加载演员失败'), loadActorsGrid));
      });
    }

    function choose(a) { openFromActorOptions(project, a); }

    loadActorsGrid();

    ui().modal({
      title: '从演员库导入角色',
      desc: '选一个演员，在该漫画里以它为基准新建角色卡。',
      body: body,
      size: 'lg',
      actions: [{ label: '关闭', variant: 'ghost', onClick: function () { return true; } }]
    });
  }

  function renderActorMiniCard(a, onPick) {
    var card = ui().el('article', 'entry-card');
    if (a.image_url) {
      // 左图右文：与演员库卡片保持一致
      card.classList.add('entry-card--split');
      var im = ui().el('img', 'entry-thumb');
      im.src = a.image_url;
      im.alt = (a.name || '演员') + ' 定妆图';
      im.loading = 'lazy';
      card.appendChild(im);
    }
    var body = ui().el('div', 'entry-body');
    body.appendChild(ui().el('h4', 'entry-title', a.name || '未命名演员'));
    var badges = ui().el('div', 'result-actions');
    badges.appendChild(sourceBadge(a.source_type));
    if (a.link_count) badges.appendChild(metaBadge('出演 ' + a.link_count));
    body.appendChild(badges);
    body.appendChild(ui().el('p', 'entry-text clamp-2', a.appearance || a.anchor || '（暂无定妆描述）'));
    var actions = ui().el('div', 'entry-actions');
    actions.appendChild(btn('primary', 'plus', '导入为角色', function () { onPick(a); }));
    body.appendChild(actions);
    card.appendChild(body);
    return card;
  }

  function openFromActorOptions(project, a) {
    var body = ui().el('div');
    var inner = ui().el('div', 'form-stack');
    if (a.image_url) {
      // 左图右文：定妆图占左侧固定画框完整显示，右侧放表单
      body.classList.add('entry-split');
      var im = ui().el('img', 'entry-thumb-lg');
      im.src = a.image_url;
      im.alt = (a.name || '演员') + ' 定妆图';
      body.appendChild(im);
    }
    body.appendChild(inner);

    var nameWrap = ui().el('label', 'field');
    nameWrap.appendChild(ui().el('span', 'field-label', '角色名（默认沿用演员名）'));
    var nameInput = ui().el('input', 'input');
    nameInput.type = 'text';
    nameInput.value = a.name || '';
    nameWrap.appendChild(nameInput);
    inner.appendChild(nameWrap);

    var roleWrap = ui().el('label', 'field');
    roleWrap.appendChild(ui().el('span', 'field-label', '出演备注（可选）'));
    var roleInput = ui().el('input', 'input');
    roleInput.type = 'text';
    roleInput.placeholder = '例如 男一号';
    roleWrap.appendChild(roleInput);
    inner.appendChild(roleWrap);

    var mainWrap = ui().el('label', 'field');
    var mainChk = ui().el('input');
    mainChk.type = 'checkbox';
    mainChk.style.cssText = 'width:16px;height:16px;margin-right:8px';
    mainWrap.appendChild(mainChk);
    mainWrap.appendChild(ui().el('span', null, '设为主角（锚点注入每一页）'));
    inner.appendChild(mainWrap);

    ui().modal({
      title: '以「' + (a.name || '演员') + '」为基准新建角色',
      desc: '角色卡外貌/服装/配色/排除项将继承演员，建立演员↔角色关联。',
      body: body,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost', onClick: function () { return true; } },
        {
          label: '创建角色',
          variant: 'primary',
          onClick: function () {
            var name = String(nameInput.value || '').trim();
            if (!name) { ui().toastError('请填写角色名'); return false; }
            var payload = { actor_id: a.id, name: name };
            var rn = String(roleInput.value || '').trim();
            if (rn) payload.role_note = rn;
            payload.is_main = !!mainChk.checked;
            api().post('/api/comic/projects/' + project.id + '/characters/from-actor', payload).then(function () {
              ui().toastSuccess('角色「' + name + '」已以演员为基准创建');
              if (AIBAR.comic && AIBAR.comic.refreshCharacters) AIBAR.comic.refreshCharacters();
            }, function (err) { ui().toastError(ui().errorText(err, '创建失败')); });
            return true;
          }
        }
      ]
    });
  }

  /* ---------------------------------------------------------- 通用表单（与 comic.js openForm 同款） */

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

  /* ---------------------------------------------------------- 导出 */

  AIBAR.actors = {
    init: init,
    onEnter: onEnter,
    render: render,
    openImportFromActor: openImportFromActor
  };
})(typeof window !== 'undefined' ? window : globalThis);
