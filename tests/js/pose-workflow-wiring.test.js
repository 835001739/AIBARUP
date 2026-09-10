/* 姿势工作流入口（M14 · SDXL + ControlNet-Union openpose）接线测试
   运行：node --test tests/js/

   之前 poses/ensure 与 poses/workflow 两条后端路由存在已久，但前端**完全没有调用点**
   —— 是死路由。本测试把「按钮 → JS → 后端路由 → Python 实现」整条链钉死：
     index.html #btn-wf-pose
       → pages.js refs.wfPoseBtn / openPoseWorkflowModal
         → GET  /api/comic/poses/assets           （素材下拉）
         → POST /api/comic/poses/build-workflow   （落盘，带 variant）
           → comic/pose.py ensure_pose_workflow(filename=, auto_fill=)

   任一处漏接都是静默失败（按钮点了没反应 / 生成出来还是锁脸版），静态断言最划算。
*/

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const STATIC = path.join(ROOT, 'static');

function readStatic(rel) {
  return fs.readFileSync(path.join(STATIC, rel), 'utf8');
}
function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const INDEX_HTML = readStatic('index.html');
const PAGES_JS = readStatic('js/pages.js');
const COMIC_ROUTES = read('comic/routes.py');
const POSE_PY = read('comic/pose.py');

/* ---------------------------------------------------------- 前端按钮存在 */

test('工作流面板工具栏有「生成姿势工作流」按钮', () => {
  assert.ok(
    /id="btn-wf-pose"/.test(INDEX_HTML),
    'index.html 缺少 #btn-wf-pose 按钮'
  );
  // 按钮必须在 workflows 面板内，不能挂错页面
  const panel = INDEX_HTML.slice(INDEX_HTML.indexOf('data-panel="workflows"'));
  const nextPanel = panel.indexOf('data-panel=', 10);
  const wfPanel = nextPanel > 0 ? panel.slice(0, nextPanel) : panel;
  assert.ok(wfPanel.includes('btn-wf-pose'), 'btn-wf-pose 必须位于 workflows 面板内');
});

test('pages.js 绑定了 #btn-wf-pose 的点击事件', () => {
  assert.ok(
    /refs\.wfPoseBtn\s*=\s*doc\.getElementById\('btn-wf-pose'\)/.test(PAGES_JS),
    'pages.js 未取到 btn-wf-pose 引用'
  );
  assert.ok(
    /refs\.wfPoseBtn\.addEventListener\('click',\s*openPoseWorkflowModal\)/.test(PAGES_JS),
    'btn-wf-pose 未绑定 openPoseWorkflowModal'
  );
});

/* ---------------------------------------------------------- 端点字面量 */

test('前端调用的两个端点与后端路由一一对应', () => {
  assert.ok(
    PAGES_JS.includes("'/api/comic/poses/assets'"),
    'pages.js 未调用 /api/comic/poses/assets'
  );
  assert.ok(
    PAGES_JS.includes("'/api/comic/poses/build-workflow'"),
    'pages.js 未调用 /api/comic/poses/build-workflow'
  );
  assert.ok(
    /@bp\.get\("\/poses\/assets"\)/.test(COMIC_ROUTES),
    'comic/routes.py 缺少 GET /poses/assets'
  );
  assert.ok(
    /@bp\.post\("\/poses\/build-workflow"\)/.test(COMIC_ROUTES),
    'comic/routes.py 缺少 POST /poses/build-workflow'
  );
});

/* ---------------------------------------------------------- variant 契约 */

test('前端只发送 consistent / openpose 两种 variant', () => {
  assert.ok(
    /variant:\s*form\.variant/.test(PAGES_JS),
    '提交体未带 variant'
  );
  assert.ok(
    /form\s*=\s*\{\s*variant:\s*'consistent'/.test(PAGES_JS),
    '默认变体必须是 consistent'
  );
  // 两个可选项都要出现，否则变体切换是假的
  assert.ok(/form\.variant\s*=\s*'consistent'/.test(PAGES_JS), '缺少切回 consistent 的分支');
  assert.ok(/form\.variant\s*=\s*'openpose'/.test(PAGES_JS), '缺少切到 openpose 的分支');
});

test('后端校验 variant 取值，非二者之一直接 400', () => {
  const fn = COMIC_ROUTES.slice(COMIC_ROUTES.indexOf('def poses_build_workflow('));
  assert.ok(
    /variant not in \("consistent", "openpose"\)/.test(fn),
    '后端未校验 variant 白名单'
  );
  assert.ok(/AIBARError\("invalid_input"/.test(fn), '非法 variant 应抛 invalid_input');
});

/* ---------------------------------------------------------- auto_fill 防回归 */

test('openpose 变体必须 auto_fill=False（否则会被补成 15 节点锁脸版）', () => {
  const fn = COMIC_ROUTES.slice(COMIC_ROUTES.indexOf('def poses_build_workflow('));
  const openposeBranch = fn.slice(fn.indexOf('if variant == "openpose"'));
  assert.ok(
    /auto_fill=False/.test(openposeBranch),
    'openpose 分支必须显式 auto_fill=False，否则自动扫描会把参考图补回来'
  );
  assert.ok(
    /POSE_OPENPOSE_FILENAME/.test(openposeBranch),
    'openpose 分支必须落到 POSE_OPENPOSE_FILENAME，不能覆盖锁脸版'
  );
  // 纯控姿分支绝不能给 reference_image 赋值（注意：注释里会提到这个词，只查赋值）
  const openposeBody = openposeBranch.slice(0, openposeBranch.indexOf('else:'));
  assert.ok(
    !/reference_image\s*=/.test(openposeBody),
    'openpose 分支不应给 reference_image 赋值'
  );
});

test('两个变体落盘到不同文件（互不覆盖）', () => {
  assert.ok(
    /POSE_WORKFLOW_FILENAME\s*=\s*"aibar_pose_consistent\.json"/.test(POSE_PY),
    '锁脸版文件名常量变了'
  );
  assert.ok(
    /POSE_OPENPOSE_FILENAME\s*=\s*"aibar_pose_openpose\.json"/.test(POSE_PY),
    '纯 openpose 版文件名常量变了'
  );
  assert.ok(
    /POSE_OPENPOSE_FILENAME\s*!=\s*(pose\.)?POSE_WORKFLOW_FILENAME/.test(read('tests/py/test_comic_pose.py')),
    'Python 侧应有断言两个文件名不同'
  );
});

/* ---------------------------------------------------------- modal 用法正确性 */

test('「生成」按钮用 close:false + return false（否则还没提交弹窗就关了）', () => {
  assert.ok(
    /variant: 'primary',\s*close: false,\s*onClick: submitPoseWorkflow/.test(PAGES_JS),
    '生成按钮必须 close:false，否则点击即关闭'
  );
  assert.ok(
    /return false; \/\/ 保持弹窗打开/.test(PAGES_JS),
    'onClick 必须返回 false 阻止自动关闭'
  );
});

test('生成成功后触发同步再刷新列表（否则列表里看不到新工作流）', () => {
  const fn = PAGES_JS.slice(PAGES_JS.indexOf('function submitPoseWorkflow('));
  const end = fn.indexOf('api().get(\'/api/comic/poses/assets\'');
  const body = end > 0 ? fn.slice(0, end) : fn;
  assert.ok(/\/api\/sync\/now/.test(body), '落盘后必须同步一次');
  assert.ok(/loadWorkflows\(1\)/.test(body), '同步后要刷新工作流列表');
});
