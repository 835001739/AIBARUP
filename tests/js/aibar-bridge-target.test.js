/* ComfyUI 桥梁扩展：aibar_target 的解析与 widget 定位。
 *
 * 桥梁是 ES module，且 import 的是 ComfyUI 运行时的 "/scripts/app.js"，
 * Node 无法直接 require。这里把源文件读进来、去掉 import 后整体求值，
 * 只取出纯函数来测——解析逻辑一旦改动，AIBAR 传过来的 target 就接不上了。
 */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert');

const SRC = path.join(__dirname, '../../comfyui_bridge/AIBAR-Bridge/js/aibar_bridge.js');

function loadBridge(fakeApp) {
  const source = fs.readFileSync(SRC, 'utf8');
  // 去掉 ES module import，并注入一个空实现让后续顶层代码安全执行。
  // 顶层那句 `const app = ...` 一并替换成可注入的假 app，
  // 这样 canvasSignature 这类依赖画布的函数也能测。
  // 注意：假 app 千万别带 registerExtension，否则模块尾部会真的去注册扩展、
  // 触发 run()，而 Node 里没有 window.location。
  const patched = source
    .replace(/^import\s+[^;]+;/m, 'const appModule = null;')
    .replace(/^const app = appModule[^\n]*$/m, 'const app = globalThis.__AIBAR_TEST_APP__ || null;');
  globalThis.__AIBAR_TEST_APP__ = fakeApp || null;
  const factory = new Function(
    `${patched}\nreturn { PARAM, parseTarget, pickWidget, textWidget,` +
      ` canvasSignature, graphSignature, isConfiguring, nodeType };`
  );
  // 加载时会因找不到 app 打一条 warn，测试里静音
  const originalWarn = console.warn;
  console.warn = () => {};
  try {
    return factory();
  } finally {
    console.warn = originalWarn;
  }
}

const bridge = loadBridge();

test('PARAM：与 AIBAR 后端约定一致的参数名', () => {
  assert.deepStrictEqual(bridge.PARAM, {
    graph: 'aibar_wf',
    prompt: 'aibar_prompt',
    negative: 'aibar_neg',
    target: 'aibar_target',
    name: 'aibar_name',
  });
});

test('parseTarget：纯节点 id（普通画布节点）', () => {
  assert.deepStrictEqual(bridge.parseTarget('12'), { id: '12' });
  assert.deepStrictEqual(bridge.parseTarget(12), { id: '12' });
});

test('parseTarget：节点 id + widget 名（子图提升出来的输入框）', () => {
  assert.deepStrictEqual(bridge.parseTarget('68@text'), { id: '68', widget: 'text' });
  assert.deepStrictEqual(bridge.parseTarget('68@prompt_text'), { id: '68', widget: 'prompt_text' });
});

test('parseTarget：节点 id + widget 下标（值只在 widgets_values 里）', () => {
  const spec = bridge.parseTarget('105#0');
  assert.deepStrictEqual(spec, { id: '105', index: 0 });
  assert.strictEqual(typeof spec.index, 'number');
});

test('parseTarget：空值一律返回 null', () => {
  [null, undefined, ''].forEach((value) => {
    assert.strictEqual(bridge.parseTarget(value), null);
  });
});

test('parseTarget：节点 id 里含 @ 或 # 时按最右侧切分', () => {
  // 贪婪匹配 id、非贪婪匹配后缀，避免把 "a@b@text" 切成 id="a@b@text"
  assert.deepStrictEqual(bridge.parseTarget('a@b@text'), { id: 'a@b', widget: 'text' });
});

test('pickWidget：按名字找，找不到时退回名为 text 的 widget', () => {
  const node = {
    widgets: [
      { name: 'seed', value: 42 },
      { name: 'text', value: '原始提示词' },
    ],
  };
  assert.strictEqual(bridge.pickWidget(node, { widget: 'text' }).value, '原始提示词');
  assert.strictEqual(bridge.pickWidget(node, { widget: 'nonexistent' }).value, '原始提示词');
  assert.strictEqual(bridge.pickWidget(node, null).value, '原始提示词');
});

test('pickWidget：按下标找，且只认字符串值', () => {
  const node = {
    widgets: [
      { name: 'prompt', value: '下标零的提示词' },
      { name: 'width', value: 1344 },
    ],
  };
  assert.strictEqual(bridge.pickWidget(node, { index: 0 }).value, '下标零的提示词');
  // 下标 1 是数字，不是文本 widget，应退回名为 text 的 widget（这里没有）
  assert.strictEqual(bridge.pickWidget(node, { index: 1 }), null);
});

test('pickWidget：节点没有 widgets 时不炸', () => {
  assert.strictEqual(bridge.pickWidget({}, { widget: 'text' }), null);
  assert.strictEqual(bridge.pickWidget(null, { index: 0 }), null);
});

test('textWidget：只认名为 text 且值为字符串的 widget', () => {
  const node = { widgets: [{ name: 'text', value: 'ok' }] };
  assert.strictEqual(bridge.textWidget(node).value, 'ok');
  assert.strictEqual(bridge.textWidget({ widgets: [{ name: 'text', value: 123 }] }), null);
  assert.strictEqual(bridge.textWidget({ widgets: [] }), null);
});

/* ------------------------------------------------------------------ 指纹 */

function canvasOf(nodes) {
  return loadBridge({ graph: { _nodes: nodes } });
}

test('canvasSignature 与 graphSignature 必须同算法', () => {
  // 载入校验靠的就是"两个指纹相等"。一旦两边算法漂移，
  // loadGraphVerified 会永远判定失败并重试到底，所以这条是硬约束。
  const uiGraph = {
    nodes: [
      { type: 'KSampler' },
      { type: 'CLIPTextEncode' },
      { type: 'VAEDecode' },
    ],
  };
  const b = canvasOf(uiGraph.nodes);
  assert.strictEqual(b.canvasSignature(), b.graphSignature(uiGraph));
});

test('canvasSignature：与节点顺序无关（排序后比较）', () => {
  const a = canvasOf([{ type: 'A' }, { type: 'B' }]);
  const b = canvasOf([{ type: 'B' }, { type: 'A' }]);
  assert.strictEqual(a.canvasSignature(), b.canvasSignature());
});

test('canvasSignature：节点数或类型变了指纹就变', () => {
  const two = canvasOf([{ type: 'A' }, { type: 'B' }]).canvasSignature();
  const three = canvasOf([{ type: 'A' }, { type: 'B' }, { type: 'C' }]).canvasSignature();
  const swapped = canvasOf([{ type: 'A' }, { type: 'C' }]).canvasSignature();
  assert.notStrictEqual(two, three);
  assert.notStrictEqual(two, swapped);
});

test('canvasSignature：空画布与缺省 app 都不炸', () => {
  assert.strictEqual(canvasOf([]).canvasSignature(), '0:');
  assert.strictEqual(bridge.canvasSignature(), '0:');
});

test('graphSignature：API 格式图（没有 nodes）不会误判为成功', () => {
  // API 图无法比对指纹，loadGraphVerified 会退化为"节点数 > 0"的判断
  assert.strictEqual(bridge.graphSignature({ '1': { class_type: 'KSampler' } }), '0:');
  assert.strictEqual(bridge.graphSignature(null), '0:');
});

test('isConfiguring：读 ComfyUI 的 configuringGraphLevel', () => {
  assert.strictEqual(canvasOf([{ type: 'A' }]).isConfiguring(), false);
  const busy = loadBridge({ graph: { _nodes: [] }, configuringGraphLevel: 2 });
  assert.strictEqual(busy.isConfiguring(), true);
});
