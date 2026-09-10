/* 视频转绘模块（M16）接线与契约测试
   运行：node --test tests/js/

   视频转绘是「跨 6 个文件接线」的：index.html 的导航/面板/脚本顺序、app.js 的
   PAGES/ORDER/onEnter/init、videopaint.js 的导出与端点字面量、player.js 的通用播放器、
   ui.js 的 select/fieldRow/modal/confirm、pages.css 的专用类、以及 videopaint/routes.py 的后端路由。
   任一处漏接都是静默失败，用静态断言把接线钉死。
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
const VIDEOPAINT_JS = read('js/videopaint.js');
const ROUTES = fs.readFileSync(path.join(__dirname, '..', '..', 'videopaint', 'routes.py'), 'utf8');
const APP_PY = fs.readFileSync(path.join(__dirname, '..', '..', 'app.py'), 'utf8');

/* ---------------------------------------------------------- 模块导出契约 */

test('videopaint.js 在无 DOM 的 Node 环境下可安全加载并导出模块接口', () => {
  const vp = require('../../static/js/videopaint.js');
  ['init', 'onEnter', 'refresh', 'openDetail'].forEach((fn) => {
    assert.strictEqual(typeof vp[fn], 'function', '缺少导出：' + fn);
  });
});

test('player.js 可安全加载并导出通用播放器（连播生成序列复用）', () => {
  const player = require('../../static/js/player.js');
  assert.strictEqual(typeof player.open, 'function', 'player.js 必须导出 open()');
});

test('ui.js 导出共享的 select / fieldRow / modal / confirm', () => {
  const UI_JS = read('js/ui.js');
  ['select:', 'fieldRow:', 'modal:', 'confirm:'].forEach((sym) => {
    assert.match(UI_JS, new RegExp(sym + '\\s*\\w+'), 'ui.js 未导出 ' + sym);
  });
});

/* ---------------------------------------------------------- 外壳接线 */

test('index.html 注册了视频转绘导航项', () => {
  assert.match(INDEX_HTML, /data-tab="videopaint"/, '缺少 data-tab="videopaint" 导航项');
});

test('index.html 注册了视频转绘面板容器', () => {
  assert.match(INDEX_HTML, /data-panel="videopaint"/, '缺少 data-panel="videopaint" 面板');
  assert.match(INDEX_HTML, /id="vp-root"/, '缺少 #vp-root 容器');
});

test('脚本加载顺序：videopaint.js 在 app.js 之前，且 player.js / ui.js 在前', () => {
  const order = (INDEX_HTML.match(/<script src="\/static\/js\/(\w+)\.js"><\/script>/g) || [])
    .map((s) => s.replace(/.*js\//, '').replace(/\.js.*/, ''));
  assert.ok(order.indexOf('videopaint') < order.indexOf('app'),
    'videopaint.js 必须在 app.js 之前加载（否则 app boot 时 AIBAR.videopaint 未定义）');
  assert.ok(order.indexOf('ui') < order.indexOf('videopaint'),
    'ui.js 必须在 videopaint.js 之前（模态/下拉依赖 ui）');
  assert.ok(order.indexOf('player') < order.indexOf('videopaint'),
    'player.js 必须在 videopaint.js 之前（连播复用播放器）');
});

test('app.js 的 PAGES 与 ORDER 都覆盖了 videopaint', () => {
  assert.match(APP_JS, /videopaint:\s*\{/, 'PAGES 缺少 videopaint 条目');
  assert.match(APP_JS, /'video',\s*'videopaint'/, "ORDER 末尾应追加 'videopaint'");
});

test('app.js 的 onEnter 与 init 都派发给 AIBAR.videopaint', () => {
  assert.match(APP_JS, /AIBAR\.videopaint && AIBAR\.videopaint\.onEnter/,
    'onEnter 未派发给 AIBAR.videopaint');
  assert.match(APP_JS, /AIBAR\.videopaint\.init\(\)/,
    'init 未调用 AIBAR.videopaint.init()');
});

/* ---------------------------------------------------------- 后端契约 */

test('后端注册了视频转绘的全部路由', () => {
  [
    '/clips',
    '/jobs',
    '/jobs/<int:job_id>',
    "/jobs/<int:job_id>/prepare",
    "/jobs/<int:job_id>/pose",
    "/jobs/<int:job_id>/generate",
    "/jobs/<int:job_id>/nobg",
    "/jobs/<int:job_id>/sheet",
    "/jobs/<int:job_id>/frames",
    "/jobs/<int:job_id>/sheet/<variant>",
    "/jobs/<int:job_id>/file/<kind>/<int:order_idx>",
    "/jobs/<int:job_id>/frames/<int:order_idx>/workflow-graph",
    "/jobs/<int:job_id>/frames/<int:order_idx>/editor-link",
    "/jobs/<int:job_id>/frames/<int:order_idx>/flux2-workflow-graph",
    "/jobs/<int:job_id>/frames/<int:order_idx>/flux2-editor-link",
    '/reference-upload'
  ].forEach((rule) => {
    assert.ok(ROUTES.includes('"' + rule + '"'), '缺少路由：' + rule);
  });
});

test('前端用到的每个 /api/videopaint 端点后端都有对应路由', () => {
  // 前端用动态 stage 拼接端点（runStage）：'/api/videopaint/jobs/' + job.id + '/' + stage
  assert.ok(VIDEOPAINT_JS.includes("'/api/videopaint/jobs/' + job.id + '/' + stage"),
    '前端应动态拼接 stage 端点（runStage 的 endpoint 构造）');
  const stages = ['prepare', 'pose', 'generate', 'nobg', 'sheet'];
  const routeFor = {
    prepare: "/jobs/<int:job_id>/prepare",
    pose: "/jobs/<int:job_id>/pose",
    generate: "/jobs/<int:job_id>/generate",
    nobg: "/jobs/<int:job_id>/nobg",
    sheet: "/jobs/<int:job_id>/sheet",
  };
  stages.forEach((st) => {
    // 每个 stage 取值确实被使用（stageBtn(job, 'prepare', ...)）
    assert.ok(VIDEOPAINT_JS.includes("stageBtn(job, '" + st + "'"),
      '前端未触发 stage：' + st);
    assert.ok(ROUTES.includes('"' + routeFor[st] + '"'),
      '后端缺少路由：' + routeFor[st] + '（前端 stage=' + st + ' 用得到）');
  });
  assert.ok(VIDEOPAINT_JS.includes('/api/videopaint/jobs'), '前端未调用 /api/videopaint/jobs');
  assert.ok(VIDEOPAINT_JS.includes('/api/videopaint/clips'), '前端未调用 /api/videopaint/clips');
  assert.ok(VIDEOPAINT_JS.includes('/api/videopaint/reference-upload'), '前端未调用 /api/videopaint/reference-upload');
});

test('app.py 注册了 videopaint 蓝图', () => {
  assert.match(APP_PY, /from videopaint\.routes import bp as videopaint_bp/,
    'app.py 未导入 videopaint 蓝图');
  assert.match(APP_PY, /app\.register_blueprint\(videopaint_bp\)/,
    'app.py 未注册 videopaint 蓝图');
});

/* ---------------------------------------------------------- 复用播放器 / 跳转 */

test('连播生成序列复用通用播放器（AIBAR.player.open）', () => {
  assert.ok(VIDEOPAINT_JS.includes('AIBAR.player.open('),
    'playGenerated 应调用 AIBAR.player.open()');
  // 拼 items 时筛掉没有 image_path_url 的帧
  assert.ok(VIDEOPAINT_JS.includes('f.image_path_url'), '应按 image_path_url 筛选可连播帧');
});

test('查看组图复用 app 导航跳转', () => {
  assert.ok(VIDEOPAINT_JS.includes("AIBAR.app.navigate('groups')"),
    'gotoGroups 应调用 AIBAR.app.navigate(\'groups\')');
});

test('工作流按钮：前端调用 editor-link 路由并新开 ComfyUI', () => {
  // 逐帧卡片按钮 + 详情操作区按钮都走 openFrameWorkflow
  assert.ok(VIDEOPAINT_JS.includes('openFrameWorkflow('),
    '应定义 openFrameWorkflow 并接线到按钮');
  assert.ok(VIDEOPAINT_JS.includes("'/api/videopaint/jobs/' + jobId + '/frames/' + orderIdx + '/editor-link'"),
    'openFrameWorkflow 应请求 editor-link 路由');
  assert.ok(VIDEOPAINT_JS.includes('window.open(r.url'),
    '拿到链接后应 window.open 新开标签页');
  // 按钮文案「工作流」与图标 'workflow'
  assert.ok(VIDEOPAINT_JS.includes("'工作流'"), '按钮文案应为“工作流”');
  assert.ok(VIDEOPAINT_JS.includes("'workflow'"), '应使用 workflow 图标');
  // 后端 editor-link 路由返回 mode=graph 的 dict
  assert.ok(ROUTES.includes('def frame_editor_link('), 'routes.py 应定义 frame_editor_link');
  assert.ok(ROUTES.includes('service.frame_editor_link('), 'editor-link 路由应调用 service.frame_editor_link');
});

test('M17 · FLUX.2 工作流按钮与姿态工作流按钮并列', () => {
  // 前端：帧卡片上两个按钮并排，详情操作区两个入口并排
  assert.ok(VIDEOPAINT_JS.includes('frameFlux2Btn(f)'),
    '帧卡片 actions 应挂 frameFlux2Btn（与 frameWorkflowBtn 并列）');
  assert.ok(VIDEOPAINT_JS.includes('openFrameFlux2Workflow('),
    '应定义 openFrameFlux2Workflow 并接线到按钮');
  assert.ok(VIDEOPAINT_JS.includes("'/api/videopaint/jobs/' + jobId + '/frames/' + orderIdx + '/flux2-editor-link'"),
    'openFrameFlux2Workflow 应请求 flux2-editor-link 路由');
  assert.ok(VIDEOPAINT_JS.includes("'FLUX.2'"), '按钮文案应为“FLUX.2”');
  assert.ok(VIDEOPAINT_JS.includes("'sparkles'"), 'FLUX.2 按钮应使用 sparkles 图标');
  // 后端：两个新路由 + 两个 service 函数
  assert.ok(ROUTES.includes('def frame_flux2_workflow_graph('), 'routes.py 应定义 frame_flux2_workflow_graph');
  assert.ok(ROUTES.includes('def frame_flux2_editor_link('), 'routes.py 应定义 frame_flux2_editor_link');
  assert.ok(ROUTES.includes('service.build_frame_flux2_ui_graph('), 'graph 路由应调用 service.build_frame_flux2_ui_graph');
  assert.ok(ROUTES.includes('service.frame_flux2_editor_link('), 'editor-link 路由应调用 service.frame_flux2_editor_link');
});

test('FLUX.2 深链接 target 指向正向提示词节点 4', () => {
  const SVC = fs.readFileSync(path.join(__dirname, '..', '..', 'videopaint', 'service.py'), 'utf8');
  const seg = SVC.slice(SVC.indexOf('def frame_flux2_editor_link('));
  assert.ok(seg.includes('target="4"'),
    'build_flux2_workflow 中 CLIPTextEncode 正向节点 id 是 4，深链接 target 必须同步');
  assert.ok(seg.includes('flux2-workflow-graph'),
    'editor-link 的 graph_url 必须指向 flux2-workflow-graph 路由');
});

test('删除走 ui.confirm 二次确认', () => {
  assert.ok(VIDEOPAINT_JS.includes('ui().confirm('), 'doDelete 应调用 ui().confirm');
  assert.ok(VIDEOPAINT_JS.includes("danger: true"), '删除确认应为危险操作');
});

/* ---------------------------------------------------------- 样式 */

test('视频转绘专用样式齐全（缺一个容器/按钮就裸奔）', () => {
  [
    '.vp-detail-head', '.vp-detail-title', '.vp-config', '.vp-ref-img',
    '.vp-pipeline', '.vp-stage', '.vp-stage-label', '.vp-stage-desc',
    '.vp-actions', '.vp-frames-title', '.vp-frames', '.vp-frame-card',
    '.vp-frame-head', '.vp-frame-idx', '.vp-frame-imgs',
    '.vp-thumb', '.vp-thumb-img', '.vp-thumb-empty', '.vp-thumb-label',
    '.vp-status-row', '.vp-frame-actions'
  ].forEach((cls) => {
    assert.ok(PAGES_CSS.includes(cls + ' {'), '缺少样式：' + cls);
  });
});

test('复用公共类（form-grid-2 / card-grid / badge 修饰类）已存在', () => {
  assert.ok(PAGES_CSS.includes('.form-grid-2 {'), '缺少 .form-grid-2');
  assert.ok(PAGES_CSS.includes('.card-grid {'), '缺少 .card-grid');
  const COMPONENTS = read('css/components.css');
  ['badge-success', 'badge-warning', 'badge-error'].forEach((cls) => {
    assert.ok(COMPONENTS.includes('.' + cls), 'components.css 缺少 .' + cls);
  });
  // videopaint.js 只拼真实存在的 badge 修饰类
  const codeOnly = VIDEOPAINT_JS.split('\n')
    .filter((l) => !/^\s*\/?\*|\s*\/\//.test(l)).join('\n');
  const appended = [...codeOnly.matchAll(/cls \+= ' (badge-[\w-]+)'/g)].map((m) => m[1]);
  assert.ok(appended.length > 0, 'badge() 应至少拼一个修饰类');
  appended.forEach((cls) => {
    assert.ok(COMPONENTS.includes('.' + cls), '拼了不存在的徽标类：' + cls);
  });
});
