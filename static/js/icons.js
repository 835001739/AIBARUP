/* ============================================================
   AIBAR · 内联 SVG 图标集
   约束（PRD M8.1）：不使用 emoji 作为功能/分类图标，不引入图标字体或 CDN。
   全部为 24×24 线性图标，使用 currentColor 继承文字颜色。
   ============================================================ */

(function (root) {
  'use strict';

  var AIBAR = root.AIBAR = root.AIBAR || {};

  var P = {
    /* 导航 */
    sparkles: '<path d="M12 3l1.9 4.6L18.5 9.5 13.9 11.4 12 16l-1.9-4.6L5.5 9.5l4.6-1.9L12 3Z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2Z"/>',
    workflow: '<rect x="3" y="3" width="7" height="6" rx="1.6"/><rect x="14" y="15" width="7" height="6" rx="1.6"/><path d="M6.5 9v6a2 2 0 0 0 2 2H14"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2.4"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M4 17l4.5-4.5 3.5 3.5 3-3L20 17"/>',
    layers: '<path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="M3 13.5 12 18.5l9-5"/>',
    book: '<path d="M4 5.5A2 2 0 0 1 6 4h6v15H6a2 2 0 0 0-2 1.5V5.5Z"/><path d="M20 5.5A2 2 0 0 0 18 4h-6v15h6a2 2 0 0 1 2-1.5V5.5Z"/>',
    cube: '<path d="M12 3 4 7v10l8 4 8-4V7l-8-4Z"/><path d="M4 7l8 4 8-4"/><path d="M12 11v10"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.6" cy="6" r="1.2"/><circle cx="3.6" cy="12" r="1.2"/><circle cx="3.6" cy="18" r="1.2"/>',
    /* 通用操作 */
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.6-3.6"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.6-5.9"/><path d="M20 4v5h-5"/>',
    play: '<path d="M7 4.5 19 12 7 19.5V4.5Z"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    check: '<path d="m4 12.5 5 5L20 6.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    trash: '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/>',
    copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
    clipboard: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1"/><path d="M9 11h6M9 15h4"/>',
    film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
    paste: '<path d="M9 4h6v3H9z"/><path d="M7 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-1"/><path d="M9 13h6M9 17h4"/>',
    import: '<path d="M12 3v11"/><path d="m8 10 4 4 4-4"/><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
    edit: '<path d="M4 20h4L20 8l-4-4L4 16v4Z"/><path d="m14 6 4 4"/>',
    undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h9a7 7 0 0 1 0 14H8"/>',
    download: '<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M4 20h16"/>',
    upload: '<path d="M12 21V9"/><path d="m7 13 5-5 5 5"/><path d="M4 4h16"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    filter: '<path d="M4 5h16l-6.2 7.4V19l-3.6-2v-4.6L4 5Z"/>',
    activity: '<path d="M3 12h4l2.5-7 4 14L16 12h5"/>',
    chevronDown: '<path d="m6 9 6 6 6-6"/>',
    chevronRight: '<path d="m9 6 6 6-6 6"/>',
    chevronLeft: '<path d="m15 6-6 6 6 6"/>',
    panelLeft: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    panelRight: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    sliders: '<path d="M4 8h10M18 8h2M4 16h4M12 16h8"/><circle cx="16" cy="8" r="2"/><circle cx="10" cy="16" r="2"/>',
    /* 人物与进程（漫画工作室） */
    users: '<circle cx="9" cy="8" r="3.2"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><path d="M16.2 5.3a3.2 3.2 0 0 1 0 6.1"/><path d="M17.6 14.6c2.1.8 3.4 2.8 3.4 5.4"/>',
    link: '<path d="M9.5 14.5l5-5"/><path d="M10.5 6.8l1.1-1.1a3.7 3.7 0 0 1 5.2 5.2l-1.5 1.5"/><path d="M13.5 17.2l-1.1 1.1A3.7 3.7 0 0 1 7.2 13l1.5-1.5"/>',
    /* 状态与提示 */
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><circle cx="12" cy="7.8" r="1"/>',
    alert: '<path d="M12 4 3 20h18L12 4Z"/><path d="M12 10v5"/><circle cx="12" cy="17.6" r="1"/>',
    checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.6 2.6L16 9.5"/>',
    /* 收藏 / 词条 */
    star: '<path d="m12 4 2.5 5.2 5.5.8-4 3.9 1 5.6-5-2.9-5 2.9 1-5.6-4-3.9 5.5-.8L12 4Z"/>',
    /* 资源与 Provider */
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    cloud: '<path d="M7 18h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 10.5 3.5 3.5 0 0 0 7 18Z"/>',
    database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
    arrowRight: '<path d="M5 12h14"/><path d="m13 6 6 6-6 6"/>',
    external: '<path d="M14 4h6v6"/><path d="M20 4 11 13"/><path d="M18 14v5a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 19V7.5A1.5 1.5 0 0 1 5 6h5"/>',
    shield: '<path d="M12 3 5 6v6c0 4.2 2.9 7.7 7 9 4.1-1.3 7-4.8 7-9V6l-7-3Z"/>'
  };

  /**
   * 取得图标的 SVG 字符串。
   * @param {string} name 图标名
   * @param {number} [size=18] 边长（px）
   * @param {string} [cls] 附加 class
   * @returns {string} SVG 片段；未知图标返回空图形占位（不抛错）
   */
  function get(name, size, cls) {
    var body = P[name] || P.info;
    var px = size || 18;
    return '<svg class="icon ' + (cls || '') + '" viewBox="0 0 24 24" width="' + px +
      '" height="' + px + '" fill="none" stroke="currentColor" stroke-width="1.6" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
      body + '</svg>';
  }

  /** 收藏图标的实心 / 空心两态（颜色 + 形状双重表达，不依赖颜色单独表达状态） */
  function star(filled, size) {
    var px = size || 16;
    return '<svg class="icon" viewBox="0 0 24 24" width="' + px + '" height="' + px +
      '" fill="' + (filled ? 'currentColor' : 'none') + '" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
      P.star + '</svg>';
  }

  AIBAR.icons = { get: get, star: star, names: Object.keys(P) };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = AIBAR.icons;
  }
})(typeof window !== 'undefined' ? window : globalThis);
