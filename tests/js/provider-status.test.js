/* Provider 状态中文原因 + 恢复操作映射（PRD M10.3 / M10.4） */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const {
  formatProviderStatus,
  qualityTierLabel,
  STATUS_MAP,
  STAGE_LABELS
} = require('../../static/js/reverse.js');

const EXPECTED_STATUS = [
  'ready', 'unconfigured', 'offline', 'missing_node', 'missing_model',
  'missing_mmproj', 'incompatible_runtime', 'out_of_memory', 'unauthorized', 'error'
];

test('formatProviderStatus：覆盖后端十种状态枚举', () => {
  assert.deepStrictEqual(Object.keys(STATUS_MAP).sort(), EXPECTED_STATUS.slice().sort());
  EXPECTED_STATUS.forEach((key) => {
    const status = formatProviderStatus(key);
    assert.strictEqual(status.status, key);
    assert.ok(status.label, key + ' 需要中文状态名');
    assert.ok(status.hint, key + ' 需要中文原因说明');
    assert.ok(status.action, key + ' 需要恢复动作');
    assert.ok(['success', 'warning', 'error', 'neutral'].indexOf(status.tone) !== -1, key + ' 的 tone 非法');
  });
});

test('formatProviderStatus：可用状态不需要操作', () => {
  const status = formatProviderStatus('ready');
  assert.strictEqual(status.label, '可用');
  assert.strictEqual(status.tone, 'success');
  assert.strictEqual(status.action, 'none');
  assert.strictEqual(status.actionLabel, '');
});

test('formatProviderStatus：可恢复状态给出对应操作', () => {
  assert.strictEqual(formatProviderStatus('offline').action, 'start_comfyui');
  assert.strictEqual(formatProviderStatus('offline').actionLabel, '启动 ComfyUI');
  assert.strictEqual(formatProviderStatus('unauthorized').action, 'consent');
  assert.strictEqual(formatProviderStatus('unauthorized').tone, 'warning');
  assert.strictEqual(formatProviderStatus('out_of_memory').action, 'retry');
  assert.strictEqual(formatProviderStatus('unconfigured').action, 'configure');
  assert.strictEqual(formatProviderStatus('missing_node').action, 'check_nodes');
  assert.strictEqual(formatProviderStatus('missing_model').action, 'check_nodes');
  assert.strictEqual(formatProviderStatus('missing_mmproj').action, 'check_nodes');
  assert.strictEqual(formatProviderStatus('incompatible_runtime').action, 'configure');
  assert.strictEqual(formatProviderStatus('error').action, 'refresh');
});

test('formatProviderStatus：未知状态退回「未知状态 + 重新检测」', () => {
  const unknown = formatProviderStatus('some_new_status');
  assert.strictEqual(unknown.label, '未知状态');
  assert.strictEqual(unknown.action, 'refresh');
  assert.strictEqual(unknown.actionLabel, '重新检测');
  assert.strictEqual(unknown.status, 'some_new_status');
});

test('formatProviderStatus：空值不抛错并标记为 unknown', () => {
  ['', null, undefined].forEach((value) => {
    const status = formatProviderStatus(value);
    assert.strictEqual(status.label, '未知状态');
    assert.strictEqual(status.status, 'unknown');
  });
  // 后端偶尔会带空白，需要 trim 后再匹配
  assert.strictEqual(formatProviderStatus(' ready ').label, '可用');
});

test('formatProviderStatus：返回新对象，不泄漏内部映射表', () => {
  const first = formatProviderStatus('ready');
  first.label = '被改坏了';
  assert.strictEqual(formatProviderStatus('ready').label, '可用');
  assert.notStrictEqual(formatProviderStatus('ready'), STATUS_MAP.ready);
});

test('qualityTierLabel：质量级别中文化', () => {
  assert.strictEqual(qualityTierLabel('advanced'), '高级');
  assert.strictEqual(qualityTierLabel('original'), '原始元数据');
  assert.strictEqual(qualityTierLabel('basic'), '基础');
  assert.strictEqual(qualityTierLabel(undefined), '基础');
});

test('STAGE_LABELS：反推阶段为中文且不使用百分比', () => {
  assert.deepStrictEqual(Object.keys(STAGE_LABELS), ['reading', 'metadata', 'vision', 'structuring']);
  Object.keys(STAGE_LABELS).forEach((key) => {
    assert.ok(STAGE_LABELS[key], key + ' 需要中文阶段名');
  });
  assert.strictEqual(STAGE_LABELS.reading, '读取图片');
  assert.strictEqual(STAGE_LABELS.structuring, '结构化整理');
});
