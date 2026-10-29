/* AI 视频工作台接线与纯函数契约测试
   运行：node --test tests/js/ai-video-wiring.test.js
*/

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const INDEX = read('static/index.html');
const APP = read('static/js/app.js');
const MODULE = read('static/js/ai-video.js');
const CSS = read('static/css/pages.css');
const aiVideo = require('../../static/js/ai-video.js');

test('ai-video.js 在 Node 环境安全加载并导出生命周期与纯函数', () => {
  ['init', 'onEnter', 'refresh', 'normalizeShotStatus', 'progressModel', 'filterShots'].forEach((name) => {
    assert.strictEqual(typeof aiVideo[name], 'function', '缺少导出：' + name);
  });
});

test('normalizeShotStatus 统一常见后端状态别名', () => {
  assert.strictEqual(aiVideo.normalizeShotStatus('done'), 'generated');
  assert.strictEqual(aiVideo.normalizeShotStatus('lip-syncing'), 'lip_sync');
  assert.strictEqual(aiVideo.normalizeShotStatus('passed'), 'approved');
  assert.strictEqual(aiVideo.normalizeShotStatus('unexpected'), 'pending');
});

test('progressModel 生成完整 L0-L5 阶段模型并限制百分比', () => {
  const model = aiVideo.progressModel({ current_stage: 'L3', progress_percent: 130 });
  assert.strictEqual(model.current, 'L3');
  assert.strictEqual(model.index, 3);
  assert.strictEqual(model.percent, 100);
  assert.deepStrictEqual(model.steps.map((step) => step.key), ['L0', 'L1', 'L2', 'L3', 'L4', 'L5']);
  assert.deepStrictEqual(model.steps.map((step) => step.state), ['done', 'done', 'done', 'active', 'todo', 'todo']);
});

test('filterShots 可组合状态与口型筛选且不修改输入', () => {
  const shots = [
    { id: 1, status: 'done', lip_sync_required: true },
    { id: 2, status: 'approved', lip_sync_required: false },
    { id: 3, status: 'approved', lipsync: 'yes' }
  ];
  const result = aiVideo.filterShots(shots, { status: 'approved', lipsync: 'yes' });
  assert.deepStrictEqual(result.map((shot) => shot.id), [3]);
  assert.strictEqual(shots.length, 3);
});

test('index.html 注册导航、面板 root 与正确脚本顺序', () => {
  assert.match(INDEX, /data-tab="ai-video"/);
  assert.match(INDEX, /data-panel="ai-video"/);
  assert.match(INDEX, /id="ai-video-root"/);
  const aiIndex = INDEX.indexOf('/static/js/ai-video.js');
  const appIndex = INDEX.indexOf('/static/js/app.js');
  assert.ok(aiIndex >= 0 && aiIndex < appIndex, 'ai-video.js 必须在 app.js 之前加载');
});

test('app.js 完成 PAGES / ORDER / onEnter / init 四处接线', () => {
  assert.match(APP, /'ai-video':\s*\{/);
  assert.match(APP, /'workflows',\s*'ai-video'/);
  assert.match(APP, /AIBAR\.aiVideo && AIBAR\.aiVideo\.onEnter/);
  assert.match(APP, /AIBAR\.aiVideo\.init\(\)/);
});

test('ai-video.js 覆盖约定 API 端点与方法', () => {
  [
    '/api/ai-video/meta',
    '/api/ai-video/projects',
    "'/dashboard'",
    "'/seed-template'",
    "'/scenes'",
    "'/shots'",
    '/api/ai-video/shots/',
    "'/assets'",
    '/api/ai-video/assets/'
  ].forEach((literal) => assert.ok(MODULE.includes(literal), '缺少端点字面量：' + literal));
  assert.match(MODULE, /api\(\)\.patch\('\/api\/ai-video\/projects\//);
  assert.match(MODULE, /api\(\)\.del\('\/api\/ai-video\/projects\//);
  assert.match(MODULE, /api\(\)\.patch\('\/api\/ai-video\/shots\//);
  assert.match(MODULE, /api\(\)\.del\('\/api\/ai-video\/assets\//);
});

test('工作台包含关键功能文案与响应式暗色样式', () => {
  ['真人写实短剧 28 镜模板', '资产层', '场景与镜头', '需要口型', '人脸一致', '口型质量', '首帧', '尾帧'].forEach((text) => {
    assert.ok(MODULE.includes(text), '缺少功能文案：' + text);
  });
  ['.ai-video-workbench {', '.ai-video-stages {', '.ai-video-assets {', '.ai-video-shot-card {', '.ai-video-editor {'].forEach((selector) => {
    assert.ok(CSS.includes(selector), '缺少样式：' + selector);
  });
  assert.match(CSS, /@media \(max-width: 760px\)/);
  assert.match(CSS, /var\(--color-bg-surface\)/);
});

test('资产图片/信息与工作流管理接线', () => {
  ['openAssetEditor', 'openWorkflowManager', 'startAssetPolling', 'quickGenerateAsset'].forEach((name) => {
    assert.ok(MODULE.includes(name), '缺少函数：' + name);
  });
  ['/api/ai-video/workflows', '/generate', 'asset.image_url', "api().patch('/api/ai-video/assets/"].forEach((literal) => {
    assert.ok(MODULE.includes(literal), '缺少资产/工作流端点：' + literal);
  });
  assert.ok(MODULE.includes('资产详情'), '缺少资产详情入口');
  assert.ok(MODULE.includes('出图工作流'), '缺少工作流管理入口');
  assert.ok(MODULE.includes('重新生成图片'), '缺少重新生成动作');
  ['.ai-video-asset-card {', '.ai-video-asset-cover {', '.ai-video-asset-editor-preview {', '.ai-video-workflow-row {'].forEach((selector) => {
    assert.ok(CSS.includes(selector), '缺少资产/工作流样式：' + selector);
  });
});

test('资产类型、状态与工作流选项使用中文标签', () => {
  assert.strictEqual(aiVideo.assetTypeLabel('character'), '角色');
  assert.strictEqual(aiVideo.assetTypeLabel('scene'), '场景');
  assert.strictEqual(aiVideo.assetStatusLabel('ready'), '已就绪');
  assert.strictEqual(aiVideo.assetStatusLabel('failed'), '生成失败');
  assert.deepStrictEqual(aiVideo.workflowChoices('')[0], ['', '沿用项目默认']);
  assert.strictEqual(aiVideo.workflowChoices('custom.json')[1][0], 'custom.json');
});

test('角色多图：角度组、表情组、封面与删除接线', () => {
  ['assetGalleryField', 'loadAssetGallery', 'startAssetSet'].forEach((name) => {
    assert.ok(MODULE.includes(name), '缺少函数：' + name);
  });
  ["'/generate-set'", '/images/', "'/cover'", '设为封面', '生成角度组', '生成表情组'].forEach((literal) => {
    assert.ok(MODULE.includes(literal), '缺少多图端点或文案：' + literal);
  });
  ['.ai-video-asset-gallery {', '.ai-video-asset-variant {'].forEach((selector) => {
    assert.ok(CSS.includes(selector), '缺少多图样式：' + selector);
  });
});

test('页面阶段由真实资产/镜头状态推导，而非只按批准率', () => {
  assert.strictEqual(aiVideo.deriveProjectStage({}, [], [], []), 0);
  assert.strictEqual(aiVideo.deriveProjectStage({}, [{ id: 1 }], [], [{ status: 'draft' }]), 1);
  assert.strictEqual(aiVideo.deriveProjectStage({}, [{ id: 1 }], [{ status: 'pending' }], [{ status: 'ready' }]), 2);
  assert.strictEqual(aiVideo.deriveProjectStage({}, [{ id: 1 }], [{ status: 'approved' }, { status: 'pending' }], [{ status: 'ready' }]), 3);
  assert.strictEqual(aiVideo.deriveProjectStage({}, [{ id: 1 }], [{ status: 'approved' }], [{ status: 'ready' }]), 4);
  assert.strictEqual(aiVideo.deriveProjectStage({}, [{ id: 1 }], [{ status: 'done' }], [{ status: 'ready' }]), 5);
});

test('镜头类型与精确状态使用中文标签，筛选不再合并状态', () => {
  assert.strictEqual(aiVideo.shotTypeLabel('reaction_cu'), '反应特写');
  assert.strictEqual(aiVideo.shotTypeLabel('ots'), '过肩镜头');
  assert.strictEqual(aiVideo.shotStatusLabel('frame_draft'), '首帧草稿');
  assert.strictEqual(aiVideo.shotStatusLabel('needs_human'), '需人工');
  const shots = [{ status: 'tts_ready' }, { status: 'frame_approved' }];
  assert.deepStrictEqual(
    aiVideo.filterShots(shots, { status: 'frame_approved', lipsync: 'all', exact: true }).length,
    1
  );
});

test('工作流选择过滤不可用于角色出图的条目', () => {
  assert.ok(MODULE.includes('character_ready'));
  assert.ok(MODULE.includes('workflow_reason'));
  assert.ok(MODULE.includes('is-disabled'));
  assert.ok(MODULE.includes('ai-video-workflow-badge'));
  assert.ok(CSS.includes('.ai-video-workflow-badge'));
});

test('缩略图点击查看全图与键盘关闭', () => {
  ['openAssetViewer', 'openAssetViewerForAsset', 'closeAssetViewer'].forEach((name) => {
    assert.ok(MODULE.includes(name), '缺少查看全图函数：' + name);
  });
  ['ai-video-lightbox', 'ai-video-viewer-caption', 'ArrowLeft', 'ArrowRight', 'Escape', 'ai-video-thumb-clickable'].forEach((literal) => {
    assert.ok(MODULE.includes(literal), '缺少全图交互：' + literal);
  });
  ['.ai-video-lightbox {', '.ai-video-viewer-caption {', '.ai-video-thumb-clickable {'].forEach((selector) => {
    assert.ok(CSS.includes(selector), '缺少全图样式：' + selector);
  });
});

test('后台刷新不会移除正在编辑的详情抽屉', () => {
  assert.ok(MODULE.includes("var openEditor = main.querySelector('.ai-video-editor')"));
  assert.ok(MODULE.includes('if (openEditor) main.appendChild(openEditor)'));
  assert.ok(MODULE.includes('String(state.projectId) !== String(projectId)'));
  assert.ok(MODULE.includes('closeEditors();'));
});

test('首尾帧可点击选择项目图片或上传', () => {
  ['openFramePicker', 'loadFrameCandidates'].forEach((name) => {
    assert.ok(MODULE.includes(name), '缺少首尾帧函数：' + name);
  });
  ['/frames/', '/upload', '设置首帧', '设置尾帧', '清除', '上传本地图片'].forEach((literal) => {
    assert.ok(MODULE.includes(literal), '缺少首尾帧交互：' + literal);
  });
  ['.ai-video-frame-picker {', '.ai-video-frame-candidate {', '.ai-video-frame-current {'].forEach((selector) => {
    assert.ok(CSS.includes(selector), '缺少首尾帧样式：' + selector);
  });
});

test('首尾帧可直接生成并轮询状态', () => {
  ['appendFrameGenerate', 'startFramePolling', 'frameStatus'].forEach((name) => {
    assert.ok(MODULE.includes(name), '缺少帧生成函数：' + name);
  });
  ['/generate', '正在生成', '开始生成', '首帧', '尾帧'].forEach((literal) => {
    assert.ok(MODULE.includes(literal), '缺少帧生成交互：' + literal);
  });
  assert.ok(MODULE.includes('frameTimers'));
  assert.ok(CSS.includes('.ai-video-frame.is-generating'));
  assert.ok(CSS.includes('.ai-video-frame-generate {'));
});
