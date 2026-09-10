/* 提示词库筛选状态按媒体类型分别记忆的 sessionStorage 键名（PRD M7.2） */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const { mediaStateKey, mediaFiltersKey } = require('../../static/js/library.js');

test('mediaStateKey：每种媒体类型一个独立键', () => {
  assert.strictEqual(mediaStateKey('image'), 'aibar:lib:filters:image');
  assert.strictEqual(mediaStateKey('video'), 'aibar:lib:filters:video');
  assert.strictEqual(mediaStateKey('music'), 'aibar:lib:filters:music');
});

test('mediaStateKey：不同媒体的键互不覆盖', () => {
  const keys = ['image', 'video', 'music'].map(mediaStateKey);
  assert.strictEqual(new Set(keys).size, 3);
});

test('mediaStateKey：未知或缺失媒体回退到 image', () => {
  assert.strictEqual(mediaStateKey(), 'aibar:lib:filters:image');
  assert.strictEqual(mediaStateKey(null), 'aibar:lib:filters:image');
  assert.strictEqual(mediaStateKey(''), 'aibar:lib:filters:image');
  assert.strictEqual(mediaStateKey('unknown'), 'aibar:lib:filters:unknown');
});

test('mediaStateKey：与 mediaFiltersKey 完全等价', () => {
  ['image', 'video', 'music', undefined].forEach((media) => {
    assert.strictEqual(mediaStateKey(media), mediaFiltersKey(media));
  });
});

test('mediaStateKey：统一使用 aibar 前缀，避免与其他功能串 key', () => {
  assert.ok(mediaStateKey('image').indexOf('aibar:lib:filters:') === 0);
});
