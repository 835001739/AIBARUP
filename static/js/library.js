/* ============================================================
   AIBAR · M7 / M8.6 提示词库左栏
   - 分面筛选（媒体 / 维度 / 子类 / 模型 / 排序 / 收藏 / 标签 / 来源）
   - 渐进披露：默认只显示搜索与当前分类，更多筛选在可展开面板内
   - 已激活筛选以可单项删除的 Chip 汇总
   - 词条卡片三级层次：中文短标题 / 精品正文 / 所属维度
   - 回填规则（M7.3）：光标插入、去重、可撤销 Toast、使用次数更新
   纯逻辑（insertAtCursor / buildChipList / removeFilter / mediaFiltersKey）
   同时挂到 window 与 module.exports，供 Node 测试直接 require。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};
  var doc = typeof document !== 'undefined' ? document : null;

  /* ============================================================
     一、纯逻辑（不碰 DOM，可独立测试）
     ============================================================ */

  /* 按模型档案选择连接符：英文逗号 / 自然语言句点 / 中文逗号 */
  var SEPARATORS = {
    sd15_sdxl: ', ',
    flux_flux2: '. ',
    generic: '，'
  };

  var DEFAULT_SEPARATOR = SEPARATORS.generic;

  function separatorFor(profile) {
    return SEPARATORS[profile] || DEFAULT_SEPARATOR;
  }

  /* 已经处于分隔符位置时不重复补分隔符 */
  function isBoundary(ch) {
    return /[\s,，、;；.。]/.test(ch || '');
  }

  /* ---------------------------------------------------------- 文本规范化
     与后端 core/textutil.normalize_text 等价：NFKC → 全角标点转半角 →
     折叠空白 → 去首尾标点 → 英文小写。只用于去重比较，不用于展示。
     ------------------------------------------------------------------ */

  var PUNCT_MAP = {
    '，': ',', '、': ',', '；': ',', ';': ',', '：': ':', ':': ':',
    '。': '.', '！': '!', '？': '?', '（': '(', '）': ')',
    '【': '[', '】': ']', '《': '<', '》': '>',
    '\u201c': '"', '\u201d': '"', '\u2018': "'", '\u2019': "'",
    '～': '~', '\uff0d': '-', '—': '-', '\u3000': ' ', '\t': ' '
  };

  var CTRL_RE = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;
  var WS_RE = /\s+/g;
  var TRAIL_RE = /^[\s,.:;!?\-~*#"'\(\)\[\]<>\/\\|]+|[\s,.:;!?\-~*#"'\(\)\[\]<>\/\\|]+$/g;

  /**
   * 规范化文本，用于去重与指纹比较。
   * @param {string} text
   * @returns {string}
   */
  function normalizeText(text) {
    if (!text) return '';
    var raw = String(text);
    var out = typeof raw.normalize === 'function' ? raw.normalize('NFKC') : raw;
    var buf = '';
    for (var i = 0; i < out.length; i += 1) {
      var ch = out.charAt(i);
      buf += Object.prototype.hasOwnProperty.call(PUNCT_MAP, ch) ? PUNCT_MAP[ch] : ch;
    }
    out = buf.replace(CTRL_RE, ' ').replace(WS_RE, ' ').trim();
    out = out.replace(TRAIL_RE, '');
    return out.toLowerCase();
  }

  /**
   * 把词条文本插入原始提示词。
   * @param {string} text 当前文本
   * @param {string} addition 待插入片段
   * @param {number|null} cursorPos 光标位置；为 null / 越界时追加到末尾
   * @param {string} [profile] 模型档案（决定连接符）
   * @returns {{text:string, cursorPos:number, inserted:boolean, reason:string, separator:string}}
   */
  function insertAtCursor(text, addition, cursorPos, profile) {
    var current = String(text === null || text === undefined ? '' : text);
    var piece = String(addition === null || addition === undefined ? '' : addition).trim();
    var sep = separatorFor(profile);
    var length = current.length;
    var at = (typeof cursorPos === 'number' && isFinite(cursorPos) && cursorPos >= 0 && cursorPos <= length)
      ? cursorPos
      : length;

    if (!piece) {
      return { text: current, cursorPos: at, inserted: false, reason: 'empty', separator: sep };
    }
    /*
     * 已有完全相同片段时不重复插入。
     * 判定用 normalizeText：忽略大小写、全角/半角标点差异、重复空白与首尾标点，
     * 与后端 textutil.join_text 的 `normalize_text(addition) in normalize_text(existing)`
     * 以及 endswith 分支保持一致。
     */
    var normPiece = normalizeText(piece);
    var normCurrent = normalizeText(current);
    if (normPiece && normCurrent &&
      (normCurrent.indexOf(normPiece) !== -1 ||
        normCurrent.slice(normCurrent.length - normPiece.length) === normPiece)) {
      return { text: current, cursorPos: at, inserted: false, reason: 'duplicate', separator: sep };
    }

    var before = current.slice(0, at);
    var after = current.slice(at);
    var lead = '';
    var tail = '';

    if (before) {
      var last = before.charAt(before.length - 1);
      if (!isBoundary(last)) {
        lead = sep;
      } else if (/[,，、]/.test(last) && !/\s$/.test(before)) {
        lead = ' ';
      }
    }
    if (after && !isBoundary(after.charAt(0))) {
      tail = sep;
    }

    var insertText = lead + piece + tail;
    var next = before + insertText + after;
    return {
      text: next,
      cursorPos: at + insertText.length,
      inserted: true,
      reason: '',
      separator: sep
    };
  }

  /* ---- 筛选 Chip ---- */

  var FILTER_LABELS = {
    media_type: '媒体',
    dimension: '维度',
    subcategory: '子类',
    profile: '模型',
    q: '搜索',
    favorite: '收藏',
    source: '来源',
    tag: '标签',
    sort: '排序'
  };

  var FILTER_ORDER = ['media_type', 'q', 'dimension', 'subcategory', 'profile', 'source', 'tag', 'favorite', 'sort'];
  var DEFAULT_SORT = 'recommended';
  var SORT_LABELS = {
    recommended: '推荐',
    recent: '最近新增',
    recent_used: '最近使用',
    most_used: '使用最多'
  };
  var MEDIA_LABELS = { image: '图片', video: '视频', music: '歌曲' };

  /* ---------------------------------------------------------- 词条编辑
     上下限与后端 promptlib/entries.py 保持一致（MIN_TEXT_LEN / MAX_*_LEN / MAX_TAGS），
     前端先拦一道，用户不必等一次往返才知道正文太短。
     ------------------------------------------------------------------ */

  var ENTRY_LIMITS = {
    minText: 4,
    maxText: 200,
    maxTitle: 60,
    maxDesc: 300,
    maxNegative: 300,
    maxTags: 20
  };

  /**
   * 校验词条表单。纯函数，不碰 DOM。
   *
   * @param {{prompt_text?:string, title?:string, description?:string,
   *          negative_text?:string, tags?:Array, dimension?:string}} payload
   * @param {{requireDimension?:boolean}} [options]
   *   requireDimension=true 用于**新建**：后端 insert_entry 会拿维度算内容指纹并校验
   *   合法性，缺了直接 400（"媒体 image 没有维度：(空)"）。编辑时不必填——PATCH 只改
   *   提交的字段，维度留空即是"不改"。
   * @returns {{ok:boolean, field:string, message:string}}
   */
  function validateEntryInput(payload, options) {
    var data = payload || {};
    var opts = options || {};
    var text = String(data.prompt_text === null || data.prompt_text === undefined ? '' : data.prompt_text).trim();
    if (text.length < ENTRY_LIMITS.minText) {
      return { ok: false, field: 'prompt_text', message: '提示词正文至少需要 ' + ENTRY_LIMITS.minText + ' 个字符' };
    }
    if (text.length > ENTRY_LIMITS.maxText) {
      return { ok: false, field: 'prompt_text', message: '提示词正文不能超过 ' + ENTRY_LIMITS.maxText + ' 个字符' };
    }
    if (String(data.title || '').trim().length > ENTRY_LIMITS.maxTitle) {
      return { ok: false, field: 'title', message: '标题不能超过 ' + ENTRY_LIMITS.maxTitle + ' 个字符' };
    }
    if (String(data.description || '').trim().length > ENTRY_LIMITS.maxDesc) {
      return { ok: false, field: 'description', message: '说明不能超过 ' + ENTRY_LIMITS.maxDesc + ' 个字符' };
    }
    if (String(data.negative_text || '').trim().length > ENTRY_LIMITS.maxNegative) {
      return { ok: false, field: 'negative_text', message: '反向提示词不能超过 ' + ENTRY_LIMITS.maxNegative + ' 个字符' };
    }
    var tags = data.tags || [];
    if (tags.length > ENTRY_LIMITS.maxTags) {
      return { ok: false, field: 'tags', message: '标签最多 ' + ENTRY_LIMITS.maxTags + ' 个' };
    }
    // 维度放在最后判：正文太短是更该先说的错误，别让用户先去选维度再回来发现还得改正文
    if (opts.requireDimension && !String(data.dimension || '').trim()) {
      return { ok: false, field: 'dimension', message: '请先选择一个维度' };
    }
    return { ok: true, field: '', message: '' };
  }

  /**
   * 把「标签」输入框里的一串文本切成标签数组：中英文逗号、顿号、分号、空白都能当分隔符。
   * @param {string} value
   * @returns {string[]} 去重后的标签（保持输入顺序）
   */
  function parseTags(value) {
    var out = [];
    String(value === null || value === undefined ? '' : value)
      .split(/[,，、;；\s]+/)
      .forEach(function (part) {
        var tag = part.trim();
        if (tag && out.indexOf(tag) === -1) out.push(tag);
      });
    return out;
  }

  function isFilterActive(filters, key) {
    if (!filters) return false;
    var value = filters[key];
    if (value === null || value === undefined || value === '') return false;
    if (key === 'favorite') return !(value === false || value === '0' || value === 'false');
    if (key === 'sort') return String(value) !== DEFAULT_SORT;
    return true;
  }

  /**
   * 生成已激活筛选的 Chip 列表（可单项删除）。
   * @param {Object} filters 当前筛选
   * @param {Object} [labels] 值 -> 文案映射，如 {dimension:{'subject':'主体与外观'}}
   * @returns {Array<{key:string,label:string,value:string,text:string}>}
   */
  function buildChipList(filters, labels) {
    var map = labels || {};
    var result = [];
    FILTER_ORDER.forEach(function (key) {
      if (!isFilterActive(filters, key)) return;
      var raw = filters[key];
      var value = String(raw);
      if (key === 'favorite') value = '仅收藏';
      else if (key === 'sort') value = SORT_LABELS[value] || value;
      else if (key === 'media_type') value = MEDIA_LABELS[value] || value;
      else {
        var dict = map[key];
        if (dict && dict[value]) value = String(dict[value]);
      }
      result.push({
        key: key,
        label: FILTER_LABELS[key] || key,
        value: value,
        text: (FILTER_LABELS[key] || key) + '：' + value
      });
    });
    return result;
  }

  /** 返回删除某项筛选后的新筛选对象（不修改入参） */
  function removeFilter(filters, key) {
    var next = {};
    Object.keys(filters || {}).forEach(function (item) {
      next[item] = filters[item];
    });
    next[key] = '';
    // 排序回到默认，避免残留非默认值
    if (key === 'sort') next[key] = '';
    return next;
  }

  /** 媒体类型分 key 存取的 sessionStorage 键名 */
  function mediaFiltersKey(mediaType) {
    return 'aibar:lib:filters:' + (mediaType || 'image');
  }

  /** mediaFiltersKey 的别名：切换媒体类型时按 key 分别记忆筛选状态 */
  function mediaStateKey(mediaType) {
    return mediaFiltersKey(mediaType);
  }

  /* ============================================================
     二、DOM 层
     ============================================================ */

  var PAGE_SIZE = 24;

  var state = {
    media: 'image',
    filters: { q: '', dimension: '', subcategory: '', profile: '', source: '', tag: '', favorite: '', sort: '' },
    facets: null,
    entries: [],
    total: 0,
    page: 1,
    hasMore: false,
    loading: false,
    view: 'main',
    candidates: { items: [], total: 0, page: 1, loading: false },
    requestSeq: 0
  };

  var refs = {};

  function api() { return AIBAR.api; }
  function ui() { return AIBAR.ui; }

  /* ---- 筛选状态存取（按 media_type 分 key，刷新后保留） ---- */

  var EMPTY_FILTERS = function () {
    return { q: '', dimension: '', subcategory: '', profile: '', source: '', tag: '', favorite: '', sort: '' };
  };

  function loadFilters(media) {
    var empty = EMPTY_FILTERS();
    try {
      var raw = root.sessionStorage.getItem(mediaFiltersKey(media));
      if (!raw) return empty;
      var parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return empty;
      Object.keys(empty).forEach(function (key) {
        if (typeof parsed[key] === 'string') empty[key] = parsed[key];
      });
    } catch (err) {
      /* 存储不可用时退回默认筛选，不影响使用 */
    }
    return empty;
  }

  function saveFilters() {
    try {
      root.sessionStorage.setItem(mediaFiltersKey(state.media), JSON.stringify(state.filters));
    } catch (err) {
      /* 忽略：隐私模式下 sessionStorage 可能不可用 */
    }
  }

  /* ---- 初始化 ---- */

  function init() {
    refs.search = doc.getElementById('lib-search');
    refs.chips = doc.getElementById('lib-chips');
    refs.count = doc.getElementById('lib-count');
    refs.list = doc.getElementById('lib-list');
    refs.pager = doc.getElementById('lib-pager');
    refs.filtersToggle = doc.getElementById('lib-filters-toggle');
    refs.filtersPanel = doc.getElementById('lib-filters-panel');
    refs.fDimension = doc.getElementById('lib-f-dimension');
    refs.fSubcategory = doc.getElementById('lib-f-subcategory');
    refs.fProfile = doc.getElementById('lib-f-profile');
    refs.fSort = doc.getElementById('lib-f-sort');
    refs.fSource = doc.getElementById('lib-f-source');
    refs.fTag = doc.getElementById('lib-f-tag');
    refs.favorite = doc.getElementById('lib-favorite');
    refs.viewMain = doc.getElementById('lib-view-main');
    refs.viewCandidates = doc.getElementById('lib-view-candidates');
    refs.candBtn = doc.getElementById('btn-candidates');
    refs.candCount = doc.getElementById('lib-candidate-count');
    refs.candList = doc.getElementById('lib-cand-list');
    refs.candPager = doc.getElementById('lib-cand-pager');
    refs.newBtn = doc.getElementById('btn-lib-new');

    fillSortOptions();
    fillProfileOptions();

    if (refs.newBtn) {
      refs.newBtn.addEventListener('click', function () { openEntryEditor(null); });
    }

    if (refs.search) {
      var timer = null;
      refs.search.addEventListener('input', function () {
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () {
          state.filters.q = refs.search.value.trim();
          state.page = 1;
          saveFilters();
          reload();
        }, 260);
      });
    }

    if (refs.filtersToggle && refs.filtersPanel) {
      refs.filtersToggle.addEventListener('click', function () {
        var open = refs.filtersToggle.getAttribute('aria-expanded') === 'true';
        refs.filtersToggle.setAttribute('aria-expanded', open ? 'false' : 'true');
        refs.filtersPanel.hidden = open;
      });
    }

    [['fDimension', 'dimension'], ['fSubcategory', 'subcategory'], ['fProfile', 'profile'],
      ['fSort', 'sort'], ['fSource', 'source'], ['fTag', 'tag']].forEach(function (pair) {
      var node = refs[pair[0]];
      if (!node) return;
      node.addEventListener('change', function () {
        state.filters[pair[1]] = node.value;
        // 维度变化时清空子类，避免出现无结果的组合
        if (pair[1] === 'dimension') state.filters.subcategory = '';
        state.page = 1;
        saveFilters();
        reload();
      });
    });

    if (refs.favorite) {
      refs.favorite.addEventListener('change', function () {
        state.filters.favorite = refs.favorite.checked ? '1' : '';
        state.page = 1;
        saveFilters();
        reload();
      });
    }

    if (refs.candBtn) {
      refs.candBtn.addEventListener('click', function () { openCandidates(); });
    }
    var backBtn = doc.getElementById('btn-cand-back');
    if (backBtn) {
      backBtn.addEventListener('click', function () { showView('main'); });
    }

    state.filters = loadFilters(state.media);
    syncFilterControls();
    reload();
    refreshCandidateCount();
  }

  function fillSortOptions() {
    if (!refs.fSort) return;
    refs.fSort.innerHTML = '';
    Object.keys(SORT_LABELS).forEach(function (key) {
      var option = doc.createElement('option');
      option.value = key;
      option.textContent = SORT_LABELS[key];
      refs.fSort.appendChild(option);
    });
  }

  function fillProfileOptions() {
    if (!refs.fProfile) return;
    var profiles = (AIBAR.studio && AIBAR.studio.getProfiles && AIBAR.studio.getProfiles()) || [];
    refs.fProfile.innerHTML = '';
    var all = doc.createElement('option');
    all.value = '';
    all.textContent = '全部模型档案';
    refs.fProfile.appendChild(all);
    profiles.forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.key;
      option.textContent = item.label;
      refs.fProfile.appendChild(option);
    });
  }

  function syncFilterControls() {
    if (refs.search) refs.search.value = state.filters.q || '';
    if (refs.fDimension) refs.fDimension.value = state.filters.dimension || '';
    if (refs.fSubcategory) refs.fSubcategory.value = state.filters.subcategory || '';
    if (refs.fProfile) refs.fProfile.value = state.filters.profile || '';
    if (refs.fSort) refs.fSort.value = state.filters.sort || DEFAULT_SORT;
    if (refs.fSource) refs.fSource.value = state.filters.source || '';
    if (refs.fTag) refs.fTag.value = state.filters.tag || '';
    if (refs.favorite) refs.favorite.checked = isFilterActive(state.filters, 'favorite');
  }

  /* ---- 数据加载 ---- */

  function entryParams() {
    var params = {
      media_type: state.media,
      q: state.filters.q,
      dimension: state.filters.dimension,
      subcategory: state.filters.subcategory,
      profile: state.filters.profile,
      source: state.filters.source,
      tag: state.filters.tag,
      sort: state.filters.sort || DEFAULT_SORT,
      page: state.page,
      page_size: PAGE_SIZE
    };
    if (isFilterActive(state.filters, 'favorite')) params.favorite = '1';
    return params;
  }

  function reload() {
    if (!refs.list) return;
    var seq = ++state.requestSeq;
    state.loading = true;
    renderSkeleton();
    renderChips();

    var facetsParams = {
      media_type: state.media,
      dimension: state.filters.dimension,
      subcategory: state.filters.subcategory,
      profile: state.filters.profile
    };

    api().get('/api/prompt-library/facets', facetsParams).then(function (facets) {
      if (seq !== state.requestSeq) return;
      state.facets = facets || {};
      renderFacetOptions();
      renderCount();
    }, function () {
      /* 分面失败不阻断词条列表 */
    });

    api().get('/api/prompt-library/entries', entryParams()).then(function (data) {
      if (seq !== state.requestSeq) return;
      state.loading = false;
      state.entries = (data && data.items) || [];
      state.total = (data && data.total) || 0;
      state.hasMore = !!(data && data.has_more);
      renderList();
      renderCount();
    }, function (err) {
      if (seq !== state.requestSeq) return;
      state.loading = false;
      renderError(err);
    });
  }

  function renderSkeleton() {
    refs.list.innerHTML = '';
    refs.list.appendChild(ui().skeletonCards(4));
    refs.pager.innerHTML = '';
  }

  function renderError(err) {
    refs.list.innerHTML = '';
    refs.list.appendChild(ui().errorState(ui().errorText(err, '提示词库加载失败'), function () { reload(); }));
    refs.pager.innerHTML = '';
  }

  function renderCount() {
    if (!refs.count) return;
    var mediaList = (state.facets && state.facets.media_types) || [];
    var current = null;
    mediaList.forEach(function (item) {
      if (item.key === state.media) current = item;
    });
    var parts = [];
    parts.push((MEDIA_LABELS[state.media] || state.media) + ' ' + (state.total || 0) + ' 条');
    if (current) {
      parts.push('收藏 ' + (current.favorite_count || 0));
      parts.push('最近新增 ' + (current.recent_count || 0));
    }
    refs.count.textContent = parts.join(' · ');
  }

  /* ---- 分面选项（只展示仍有数据的层级） ---- */

  function fillSelect(select, items, placeholder, currentValue) {
    if (!select) return;
    select.innerHTML = '';
    var all = doc.createElement('option');
    all.value = '';
    all.textContent = placeholder;
    select.appendChild(all);
    (items || []).forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.key;
      option.textContent = item.label + '（' + (item.count === undefined ? 0 : item.count) + '）';
      select.appendChild(option);
    });
    select.value = currentValue || '';
  }

  function renderFacetOptions() {
    var facets = state.facets || {};
    fillSelect(refs.fDimension, facets.dimensions, '全部维度', state.filters.dimension);
    fillSelect(refs.fSubcategory, facets.subcategories, '全部子类', state.filters.subcategory);
    fillSelect(refs.fSource, facets.sources, '全部来源', state.filters.source);

    if (refs.fTag) {
      var tags = facets.tags || [];
      refs.fTag.innerHTML = '';
      var all = doc.createElement('option');
      all.value = '';
      all.textContent = '全部标签';
      refs.fTag.appendChild(all);
      tags.slice(0, 60).forEach(function (item) {
        var option = doc.createElement('option');
        option.value = item.key;
        option.textContent = item.key + '（' + (item.count === undefined ? 0 : item.count) + '）';
        refs.fTag.appendChild(option);
      });
      refs.fTag.value = state.filters.tag || '';
    }
  }

  function facetLabels() {
    var facets = state.facets || {};
    var map = {};
    ['dimensions', 'subcategories', 'sources'].forEach(function (key) {
      map[key === 'dimensions' ? 'dimension' : (key === 'subcategories' ? 'subcategory' : 'source')] =
        (facets[key] || []).reduce(function (acc, item) {
          acc[item.key] = item.label;
          return acc;
        }, {});
    });
    return map;
  }

  /* ---- 已激活筛选 Chip ---- */

  function renderChips() {
    if (!refs.chips) return;
    refs.chips.innerHTML = '';
    var chips = buildChipList(state.filters, facetLabels());
    if (!chips.length) {
      var hint = ui().el('span', 'text-tertiary', '未设置筛选');
      refs.chips.appendChild(hint);
      return;
    }
    chips.forEach(function (chip) {
      var node = ui().el('span', 'chip');
      node.appendChild(ui().el('span', 'chip-label', chip.label));
      node.appendChild(ui().el('span', 'chip-value break-any', chip.value));
      var remove = ui().el('button', 'chip-remove');
      remove.type = 'button';
      remove.setAttribute('aria-label', '移除筛选：' + chip.text);
      remove.innerHTML = AIBAR.icons.get('close', 12);
      remove.addEventListener('click', function () {
        state.filters = removeFilter(state.filters, chip.key);
        if (chip.key === 'q' && refs.search) refs.search.value = '';
        if (chip.key === 'favorite' && refs.favorite) refs.favorite.checked = false;
        state.page = 1;
        saveFilters();
        syncFilterControls();
        reload();
      });
      node.appendChild(remove);
      refs.chips.appendChild(node);
    });

    var clear = ui().el('button', 'btn btn-ghost btn-sm', '清空筛选');
    clear.type = 'button';
    clear.addEventListener('click', function () {
      state.filters = EMPTY_FILTERS();
      state.page = 1;
      saveFilters();
      syncFilterControls();
      reload();
    });
    refs.chips.appendChild(clear);
  }

  /* ---- 词条卡片 ---- */

  function renderList() {
    refs.list.innerHTML = '';
    if (!state.entries.length) {
      var active = buildChipList(state.filters).length > 0;
      refs.list.appendChild(ui().emptyState({
        icon: 'search',
        title: '没有匹配的精品词条',
        desc: active ? '当前筛选条件下没有结果，试试移除部分筛选或缩短关键词。' : '该分类下还没有词条，换个维度或媒体类型看看。',
        actions: active ? [{
          label: '清除筛选',
          variant: 'secondary',
          onClick: function () {
            state.filters = EMPTY_FILTERS();
            state.page = 1;
            saveFilters();
            syncFilterControls();
            reload();
          }
        }] : []
      }));
      refs.pager.innerHTML = '';
      return;
    }

    state.entries.forEach(function (item) {
      refs.list.appendChild(renderCard(item));
    });
    renderPager();
  }

  function renderCard(item) {
    var card = ui().el('article', 'entry-card');
    card.dataset.entryId = String(item.id);

    var title = ui().el('div', 'entry-title');
    title.appendChild(ui().el('span', '', item.title || '未命名片段'));
    if (item.is_favorite) {
      var star = ui().el('span', 'badge badge-warning', '已收藏');
      title.appendChild(star);
    }
    card.appendChild(title);

    var text = ui().el('p', 'entry-text clamp-3', item.prompt_text || '');
    card.appendChild(text);

    var foot = ui().el('div', 'entry-foot');
    var dimLabel = item.dimension_label || item.dimension || '未分类';
    var path = dimLabel + (item.subcategory_label ? ' · ' + item.subcategory_label : '');
    foot.appendChild(ui().el('span', 'badge', path));
    foot.appendChild(ui().el('span', '', '使用 ' + (item.use_count || 0) + ' 次'));

    var actions = ui().el('div', 'entry-actions');

    var favBtn = ui().el('button', 'icon-btn icon-btn-sm');
    favBtn.type = 'button';
    favBtn.setAttribute('aria-label', item.is_favorite ? '取消收藏该词条' : '收藏该词条');
    favBtn.setAttribute('aria-pressed', item.is_favorite ? 'true' : 'false');
    favBtn.innerHTML = AIBAR.icons.star(!!item.is_favorite, 15);
    if (item.is_favorite) favBtn.classList.add('is-on');
    favBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      toggleFavorite(item, card);
    });
    actions.appendChild(favBtn);

    var copyBtn = ui().el('button', 'icon-btn icon-btn-sm');
    copyBtn.type = 'button';
    copyBtn.setAttribute('aria-label', '复制该词条正文');
    copyBtn.innerHTML = AIBAR.icons.get('copy', 15);
    copyBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      // 复制只复制：不回填、不增加使用次数
      ui().copyText(item.prompt_text || '');
    });
    actions.appendChild(copyBtn);

    var editBtn = ui().el('button', 'icon-btn icon-btn-sm');
    editBtn.type = 'button';
    editBtn.setAttribute('aria-label', '编辑该词条');
    editBtn.innerHTML = AIBAR.icons.get('edit', 15);
    editBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      openEntryEditor(item, card);
    });
    actions.appendChild(editBtn);

    var delBtn = ui().el('button', 'icon-btn icon-btn-sm');
    delBtn.type = 'button';
    delBtn.setAttribute('aria-label', '删除该词条');
    delBtn.innerHTML = AIBAR.icons.get('trash', 15);
    delBtn.addEventListener('click', function (event) {
      event.stopPropagation();
      removeEntry(item, card);
    });
    actions.appendChild(delBtn);

    foot.appendChild(actions);
    card.appendChild(foot);

    var insert = ui().el('button', 'btn btn-secondary btn-sm entry-insert');
    insert.type = 'button';
    insert.innerHTML = AIBAR.icons.get('plus', 14) + '<span>加入原始提示词</span>';
    insert.addEventListener('click', function (event) {
      event.stopPropagation();
      insertEntry(item, card);
    });
    card.appendChild(insert);

    // 点击卡片主体即回填（最主要行为）
    card.addEventListener('click', function (event) {
      if (event.target.closest('button')) return;
      insertEntry(item, card);
    });

    return card;
  }

  function renderPager() {
    refs.pager.innerHTML = '';
    var totalPages = Math.max(1, Math.ceil(state.total / PAGE_SIZE));
    var info = ui().el('span', '', '第 ' + state.page + ' / ' + totalPages + ' 页 · 共 ' + state.total + ' 条');
    refs.pager.appendChild(info);

    var prev = ui().el('button', 'btn btn-ghost btn-sm', '上一页');
    prev.type = 'button';
    prev.disabled = state.page <= 1;
    prev.addEventListener('click', function () {
      if (state.page > 1) {
        state.page -= 1;
        reload();
      }
    });
    refs.pager.appendChild(prev);

    var next = ui().el('button', 'btn btn-ghost btn-sm', '下一页');
    next.type = 'button';
    next.disabled = !state.hasMore;
    next.addEventListener('click', function () {
      if (state.hasMore) {
        state.page += 1;
        reload();
      }
    });
    refs.pager.appendChild(next);
  }

  /* ---- 词条操作 ---- */

  function flashCard(card) {
    if (!card) return;
    card.classList.remove('is-flash');
    // 强制重排以重启动画
    void card.offsetWidth;
    card.classList.add('is-flash');
    card.scrollIntoView({ block: 'nearest' });
  }

  function insertEntry(item, card) {
    var inserted = AIBAR.studio.insertFromLibrary(item);
    if (inserted) {
      flashCard(card);
      // 插入成功后更新使用统计（PRD M7.3）
      api().post('/api/prompt-library/entries/' + encodeURIComponent(item.id) + '/use', {
        profile: AIBAR.studio.getProfile(),
        existing: AIBAR.studio.getPromptText()
      }).then(function (data) {
        var useNode = card && card.querySelector('.entry-foot span:nth-child(2)');
        if (useNode && item.use_count !== undefined) {
          item.use_count = (item.use_count || 0) + 1;
          useNode.textContent = '使用 ' + item.use_count + ' 次';
        }
        if (data && data.duplicate) {
          ui().toast({ message: '该片段已存在于原始提示词中', type: 'info' });
        }
      }, function () {
        /* 统计失败不影响回填结果 */
      });
    }
  }

  function toggleFavorite(item, card) {
    var next = !item.is_favorite;
    api().put('/api/prompt-library/entries/' + encodeURIComponent(item.id) + '/favorite', { favorite: next })
      .then(function (data) {
        item.is_favorite = !!(data && data.is_favorite !== undefined ? data.is_favorite : next);
        var index = state.entries.indexOf(item);
        if (index >= 0 && refs.list.children[index]) {
          refs.list.replaceChild(renderCard(item), refs.list.children[index]);
        } else if (card) {
          refs.list.replaceChild(renderCard(item), card);
        }
        ui().toast({ message: item.is_favorite ? '已加入收藏' : '已取消收藏', type: 'success' });
        if (isFilterActive(state.filters, 'favorite')) reload();
      }, function (err) {
        ui().toastError(ui().errorText(err, '收藏失败'));
      });
  }

  /* ---- 词条编辑（增 / 改 / 删） ---- */

  function field(labelText, control, hint) {
    var wrap = ui().el('label', 'field');
    wrap.appendChild(ui().el('span', 'field-label', labelText));
    wrap.appendChild(control);
    if (hint) wrap.appendChild(ui().el('span', 'field-hint', hint));
    return wrap;
  }

  function textInput(id, value, placeholder, multiline) {
    var node;
    if (multiline) {
      node = doc.createElement('textarea');
      node.rows = 3;
    } else {
      node = doc.createElement('input');
      node.type = 'text';
    }
    node.id = id;
    node.className = 'input';
    node.value = value || '';
    if (placeholder) node.placeholder = placeholder;
    return node;
  }

  function selectInput(id, options, current, placeholder) {
    var node = doc.createElement('select');
    node.id = id;
    node.className = 'select';
    var all = doc.createElement('option');
    all.value = '';
    all.textContent = placeholder;
    node.appendChild(all);
    (options || []).forEach(function (item) {
      var option = doc.createElement('option');
      option.value = item.key;
      option.textContent = item.label;
      node.appendChild(option);
    });
    node.value = current || '';
    return node;
  }

  /**
   * 新建（item 为 null）或编辑一条词条。
   *
   * 后端 PATCH 的语义是「给了的字段才改」，所以编辑时只提交用户动过的字段，
   * 免得把内置的 source_type 之类追溯信息一起覆盖掉。
   */
  function openEntryEditor(item, card) {
    var editing = !!item;
    var data = item || {};
    var facets = state.facets || {};

    var form = ui().el('div', 'filter-grid');
    var title = textInput('entry-title', data.title || '', '中文短标题，可留空');
    var text = textInput('entry-text', data.prompt_text || '', '提示词正文（必填）', true);
    var negative = textInput('entry-negative', data.negative_text || '', '反向提示词，可留空', true);
    var tags = textInput('entry-tags', (data.tags || []).join('、'), '用逗号或顿号分隔');
    var desc = textInput('entry-desc', data.description || '', '补充说明，可留空', true);
    var dimension = selectInput(
      'entry-dimension',
      (facets.dimensions || []).map(function (d) { return { key: d.key, label: d.label }; }),
      data.dimension || state.filters.dimension || '',
      '未分类'
    );
    var subcategory = selectInput('entry-subcategory', [], data.subcategory || '', '未分类');

    form.appendChild(field('标题', title));
    // 新建时维度必填：后端要用它算指纹并校验合法性，留空会直接 400
    form.appendChild(field('维度', dimension, editing ? '' : '新建必填'));
    form.appendChild(field('子类', subcategory));
    form.appendChild(field('提示词正文', text, '至少 ' + ENTRY_LIMITS.minText + ' 个字符'));
    form.appendChild(field('反向提示词', negative));
    form.appendChild(field('标签', tags));
    form.appendChild(field('说明', desc));

    // 子类随维度变化：不同维度的子类集合不一样，用错会直接被后端拒。
    // 两个坑：
    //   1) 换的是 subcategory 的**真实父节点**——它被 field() 包在 <label class="field">
    //      里，不是 form 的直接子节点，写 form.replaceChild 会抛 NotFoundError；
    //   2) 每次刷新都更新 facetsPending，且落地后清空。提交前据此等待，
    //      否则「改完维度立刻保存」会把旧维度的子类送上去，被后端 400 拒掉。
    var facetsPending = null;

    function refreshSubcategories() {
      var current = subcategory.value;
      var request = api().get('/api/prompt-library/facets', {
        media_type: state.media,
        dimension: dimension.value
      });
      facetsPending = request;
      var settle = function () { if (facetsPending === request) facetsPending = null; };
      return request.then(function (next) {
        var list = (next && next.subcategories) || [];
        var box = selectInput(
          'entry-subcategory',
          list.map(function (s) { return { key: s.key, label: s.label }; }),
          current,
          '未分类'
        );
        var holder = subcategory.parentNode || form;
        holder.replaceChild(box, subcategory);
        subcategory = box;
        return box;
      }, function () {
        /* 取不到分面就保留原选项，不阻断编辑 */
        return subcategory;
      }).then(function (box) { settle(); return box; }, function (err) { settle(); throw err; });
    }
    dimension.addEventListener('change', refreshSubcategories);
    // 打开即拉一次：上面的 select 是空壳，不拉就没有子类可选
    refreshSubcategories();

    var entry = ui().modal({
      title: editing ? '编辑词条' : '新建词条',
      desc: editing
        ? '修改后立即生效；内置词条来源保持不变，便于追溯。'
        : '新建的词条会立即进入词库，可在下方列表中检索到。',
      body: form,
      size: 'lg',
      actions: [
        { label: '取消', variant: 'ghost' },
        {
          label: editing ? '保存修改' : '创建',
          variant: 'primary',
          close: false,
          onClick: function (modalEntry) {
            return submitEntryForm({
              modal: modalEntry,
              editing: editing,
              item: data,
              card: card,
              title: title,
              text: text,
              negative: negative,
              tags: tags,
              desc: desc,
              dimension: dimension,
              subcategory: function () { return subcategory; },
              facetsPending: function () { return facetsPending; }
            });
          }
        }
      ]
    });
    // 焦点落在正文而不是关闭按钮，新建时更顺手
    if (text && typeof text.focus === 'function') text.focus();
    return entry;
  }

  /** 收集表单 -> 校验 -> 提交。返回 false 时模态框保持打开。 */
  function submitEntryForm(ctx) {
    var btn = primaryButton(ctx.modal);
    // 维度刚改过、子类列表还在刷新时先等它落地：否则提交的是旧维度的子类，
    // 后端会以 400 拒掉，用户只看到一句「创建失败」却不知错在哪。
    // 重入是安全的：刷新落地时已把 facetsPending 清空，第二次进来直接往下走。
    var pending = ctx.facetsPending ? ctx.facetsPending() : null;
    if (pending && typeof pending.then === 'function') {
      ui().setBusy(btn, true);
      var retry = function () {
        ui().setBusy(btn, false);
        submitEntryForm(ctx);
      };
      pending.then(retry, retry);
      return false;
    }

    var subcategory = ctx.subcategory ? ctx.subcategory() : null;
    var payload = {
      title: ctx.title.value.trim(),
      prompt_text: ctx.text.value.trim(),
      negative_text: ctx.negative.value.trim(),
      description: ctx.desc.value.trim(),
      tags: parseTags(ctx.tags.value),
      dimension: ctx.dimension.value,
      subcategory: subcategory ? subcategory.value : ''
    };
    if (!payload.dimension) delete payload.dimension;
    if (!payload.subcategory) delete payload.subcategory;

    // 新建强制要维度，编辑不强制（留空 = 不改这一项）
    var check = validateEntryInput(payload, { requireDimension: !ctx.editing });
    if (!check.ok) {
      ui().toastError(check.message);
      var focusMap = { prompt_text: ctx.text, dimension: ctx.dimension };
      var target = focusMap[check.field];
      if (target && target.focus) target.focus();
      return false;
    }

    ui().setBusy(btn, true);
    var done = function (message) {
      ui().setBusy(btn, false);
      ui().toastSuccess(message);
      ctx.modal.close(true);
      reload();
    };
    var failed = function (err) {
      ui().setBusy(btn, false);
      ui().toastError(ui().errorText(err, ctx.editing ? '保存失败' : '创建失败'));
    };

    if (ctx.editing) {
      api().patch('/api/prompt-library/entries/' + encodeURIComponent(ctx.item.id), payload)
        .then(function () { done('词条已更新'); }, failed);
    } else {
      payload.media_type = state.media;
      payload.language = 'zh';
      api().post('/api/prompt-library/entries', payload)
        .then(function () { done('词条已创建'); }, failed);
    }
    return false;  // 等请求回来再关，失败时留在原表单
  }

  /**
   * 找模态框底部的「主按钮」，提交期间把它置为忙碌态防重复点击。
   * 从 modal.root（overlay）往下查，而不是靠 body.parentNode —— 后者依赖
   * 当前的 DOM 层级，改一次结构就悄悄失效。
   */
  function primaryButton(modalEntry) {
    if (!modalEntry) return null;
    var scope = modalEntry.root || modalEntry.body || null;
    if (!scope || typeof scope.querySelector !== 'function') return null;
    return scope.querySelector('.modal-foot .btn-primary');
  }

  function removeEntry(item, card) {
    ui().confirm({
      title: '删除这条词条？',
      message: '删除后写入忽略指纹，后续同步与自动学习不会再把它推荐出来。'
        + (item.source_type === 'builtin' ? '这是内置词条，删除只在本机隐藏，不改动内置资源。' : ''),
      confirmLabel: '删除',
      danger: true,
      onConfirm: function () {
        api().del('/api/prompt-library/entries/' + encodeURIComponent(item.id)).then(function () {
          ui().toastSuccess(item.source_type === 'builtin' ? '已隐藏该内置词条' : '已删除该词条');
          if (card && card.parentNode) card.parentNode.removeChild(card);
          reload();
        }, function (err) {
          ui().toastError(ui().errorText(err, '删除失败'));
        });
      }
    });
  }

  /* ---- 候选审核（左栏内部视图切换，不遮挡画布） ---- */

  function showView(view) {
    state.view = view;
    if (refs.viewMain) refs.viewMain.hidden = view !== 'main';
    if (refs.viewCandidates) refs.viewCandidates.hidden = view !== 'candidates';
    if (view === 'candidates') loadCandidates(1);
  }

  function openCandidates() {
    showView('candidates');
  }

  function refreshCandidateCount() {
    if (!refs.candCount) return;
    api().get('/api/prompt-library/candidates', { status: 'pending', media_type: state.media, page: 1, page_size: 1 })
      .then(function (data) {
        var total = (data && data.total) || 0;
        state.candidates.total = total;
        refs.candCount.textContent = String(total);
        if (refs.candBtn) {
          refs.candBtn.title = '待确认候选 ' + total + ' 条';
        }
      }, function () {
        refs.candCount.textContent = '0';
      });
  }

  function loadCandidates(page) {
    if (state.candidates.loading) return;
    state.candidates.loading = true;
    state.candidates.page = page || 1;
    refs.candList.innerHTML = '';
    refs.candList.appendChild(ui().loadingInline('正在加载候选…'));

    api().get('/api/prompt-library/candidates', {
      status: 'pending',
      media_type: state.media,
      page: state.candidates.page,
      page_size: 20
    }).then(function (data) {
      state.candidates.loading = false;
      state.candidates.items = (data && data.items) || [];
      state.candidates.total = (data && data.total) || 0;
      if (refs.candCount) refs.candCount.textContent = String(state.candidates.total);
      renderCandidates();
    }, function (err) {
      state.candidates.loading = false;
      refs.candList.innerHTML = '';
      refs.candList.appendChild(ui().errorState(ui().errorText(err, '候选加载失败'), function () { loadCandidates(1); }));
    });
  }

  function renderCandidates() {
    refs.candList.innerHTML = '';
    if (!state.candidates.items.length) {
      refs.candList.appendChild(ui().emptyState({
        icon: 'checkCircle',
        title: '没有待确认候选',
        desc: '自动学习产生的中等置信度片段会出现在这里，等待你批准或拒绝。'
      }));
      refs.candPager.innerHTML = '';
      return;
    }
    state.candidates.items.forEach(function (item) {
      refs.candList.appendChild(renderCandidate(item));
    });

    refs.candPager.innerHTML = '';
    var totalPages = Math.max(1, Math.ceil(state.candidates.total / 20));
    refs.candPager.appendChild(ui().el('span', '', '第 ' + state.candidates.page + ' / ' + totalPages + ' 页'));
    if (state.candidates.page > 1) {
      var prev = ui().el('button', 'btn btn-ghost btn-sm', '上一页');
      prev.type = 'button';
      prev.addEventListener('click', function () { loadCandidates(state.candidates.page - 1); });
      refs.candPager.appendChild(prev);
    }
    if (state.candidates.page < totalPages) {
      var next = ui().el('button', 'btn btn-ghost btn-sm', '下一页');
      next.type = 'button';
      next.addEventListener('click', function () { loadCandidates(state.candidates.page + 1); });
      refs.candPager.appendChild(next);
    }
  }

  function renderCandidate(item) {
    var card = ui().el('div', 'cand-card');
    var head = ui().el('div', 'entry-foot');
    head.appendChild(ui().el('span', 'badge', item.suggested_dimension || '未分类'));
    if (item.confidence !== undefined && item.confidence !== null) {
      head.appendChild(ui().el('span', '', '置信度 ' + Math.round(Number(item.confidence) * 100) + '%'));
    }
    if (item.occurrence_count) {
      head.appendChild(ui().el('span', '', '出现 ' + item.occurrence_count + ' 次'));
    }
    card.appendChild(head);
    card.appendChild(ui().el('p', 'cand-text', item.raw_text || item.normalized_text || ''));

    var actions = ui().el('div', 'cand-actions');
    var approve = ui().el('button', 'btn btn-secondary btn-sm', '批准入库');
    approve.type = 'button';
    approve.addEventListener('click', function () {
      api().post('/api/prompt-library/candidates/' + encodeURIComponent(item.id) + '/approve', {})
        .then(function () {
          ui().toastSuccess('已批准该候选词条');
          loadCandidates(state.candidates.page);
          refreshCandidateCount();
          reload();
        }, function (err) {
          ui().toastError(ui().errorText(err, '批准失败'));
        });
    });
    actions.appendChild(approve);

    var editApprove = ui().el('button', 'btn btn-ghost btn-sm', '编辑后批准');
    editApprove.type = 'button';
    editApprove.addEventListener('click', function () { openCandidateEditor(item); });
    actions.appendChild(editApprove);

    var reject = ui().el('button', 'btn btn-danger btn-sm', '拒绝');
    reject.type = 'button';
    reject.addEventListener('click', function () {
      ui().confirm({
        title: '拒绝该候选？',
        message: '拒绝后会写入忽略指纹，后续不再推荐这段内容。',
        confirmLabel: '拒绝',
        danger: true,
        onConfirm: function () {
          api().post('/api/prompt-library/candidates/' + encodeURIComponent(item.id) + '/reject', {})
            .then(function () {
              ui().toastSuccess('已拒绝该候选');
              loadCandidates(state.candidates.page);
              refreshCandidateCount();
            }, function (err) {
              ui().toastError(ui().errorText(err, '拒绝失败'));
            });
        }
      });
    });
    actions.appendChild(reject);
    card.appendChild(actions);
    return card;
  }

  function openCandidateEditor(item) {
    var body = ui().el('div', 'filter-grid');

    var titleField = ui().el('label', 'field');
    titleField.appendChild(ui().el('span', 'field-label', '标题'));
    var titleInput = ui().el('input', 'input');
    titleInput.type = 'text';
    titleInput.value = item.raw_text ? String(item.raw_text).slice(0, 24) : '';
    titleField.appendChild(titleInput);
    body.appendChild(titleField);

    var dimField = ui().el('label', 'field');
    dimField.appendChild(ui().el('span', 'field-label', '维度'));
    var dimSelect = ui().el('select', 'select');
    var empty = doc.createElement('option');
    empty.value = '';
    empty.textContent = '使用建议维度';
    dimSelect.appendChild(empty);
    ((state.facets && state.facets.dimensions) || []).forEach(function (dim) {
      var option = doc.createElement('option');
      option.value = dim.key;
      option.textContent = dim.label;
      dimSelect.appendChild(option);
    });
    dimSelect.value = item.suggested_dimension || '';
    dimField.appendChild(dimSelect);
    body.appendChild(dimField);

    var subField = ui().el('label', 'field');
    subField.appendChild(ui().el('span', 'field-label', '子类'));
    var subSelect = ui().el('select', 'select');
    var subEmpty = doc.createElement('option');
    subEmpty.value = '';
    subEmpty.textContent = '不指定';
    subSelect.appendChild(subEmpty);
    ((state.facets && state.facets.subcategories) || []).forEach(function (sub) {
      var option = doc.createElement('option');
      option.value = sub.key;
      option.textContent = sub.label;
      subSelect.appendChild(option);
    });
    subSelect.value = item.suggested_subcategory || '';
    subField.appendChild(subSelect);
    body.appendChild(subField);

    ui().modal({
      title: '编辑后批准',
      desc: '确认后该片段进入正式词库，来源标记为自动学习。',
      body: body,
      actions: [
        { label: '取消', variant: 'ghost' },
        {
          label: '批准入库',
          variant: 'primary',
          onClick: function () {
            var payload = {};
            if (titleInput.value.trim()) payload.title = titleInput.value.trim();
            if (dimSelect.value) payload.dimension = dimSelect.value;
            if (subSelect.value) payload.subcategory = subSelect.value;
            api().post('/api/prompt-library/candidates/' + encodeURIComponent(item.id) + '/approve', payload)
              .then(function () {
                ui().toastSuccess('已批准该候选词条');
                loadCandidates(state.candidates.page);
                refreshCandidateCount();
                reload();
              }, function (err) {
                ui().toastError(ui().errorText(err, '批准失败'));
              });
          }
        }
      ]
    });
  }

  /* ---- 对外接口 ---- */

  /**
   * 切换媒体类型：筛选状态按媒体分别记忆，切换后恢复该媒体的筛选（PRD M7.2）。
   * @param {string} media image|video|music
   */
  function setMediaType(media) {
    var next = media || 'image';
    if (next === state.media) return;
    state.media = next;
    state.page = 1;
    state.filters = loadFilters(next);
    syncFilterControls();
    reload();
    refreshCandidateCount();
  }

  AIBAR.library = {
    init: init,
    reload: reload,
    setMediaType: setMediaType,
    refreshCandidateCount: refreshCandidateCount,
    getMediaType: function () { return state.media; },
    PAGE_SIZE: PAGE_SIZE
  };

  /* 纯逻辑导出：浏览器挂 window.AIBAR.logic，Node 走 module.exports */
  var logic = AIBAR.logic = AIBAR.logic || {};
  logic.insertAtCursor = insertAtCursor;
  logic.normalizeText = normalizeText;
  logic.separatorFor = separatorFor;
  logic.buildChipList = buildChipList;
  logic.removeFilter = removeFilter;
  logic.isFilterActive = isFilterActive;
  logic.mediaFiltersKey = mediaFiltersKey;
  logic.mediaStateKey = mediaStateKey;
  logic.SORT_LABELS = SORT_LABELS;
  logic.MEDIA_LABELS = MEDIA_LABELS;
  logic.FILTER_LABELS = FILTER_LABELS;
  logic.validateEntryInput = validateEntryInput;
  logic.parseTags = parseTags;
  logic.ENTRY_LIMITS = ENTRY_LIMITS;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      insertAtCursor: insertAtCursor,
      normalizeText: normalizeText,
      separatorFor: separatorFor,
      buildChipList: buildChipList,
      removeFilter: removeFilter,
      isFilterActive: isFilterActive,
      mediaFiltersKey: mediaFiltersKey,
      mediaStateKey: mediaStateKey,
      SORT_LABELS: SORT_LABELS,
      MEDIA_LABELS: MEDIA_LABELS,
      FILTER_LABELS: FILTER_LABELS,
      validateEntryInput: validateEntryInput,
      parseTags: parseTags,
      ENTRY_LIMITS: ENTRY_LIMITS
    };
  }

  if (typeof window !== 'undefined') {
    window.AibarUtil = window.AibarUtil || {};
    window.AibarUtil.insertAtCursor = insertAtCursor;
    window.AibarUtil.normalizeText = normalizeText;
    window.AibarUtil.buildChipList = buildChipList;
    window.AibarUtil.removeFilter = removeFilter;
    window.AibarUtil.mediaStateKey = mediaStateKey;
  }
})(typeof window !== 'undefined' ? window : globalThis);
