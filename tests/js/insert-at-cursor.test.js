/* 提示词回填：光标插入 / 末尾追加 / 去重不插入 / 模型档案连接符（PRD M7.3） */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { insertAtCursor, separatorFor } = require('../../static/js/library.js');

test('separatorFor：按模型档案选择连接符', () => {
  assert.strictEqual(separatorFor('sd15_sdxl'), ', ');
  assert.strictEqual(separatorFor('flux_flux2'), '. ');
  assert.strictEqual(separatorFor('generic'), '，');
  // 未知档案退回中文逗号，不抛错
  assert.strictEqual(separatorFor('unknown_profile'), '，');
  assert.strictEqual(separatorFor(undefined), '，');
});

test('insertAtCursor：在光标位置插入并自动补连接符', () => {
  const r = insertAtCursor('一只猫', '坐在窗台', 3, 'generic');
  assert.strictEqual(r.inserted, true);
  assert.strictEqual(r.text, '一只猫，坐在窗台');
  // cursorPos = 光标位置(3) + 实际插入的长度('，坐在窗台'=5)。
  // 这里曾经写成 6，与下面「中间插入」用例的 8 自相矛盾——以实现与相邻用例为准。
  assert.strictEqual(r.cursorPos, 8);
  assert.strictEqual(r.reason, '');
  assert.strictEqual(r.separator, '，');
});

test('insertAtCursor：中间插入时前后都补连接符', () => {
  const r = insertAtCursor('一只猫在睡觉', '可爱的', 3, 'generic');
  assert.strictEqual(r.text, '一只猫，可爱的，在睡觉');
  assert.strictEqual(r.cursorPos, 8);
});

test('insertAtCursor：光标为空时追加到末尾', () => {
  const withNull = insertAtCursor('一只猫', '坐在窗台', null, 'generic');
  assert.strictEqual(withNull.text, '一只猫，坐在窗台');
  // 光标为空即退到末尾，与「光标在末尾」是同一条路径，cursorPos 同样是 8
  assert.strictEqual(withNull.cursorPos, 8);

  const withUndefined = insertAtCursor('一只猫', '坐在窗台', undefined, 'generic');
  assert.strictEqual(withUndefined.text, '一只猫，坐在窗台');
});

test('insertAtCursor：光标越界时退回末尾', () => {
  const over = insertAtCursor('一只猫', '坐在窗台', 99, 'generic');
  assert.strictEqual(over.text, '一只猫，坐在窗台');

  const negative = insertAtCursor('一只猫', '坐在窗台', -5, 'generic');
  assert.strictEqual(negative.text, '一只猫，坐在窗台');
});

test('insertAtCursor：原文为空时不加前导连接符', () => {
  const r = insertAtCursor('', '柔光', 0, 'generic');
  assert.strictEqual(r.inserted, true);
  assert.strictEqual(r.text, '柔光');
  assert.strictEqual(r.cursorPos, 2);
});

test('insertAtCursor：已处于分隔符位置时只补一个空格', () => {
  const r = insertAtCursor('一只猫，', '在睡觉', 4, 'generic');
  assert.strictEqual(r.text, '一只猫， 在睡觉');
  assert.strictEqual(r.cursorPos, 8);
});

test('insertAtCursor：英文标签档案用逗号分隔', () => {
  const r = insertAtCursor('a', 'b', 1, 'sd15_sdxl');
  assert.strictEqual(r.text, 'a, b');
  assert.strictEqual(r.cursorPos, 4);
});

test('insertAtCursor：自然语言档案用句点分隔', () => {
  const r = insertAtCursor('A cat', 'sitting', 5, 'flux_flux2');
  assert.strictEqual(r.text, 'A cat. sitting');
  assert.strictEqual(r.cursorPos, 14);
});

test('insertAtCursor：已有完全相同片段时不插入', () => {
  const r = insertAtCursor('柔光，逆光', '柔光', 0, 'generic');
  assert.strictEqual(r.inserted, false);
  assert.strictEqual(r.reason, 'duplicate');
  assert.strictEqual(r.text, '柔光，逆光');
  assert.strictEqual(r.cursorPos, 0);
});

test('insertAtCursor：忽略中英文标点差异的重复片段', () => {
  const r = insertAtCursor('柔光,逆光', '柔光', 0, 'generic');
  assert.strictEqual(r.inserted, false);
  assert.strictEqual(r.reason, 'duplicate');
});

test('insertAtCursor：位于末尾的重复片段同样被拦下', () => {
  const r = insertAtCursor('画面 柔光', '柔光', 0, 'generic');
  assert.strictEqual(r.inserted, false);
  assert.strictEqual(r.reason, 'duplicate');
});

test('insertAtCursor：忽略大小写的重复片段', () => {
  const r = insertAtCursor('Rim Light', 'rim light', 0, 'sd15_sdxl');
  assert.strictEqual(r.inserted, false);
  assert.strictEqual(r.reason, 'duplicate');
});

test('insertAtCursor：空片段返回 empty 且不改动原文', () => {
  const r = insertAtCursor('abc', '   ', 1, 'generic');
  assert.strictEqual(r.inserted, false);
  assert.strictEqual(r.reason, 'empty');
  assert.strictEqual(r.text, 'abc');
});

test('insertAtCursor：不修改传入的原文', () => {
  const original = '一只猫';
  insertAtCursor(original, '坐在窗台', 3, 'generic');
  assert.strictEqual(original, '一只猫');
});
