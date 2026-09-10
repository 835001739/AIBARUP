/* 视频转帧模块（M15）接线与契约测试
   运行：node --test tests/js/

   和组图一样，视频模块是「跨 5 个文件接线」的：index.html 的导航/面板/脚本顺序、
   app.js 的 PAGES/ORDER/onEnter/init、video.js 的导出、player.js 的通用播放器、
   pages.css 的专用类。少接任何一处都是**静默失败**——页面能起来但 tab 点不动
   或样式全裸，必须跑起服务才看得见。这里用静态断言把接线钉死。
*/

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..', 'static');

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const INDEX_HTML = read('index.html');
const APP_JS = read('js/app.js');
const PAGES_CSS = read('css/pages.css');
const GROUPS_JS = read('js/groups.js');

/* ---------------------------------------------------------- 模块导出契约 */

test('video.js 在无 DOM 的 Node 环境下可安全加载并导出模块接口', () => {
  const video = require('../../static/js/video.js');
  ['init', 'onEnter', 'refresh', 'openPlayer'].forEach((fn) => {
    assert.strictEqual(typeof video[fn], 'function', '缺少导出：' + fn);
  });
});

test('player.js 可安全加载并导出通用播放器', () => {
  const player = require('../../static/js/player.js');
  assert.strictEqual(typeof player.open, 'function', 'player.js 必须导出 open()');
});

test('ui.js 导出共享的 select / checkbox（播放器依赖它们）', () => {
  const uiMod = require('../../static/js/ui.js');
  // ui.js 在 Node 下导出的是格式化工具，AIBAR.ui 才挂 el/modal/select/checkbox。
  // 这里只校验源码里确实把这两个助手加进了导出对象（浏览器侧契约）。
  const UI_JS = read('js/ui.js');
  assert.match(UI_JS, /select:\s*select/, 'ui.js 未导出 select');
  assert.match(UI_JS, /checkbox:\s*checkbox/, 'ui.js 未导出 checkbox');
  assert.ok(uiMod, 'ui.js 应可被 require');
});

/* ---------------------------------------------------------- 外壳接线 */

test('index.html 注册了视频导航项', () => {
  assert.match(INDEX_HTML, /data-tab="video"/, '缺少 data-tab="video" 导航项');
});

test('index.html 注册了视频面板容器', () => {
  assert.match(INDEX_HTML, /data-panel="video"/, '缺少 data-panel="video" 面板');
  assert.match(INDEX_HTML, /id="video-root"/, '缺少 #video-root 容器');
});

test('脚本加载顺序：player.js 在 groups.js 之前，video.js 在 app.js 之前', () => {
  const order = (INDEX_HTML.match(/<script src="\/static\/js\/(\w+)\.js"><\/script>/g) || [])
    .map((s) => s.replace(/.*js\//, '').replace(/\.js.*/, ''));
  assert.ok(order.indexOf('player') < order.indexOf('groups'),
    'player.js 必须在 groups.js 之前加载（否则 groups 调不到 AIBAR.player）');
  assert.ok(order.indexOf('video') < order.indexOf('app'),
    'video.js 必须在 app.js 之前加载（否则 app boot 时 AIBAR.video 未定义）');
  assert.ok(order.indexOf('ui') < order.indexOf('player'),
    'ui.js 必须在 player.js 之前（播放器依赖 ui().el/modal）');
});

test('app.js 的 PAGES 与 ORDER 都覆盖了 video', () => {
  assert.match(APP_JS, /video:\s*\{/, 'PAGES 缺少 video 条目');
  assert.match(APP_JS, /'groups',\s*'video'/, "ORDER 末尾应追加 'video'");
});

test('app.js 的 onEnter 与 init 都派发给 AIBAR.video', () => {
  assert.match(APP_JS, /AIBAR\.video && AIBAR\.video\.onEnter/,
    'onEnter 未派发给 AIBAR.video（切到视频 tab 不会刷新）');
  assert.match(APP_JS, /AIBAR\.video\.init\(\)/,
    'init 未调用 AIBAR.video.init()（面板不会渲染）');
});

/* ---------------------------------------------------------- 播放器复用 */

test('groups.js 已改为调用共享播放器，不再自带 buildPlayer', () => {
  assert.match(GROUPS_JS, /AIBAR\.player\.open\(/,
    'groups.js 应调用 AIBAR.player.open() 而不是自带播放器');
  assert.doesNotMatch(GROUPS_JS, /function buildPlayer\(/,
    'buildPlayer 已成死代码，应删除（否则两份播放器会各自漂移）');
});

/* ---------------------------------------------------------- 样式 */

test('视频模块专用样式齐全（缺一个卡片或按钮就裸奔）', () => {
  [
    '.card-grid--video', '.video-card', '.video-cover', '.video-cover-img',
    '.video-cover-empty', '.video-actions', '.video-error', '.video-status-row'
  ].forEach((cls) => {
    assert.ok(PAGES_CSS.includes(cls + ' {') || PAGES_CSS.includes(cls + ' {'),
      '缺少样式：' + cls);
  });
});

test('视频卡片只拼真实存在的徽标修饰类', () => {
  // video.js 拼的是 badge-success / badge-warning / badge-error。
  // 拼一个不存在的 badge-muted 是无效类名，样式会静默掉底。
  const COMPONENTS = read('css/components.css');
  ['badge-success', 'badge-warning', 'badge-error'].forEach((cls) => {
    assert.ok(COMPONENTS.includes('.' + cls), 'components.css 缺少 .' + cls);
  });

  const VIDEO_JS = read('js/video.js');
  // 只检查**代码**：注释里提到 badge-muted 是为了说明坑，不该被当成违规
  const codeOnly = VIDEO_JS.split('\n').filter((l) => !/^\s*\/?\*|\s*\/\//.test(l)).join('\n');
  const appended = [...codeOnly.matchAll(/cls \+= ' (badge-[\w-]+)'/g)].map((m) => m[1]);
  assert.ok(appended.length > 0, 'badge() 应至少拼一个修饰类');
  appended.forEach((cls) => {
    assert.ok(COMPONENTS.includes('.' + cls), '拼了不存在的徽标类：' + cls);
  });
});

/* ---------------------------------------------------------- 后端契约 */

test('后端注册了视频模块的全部路由', () => {
  const routes = fs.readFileSync(
    path.join(__dirname, '..', '..', 'video', 'routes.py'), 'utf8');
  [
    '/clips',
    '/clips/import-path',
    '/clips/<int:clip_id>',
    '/clips/<int:clip_id>/extract',
    '/clips/<int:clip_id>/gif',
    '/clips/<int:clip_id>/frames',
    '/clips/<int:clip_id>/frame/<path:filename>',
    '/clips/<int:clip_id>/remove-bg',
    '/clips/<int:clip_id>/clear-bg',
    '/clips/<int:clip_id>/sheet'
  ].forEach((rule) => {
    assert.ok(routes.includes('"' + rule + '"'), '缺少路由：' + rule);
  });
});

test('前端用到的每个 /api/video 端点后端都有对应路由', () => {
  const VIDEO_JS = read('js/video.js');
  const routes = fs.readFileSync(
    path.join(__dirname, '..', '..', 'video', 'routes.py'), 'utf8');

  // 前端 URL 多是拼接的（'/api/video/clips/' + id + '/extract'），
  // 直接提取完整 URL 会被引号截断。这里按「实际出现的字面量」校验：
  // - 拼接型端点：只出现后缀 `'/<name>'`
  // - 静态端点（import-path）：完整字面量
  const literals = {
    "'/extract'": '/clips/<int:clip_id>/extract',
    "'/gif'": '/clips/<int:clip_id>/gif',
    "'/frames'": '/clips/<int:clip_id>/frames',
    '/api/video/clips/import-path': '/clips/import-path'
  };
  Object.keys(literals).forEach((lit) => {
    assert.ok(VIDEO_JS.includes(lit), '前端未调用端点：' + lit);
    assert.ok(routes.includes('"' + literals[lit] + '"'),
      '后端缺少路由：' + literals[lit] + '（前端 ' + lit + ' 用得到）');
  });
  assert.ok(VIDEO_JS.includes('/api/video/clips'), '前端未调用 /api/video/clips');
});

test('app.py 注册了 video 蓝图', () => {
  const app = fs.readFileSync(path.join(__dirname, '..', '..', 'app.py'), 'utf8');
  assert.match(app, /from video\.routes import bp as video_bp/, 'app.py 未导入 video 蓝图');
  assert.match(app, /app\.register_blueprint\(video_bp\)/, 'app.py 未注册 video 蓝图');
});

/* ---------------------------------------------------------- 去背景 / 拼图 接线 */

test('去背景/拼图：前端字面量端点后端都有对应路由', () => {
  const VIDEO_JS = read('js/video.js');
  const routes = fs.readFileSync(
    path.join(__dirname, '..', '..', 'video', 'routes.py'), 'utf8');

  // 前端是拼接型端点（'/api/video/clips/' + id + '/remove-bg'），
  // 这里按出现的后缀字面量校验，同时钉死后端路由存在。
  const literals = {
    "'/remove-bg'": '/clips/<int:clip_id>/remove-bg',
    "'/clear-bg'": '/clips/<int:clip_id>/clear-bg',
    "'/sheet'": '/clips/<int:clip_id>/sheet'
  };
  Object.keys(literals).forEach((lit) => {
    assert.ok(VIDEO_JS.includes(lit), '前端未调用端点：' + lit);
    assert.ok(routes.includes('"' + literals[lit] + '"'),
      '后端缺少路由：' + literals[lit] + '（前端 ' + lit + ' 用得到）');
  });
});

test('去背景/拼图专用样式齐全（透明封面 + 变体切换 + 取色 + 图片取色器）', () => {
  [
    '.video-cover--checker', '.video-variant', '.video-variant-btn',
    '.video-color-row', '.video-color-input',
    '.video-picker-section', '.video-picker-wrap', '.video-picker-img',
    '.video-picker-crosshair', '.video-pick-result', '.video-pick-swatch',
    '.video-pick-targets', '.video-pick-chip',
    '.field-row', '.field-label', '.field-hint', '.field-value'
  ].forEach((cls) => {
    assert.ok(PAGES_CSS.includes(cls + ' {'),
      '缺少样式：' + cls);
  });
});

test('ui.js 导出 fieldRow（表单行标签）', () => {
  const UI_JS = read('js/ui.js');
  // Node 下 module.exports 不含 AIBAR.ui 全量，只校验源码里确实导出了
  assert.match(UI_JS, /fieldRow:\s*fieldRow/, 'ui.js 未导出 fieldRow');
});

test('图片取色器：前端引用了首帧 URL + canvas 取色逻辑', () => {
  const VIDEO_JS = read('js/video.js');
  // 必须有 frame_0001 引用（图片取色器加载首帧）
  assert.ok(VIDEO_JS.includes('frame_0001'), '缺少首帧加载（frame_0001）');
  // 必须有 getImageData / canvas 像素读取
  assert.ok(VIDEO_JS.includes('getImageData'), '缺少 getImageData 像素读取');
  // 必须有 crosshair 十字光标
  assert.ok(VIDEO_JS.includes('video-picker-crosshair'), '缺少十字光标元素');
});

test('双色抠图：前端有开关 + 第二把钥匙 + 取色目标切换', () => {
  const VIDEO_JS = read('js/video.js');
  assert.ok(VIDEO_JS.includes('双色抠图'), '缺少「双色抠图」开关文案');
  assert.ok(VIDEO_JS.includes('color2'), '提交 payload 未带 color2');
  assert.ok(VIDEO_JS.includes('取色到 色2'), '缺少「取色到 色2」目标切换');
  assert.ok(VIDEO_JS.includes('video-pick-chip'), '缺少取色目标 chip 元素');
  // 取色器默认只填色1，色2 仅在双色模式下参与提交
  assert.ok(VIDEO_JS.includes('dualMode && hex2Input'), '双色模式未控制 color2 提交');
});

test('双色抠图专用样式齐全（取色目标 chip）', () => {
  [
    '.video-pick-targets', '.video-pick-chip'
  ].forEach((cls) => {
    assert.ok(PAGES_CSS.includes(cls + ' {'), '缺少样式：' + cls);
  });
});

test('连通抠图：前端有模式选择 + 种子点 + 提交 mode/seed', () => {
  const VIDEO_JS = read('js/video.js');
  assert.ok(VIDEO_JS.includes('连通抠图'), '缺少「连通抠图」模式文案');
  assert.ok(VIDEO_JS.includes("value: 'flood'"), '模式选择缺少 flood 选项');
  assert.ok(VIDEO_JS.includes('种子点'), '缺少「种子点」展示');
  assert.ok(VIDEO_JS.includes('重置为左上角'), '缺少种子点重置按钮');
  // 连通模式下点击图片要把坐标写入种子点
  assert.ok(VIDEO_JS.includes('seedX = Math.max(0, Math.round(x))'),
    '点击图片未更新种子点坐标');
  // 提交 payload 必须带 mode 与 seed
  assert.ok(VIDEO_JS.includes("payload.mode = 'flood'"), '提交未带 mode=flood');
  assert.ok(VIDEO_JS.includes('payload.seed = seedX'), '提交未带 seed 坐标');
});

test('连通抠图：连通模式强制单色、隐藏双色与颜色行', () => {
  const VIDEO_JS = read('js/video.js');
  // applyModeUI 在 flood 下把双色关掉、隐藏色①与色②
  assert.ok(VIDEO_JS.includes('modeSelect.value === \'flood\''), '缺少 flood 判断');
  assert.ok(VIDEO_JS.includes('color1Field.style.display = \'none\''),
    '连通模式未隐藏色①行');
  assert.ok(VIDEO_JS.includes('dualMode = false'), '连通模式未强制单色');
});
