/* 与后端 core/textutil.normalize_text 等价的前端规范化实现（PRD M7.3 去重判定依据） */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { normalizeText } = require('../../static/js/library.js');

test('normalizeText：空值返回空串', () => {
  assert.strictEqual(normalizeText(''), '');
  assert.strictEqual(normalizeText(null), '');
  assert.strictEqual(normalizeText(undefined), '');
  assert.strictEqual(normalizeText(0), '');
});

test('normalizeText：英文小写化', () => {
  assert.strictEqual(normalizeText('Hello World'), 'hello world');
  assert.strictEqual(normalizeText('Best Quality'), 'best quality');
});

test('normalizeText：全角转半角（NFKC）', () => {
  assert.strictEqual(normalizeText('ＡＢＣ１２３'), 'abc123');
  assert.strictEqual(normalizeText('ｃａｔ'), 'cat');
});

test('normalizeText：全角标点转半角', () => {
  assert.strictEqual(normalizeText('柔光，逆光'), '柔光,逆光');
  assert.strictEqual(normalizeText('猫、狗'), '猫,狗');
  assert.strictEqual(normalizeText('侧光；轮廓光'), '侧光,轮廓光');
  assert.strictEqual(normalizeText('这是一张图。'), '这是一张图');
  assert.strictEqual(normalizeText('（逆光）'), '逆光');
});

test('normalizeText：折叠空白并去掉首尾空白', () => {
  assert.strictEqual(normalizeText('  柔光   逆光  '), '柔光 逆光');
  assert.strictEqual(normalizeText('柔光\t\t逆光'), '柔光 逆光');
  assert.strictEqual(normalizeText('柔光　逆光'), '柔光 逆光');
});

test('normalizeText：去掉首尾标点', () => {
  assert.strictEqual(normalizeText('，柔光，'), '柔光');
  assert.strictEqual(normalizeText('Hello, World.'), 'hello, world');
  assert.strictEqual(normalizeText('***逆光***'), '逆光');
});

test('normalizeText：控制字符转成空格', () => {
  assert.strictEqual(normalizeText('柔光\u0007逆光'), '柔光 逆光');
  assert.strictEqual(normalizeText('柔光\u001f逆光'), '柔光 逆光');
});

test('normalizeText：中英文标点与空白差异不影响去重判定', () => {
  assert.strictEqual(normalizeText('柔光，逆光'), normalizeText('柔光,逆光'));
  assert.strictEqual(normalizeText(' 柔光,逆光 '), normalizeText('柔光，逆光。'));
  assert.strictEqual(normalizeText('Rim Light'), normalizeText('rim  light'));
});

test('normalizeText：内容不同则结果不同', () => {
  assert.notStrictEqual(normalizeText('柔光'), normalizeText('逆光'));
  assert.notStrictEqual(normalizeText('a cat'), normalizeText('a dog'));
});
