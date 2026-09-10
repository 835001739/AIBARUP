/* 提示词库筛选：已激活筛选的 Chip 列表与单项移除（PRD M7.2 渐进披露） */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const {
  buildChipList,
  removeFilter,
  isFilterActive,
  FILTER_LABELS,
  MEDIA_LABELS,
  SORT_LABELS
} = require('../../static/js/library.js');

function emptyFilters() {
  return { q: '', dimension: '', subcategory: '', profile: '', source: '', tag: '', favorite: '', sort: '' };
}

test('buildChipList：没有激活筛选时返回空数组', () => {
  assert.deepStrictEqual(buildChipList(emptyFilters()), []);
  assert.deepStrictEqual(buildChipList(null), []);
  assert.deepStrictEqual(buildChipList(undefined), []);
  assert.deepStrictEqual(buildChipList(), []);
});

test('buildChipList：搜索关键词生成可删除的 Chip', () => {
  const chips = buildChipList(Object.assign(emptyFilters(), { q: '逆光' }));
  assert.strictEqual(chips.length, 1);
  assert.strictEqual(chips[0].key, 'q');
  assert.strictEqual(chips[0].label, '搜索');
  assert.strictEqual(chips[0].value, '逆光');
  assert.strictEqual(chips[0].text, '搜索：逆光');
});

test('buildChipList：维度/子类用分面文案回显，取不到时退回原值', () => {
  const filters = Object.assign(emptyFilters(), { dimension: 'subject', subcategory: 'lighting' });
  const labels = {
    dimension: { subject: '主体与外观' },
    subcategory: { lighting: '光线' }
  };
  const chips = buildChipList(filters, labels);
  assert.deepStrictEqual(chips.map((chip) => chip.value), ['主体与外观', '光线']);

  // 没有提供映射时显示原始 key，不能显示空白
  const raw = buildChipList(filters);
  assert.deepStrictEqual(raw.map((chip) => chip.value), ['subject', 'lighting']);
});

test('buildChipList：媒体类型与排序用固定中文文案', () => {
  const chips = buildChipList(Object.assign(emptyFilters(), { media_type: 'music', sort: 'most_used' }));
  assert.deepStrictEqual(chips.map((chip) => chip.key), ['media_type', 'sort']);
  assert.strictEqual(chips[0].value, '歌曲');
  assert.strictEqual(chips[0].label, '媒体');
  assert.strictEqual(chips[1].value, '使用最多');
});

test('buildChipList：排序为默认值时不生成 Chip', () => {
  assert.deepStrictEqual(buildChipList(Object.assign(emptyFilters(), { sort: 'recommended' })), []);
  assert.strictEqual(buildChipList(Object.assign(emptyFilters(), { sort: 'recent' })).length, 1);
});

test('buildChipList：收藏只在打开时生成 Chip', () => {
  assert.strictEqual(buildChipList(Object.assign(emptyFilters(), { favorite: '1' }))[0].value, '仅收藏');
  assert.strictEqual(buildChipList(Object.assign(emptyFilters(), { favorite: true })).length, 1);
  assert.deepStrictEqual(buildChipList(Object.assign(emptyFilters(), { favorite: '' })), []);
  assert.deepStrictEqual(buildChipList(Object.assign(emptyFilters(), { favorite: '0' })), []);
  assert.deepStrictEqual(buildChipList(Object.assign(emptyFilters(), { favorite: false })), []);
});

test('buildChipList：多个筛选按固定顺序输出', () => {
  const filters = Object.assign(emptyFilters(), {
    media_type: 'image',
    q: '猫',
    dimension: 'subject',
    subcategory: 'lighting',
    profile: 'sd15_sdxl',
    source: 'auto',
    tag: '逆光',
    favorite: '1',
    sort: 'recent'
  });
  const chips = buildChipList(filters);
  assert.deepStrictEqual(chips.map((chip) => chip.key), [
    'media_type', 'q', 'dimension', 'subcategory', 'profile', 'source', 'tag', 'favorite', 'sort'
  ]);
  chips.forEach((chip) => {
    assert.ok(chip.label, '每个 Chip 都要有标签');
    assert.ok(chip.value, '每个 Chip 都要有值');
    assert.strictEqual(chip.text, chip.label + '：' + chip.value);
  });
});

test('removeFilter：不修改入参，只清空目标筛选', () => {
  const before = Object.assign(emptyFilters(), { q: '猫', dimension: 'subject' });
  const next = removeFilter(before, 'q');
  assert.strictEqual(next.q, '');
  assert.strictEqual(next.dimension, 'subject');
  assert.strictEqual(before.q, '猫', '原对象不能被修改');
  assert.notStrictEqual(next, before);
});

test('removeFilter：移除排序回到默认，移除不存在的键不报错', () => {
  assert.strictEqual(removeFilter({ sort: 'most_used' }, 'sort').sort, '');
  const other = removeFilter({ q: '猫' }, 'dimension');
  assert.strictEqual(other.q, '猫');
  assert.strictEqual(other.dimension, '');
});

test('removeFilter：逐项移除后可把 Chip 清空', () => {
  let filters = Object.assign(emptyFilters(), { q: '猫', dimension: 'subject', favorite: '1' });
  buildChipList(filters).map((chip) => chip.key).forEach((key) => {
    filters = removeFilter(filters, key);
  });
  assert.deepStrictEqual(buildChipList(filters), []);
});

test('isFilterActive：空值一律视为未激活', () => {
  assert.strictEqual(isFilterActive(emptyFilters(), 'q'), false);
  assert.strictEqual(isFilterActive(null, 'q'), false);
  assert.strictEqual(isFilterActive({ q: '猫' }, 'q'), true);
  assert.strictEqual(isFilterActive({ tag: '逆光' }, 'tag'), true);
});

test('文案表：媒体、排序、筛选标签都有中文', () => {
  assert.deepStrictEqual(MEDIA_LABELS, { image: '图片', video: '视频', music: '歌曲' });
  assert.strictEqual(SORT_LABELS.recommended, '推荐');
  assert.strictEqual(FILTER_LABELS.subcategory, '子类');
});
