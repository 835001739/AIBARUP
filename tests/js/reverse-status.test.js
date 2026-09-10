/* 图片反推纯逻辑测试（PRD M9.3 / M10.3）
   运行：node --test tests/js/
   重点：Provider 状态枚举映射完整、未知状态有兜底、阶段文案完整。 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const reverse = require('../../static/js/reverse.js');

/* API_CONTRACT / reverse/providers/base.py 中定义的完整状态枚举 */
const STATUS_ENUM = [
  'ready',
  'unconfigured',
  'offline',
  'missing_node',
  'missing_model',
  'missing_mmproj',
  'incompatible_runtime',
  'out_of_memory',
  'unauthorized',
  'error'
];

const TONES = ['success', 'error', 'warning', 'neutral'];

/* ---------------------------------------------------------- 双环境导出 */

test('状态映射同时导出到 module.exports 与 AIBAR.logic', () => {
  assert.strictEqual(typeof reverse.formatProviderStatus, 'function');
  assert.strictEqual(globalThis.AIBAR.logic.formatProviderStatus, reverse.formatProviderStatus);
  assert.strictEqual(globalThis.AIBAR.logic.qualityTierLabel, reverse.qualityTierLabel);
});

/* ---------------------------------------------------------- formatProviderStatus */

test('状态枚举映射完整且每项都有中文原因', () => {
  STATUS_ENUM.forEach((status) => {
    const mapped = reverse.STATUS_MAP[status];
    assert.ok(mapped, '缺少状态映射：' + status);
    assert.ok(mapped.label && mapped.label.length > 0, status + ' 缺少中文状态文案');
    assert.ok(mapped.hint && mapped.hint.length > 0, status + ' 缺少原因说明');
    assert.ok(TONES.indexOf(mapped.tone) !== -1, status + ' 的 tone 不在允许集合内');
  });
});

test('formatProviderStatus 回传状态与映射一致', () => {
  STATUS_ENUM.forEach((status) => {
    const result = reverse.formatProviderStatus(status);
    assert.strictEqual(result.status, status);
    assert.strictEqual(result.label, reverse.STATUS_MAP[status].label);
    assert.strictEqual(result.tone, reverse.STATUS_MAP[status].tone);
    assert.strictEqual(result.action, reverse.STATUS_MAP[status].action);
  });
});

test('ComfyUI 未运行时给出「启动 ComfyUI」恢复操作', () => {
  const result = reverse.formatProviderStatus('offline');
  assert.strictEqual(result.action, 'start_comfyui');
  assert.strictEqual(result.actionLabel, '启动 ComfyUI');
  assert.strictEqual(result.tone, 'error');
});

test('未确认外发的外部 Provider 给出 consent 恢复操作', () => {
  const result = reverse.formatProviderStatus('unauthorized');
  assert.strictEqual(result.action, 'consent');
  assert.strictEqual(result.actionLabel, '确认图片外发');
  assert.strictEqual(result.tone, 'warning');
});

test('未知状态回退为「未知状态」并建议重新检测', () => {
  const result = reverse.formatProviderStatus('some_future_status');
  assert.strictEqual(result.status, 'some_future_status');
  assert.strictEqual(result.label, '未知状态');
  assert.strictEqual(result.tone, 'neutral');
  assert.strictEqual(result.action, 'refresh');
  assert.strictEqual(result.actionLabel, '重新检测');
});

test('空值状态同样有兜底文案，不抛异常', () => {
  const result = reverse.formatProviderStatus(undefined);
  assert.strictEqual(result.label, '未知状态');
  assert.strictEqual(reverse.formatProviderStatus('').label, '未知状态');
  assert.strictEqual(reverse.formatProviderStatus('  ready  ').label, '可用');
});

/* ---------------------------------------------------------- qualityTierLabel */

test('质量级别文案：基础 / 高级 / 原始元数据', () => {
  assert.strictEqual(reverse.qualityTierLabel('basic'), '基础');
  assert.strictEqual(reverse.qualityTierLabel('advanced'), '高级');
  assert.strictEqual(reverse.qualityTierLabel('original'), '原始元数据');
  assert.strictEqual(reverse.qualityTierLabel(undefined), '基础');
  assert.strictEqual(reverse.qualityTierLabel(''), '基础');
});

/* ---------------------------------------------------------- 阶段进度 */

test('阶段文案覆盖「读取图片 / 检查元数据 / 视觉分析 / 结构化整理」', () => {
  const labels = reverse.STAGE_LABELS;
  assert.strictEqual(labels.reading, '读取图片');
  assert.strictEqual(labels.metadata, '检查元数据');
  assert.strictEqual(labels.vision, '视觉分析');
  assert.strictEqual(labels.structuring, '结构化整理');
  assert.strictEqual(Object.keys(labels).length, 4);
});

/* ---------------------------------------------------------- 真实进度：终态判定 */

test('isTerminalStatus：三种终态都要停止轮询', () => {
  assert.strictEqual(reverse.isTerminalStatus('completed'), true);
  assert.strictEqual(reverse.isTerminalStatus('failed'), true);
  assert.strictEqual(reverse.isTerminalStatus('cancelled'), true);
});

test('isTerminalStatus：进行中的状态不能停（否则进度会卡在第一格）', () => {
  ['pending', 'running', '', undefined, null].forEach((status) => {
    assert.strictEqual(reverse.isTerminalStatus(status), false, String(status) + ' 不该是终态');
  });
});

test('isTerminalStatus 与导出的 TERMINAL_STATUSES 保持一致', () => {
  assert.deepStrictEqual(reverse.TERMINAL_STATUSES.slice().sort(), ['cancelled', 'completed', 'failed']);
  reverse.TERMINAL_STATUSES.forEach((status) => {
    assert.strictEqual(reverse.isTerminalStatus(status), true);
  });
});

/* ---------------------------------------------------------- 真实进度：阶段映射 */

test('stageView：按后端 stage 点亮到对应阶段，前面的都算已完成', () => {
  assert.deepStrictEqual(reverse.stageView({ status: 'running', stage: 'reading' }).activeKeys, ['reading']);
  assert.deepStrictEqual(reverse.stageView({ status: 'running', stage: 'metadata' }).activeKeys, ['reading', 'metadata']);
  assert.deepStrictEqual(reverse.stageView({ status: 'running', stage: 'vision' }).activeKeys, ['reading', 'metadata', 'vision']);
  assert.deepStrictEqual(
    reverse.stageView({ status: 'running', stage: 'structuring' }).activeKeys,
    reverse.STAGE_KEYS
  );
});

test('stageView：排队中一格都不点亮（后端还没开始干活）', () => {
  const view = reverse.stageView({ status: 'pending', stage: 'queued' });
  assert.deepStrictEqual(view.activeKeys, []);
  assert.strictEqual(view.label, '排队中');
  assert.strictEqual(view.terminal, false);
});

test('stageView：已完成时四格全亮', () => {
  const view = reverse.stageView({ status: 'completed', stage: 'done', stage_label: '已完成' });
  assert.deepStrictEqual(view.activeKeys, reverse.STAGE_KEYS);
  assert.strictEqual(view.terminal, true);
});

test('stageView：失败与取消都不点亮任何阶段，且都是终态', () => {
  const failed = reverse.stageView({ status: 'failed', stage: 'failed' });
  assert.deepStrictEqual(failed.activeKeys, []);
  assert.strictEqual(failed.terminal, true);

  const cancelled = reverse.stageView({ status: 'cancelled', stage: 'cancelled' });
  assert.deepStrictEqual(cancelled.activeKeys, []);
  assert.strictEqual(cancelled.terminal, true);
});

test('stageView：优先用后端 stage_label，缺失时回退本地文案', () => {
  assert.strictEqual(
    reverse.stageView({ status: 'running', stage: 'vision', stage_label: '视觉分析' }).label,
    '视觉分析'
  );
  // 后端升级加了新 stage_label 时，前端不认识也能照常显示
  assert.strictEqual(
    reverse.stageView({ status: 'running', stage: 'vision', stage_label: '调用视觉大模型' }).label,
    '调用视觉大模型'
  );
  assert.strictEqual(reverse.stageView({ status: 'running', stage: 'vision' }).label, '视觉分析');
});

test('stageView：未知 stage 不炸，也不误点亮', () => {
  const view = reverse.stageView({ status: 'running', stage: 'some_future_stage' });
  assert.deepStrictEqual(view.activeKeys, []);
  assert.strictEqual(view.label, '');
  assert.strictEqual(view.terminal, false);
});

test('stageView：空输入不抛异常（任务还没建立时的首次渲染）', () => {
  [undefined, null, {}, { status: null, stage: null }].forEach((job) => {
    const view = reverse.stageView(job);
    assert.deepStrictEqual(view.activeKeys, []);
    assert.strictEqual(view.terminal, false);
  });
});

/* ---------------------------------------------------------- 双环境导出 */

test('进度相关纯函数同时导出到 module.exports 与 AIBAR.logic', () => {
  assert.strictEqual(globalThis.AIBAR.logic.stageView, reverse.stageView);
  assert.strictEqual(globalThis.AIBAR.logic.isTerminalStatus, reverse.isTerminalStatus);
  assert.deepStrictEqual(globalThis.AIBAR.logic.STAGE_LABELS, reverse.STAGE_LABELS);
});
