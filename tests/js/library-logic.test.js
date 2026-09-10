/* 提示词库纯逻辑测试（PRD M7.3 / M8.6）
   运行：node --test tests/js/
   无 npm 依赖；被测函数同时挂在 window.AIBAR.logic 与 module.exports。 */

'use strict';

const test = require('node:test');
const assert = require('node:assert');

const lib = require('../../static/js/library.js');

/* ---------------------------------------------------------- 双环境导出 */

test('纯逻辑同时导出到 module.exports 与 AIBAR.logic', () => {
  assert.strictEqual(typeof lib.insertAtCursor, 'function');
  assert.strictEqual(typeof globalThis.AIBAR.logic.insertAtCursor, 'function');
  assert.strictEqual(globalThis.AIBAR.logic.insertAtCursor, lib.insertAtCursor);
  assert.strictEqual(typeof globalThis.AIBAR.logic.buildChipList, 'function');
  assert.strictEqual(typeof globalThis.AIBAR.logic.mediaFiltersKey, 'function');
});

/* ---------------------------------------------------------- separatorFor */

test('连接符按模型档案选择：英文逗号 / 句点 / 中文逗号', () => {
  assert.strictEqual(lib.separatorFor('sd15_sdxl'), ', ');
  assert.strictEqual(lib.separatorFor('flux_flux2'), '. ');
  assert.strictEqual(lib.separatorFor('generic'), '，');
  assert.strictEqual(lib.separatorFor('unknown_profile'), '，');
  assert.strictEqual(lib.separatorFor(undefined), '，');
});

/* ---------------------------------------------------------- insertAtCursor */

test('空原文插入时不补连接符', () => {
  const result = lib.insertAtCursor('', '电影感打光', null, 'generic');
  assert.strictEqual(result.inserted, true);
  assert.strictEqual(result.text, '电影感打光');
  assert.strictEqual(result.cursorPos, '电影感打光'.length);
});

test('无光标位置时追加到末尾', () => {
  const result = lib.insertAtCursor('雨夜街道', '霓虹反射', null, 'sd15_sdxl');
  assert.strictEqual(result.inserted, true);
  assert.strictEqual(result.text, '雨夜街道, 霓虹反射');
  assert.strictEqual(result.cursorPos, '雨夜街道, 霓虹反射'.length);
});

test('光标越界时同样追加到末尾', () => {
  const result = lib.insertAtCursor('abc', 'x', 99, 'generic');
  assert.strictEqual(result.text, 'abc，x');
  assert.strictEqual(result.cursorPos, 'abc，x'.length);
});

test('插入发生在光标位置并在两侧补连接符', () => {
  // 光标位于「，」之后：前侧只补空格，后侧补中文逗号
  const result = lib.insertAtCursor('主体：少女，背景：街道', '柔光', 6, 'generic');
  assert.strictEqual(result.inserted, true);
  assert.strictEqual(result.text, '主体：少女， 柔光，背景：街道');
  assert.strictEqual(result.cursorPos, 10);
  assert.strictEqual(result.text.slice(0, result.cursorPos), '主体：少女， 柔光，');
});

test('已存在完全相同的片段时不重复插入', () => {
  const result = lib.insertAtCursor('霓虹反射, 柔光', '柔光', 3, 'sd15_sdxl');
  assert.strictEqual(result.inserted, false);
  assert.strictEqual(result.reason, 'duplicate');
  assert.strictEqual(result.text, '霓虹反射, 柔光');
});

test('待插入内容为空时不插入', () => {
  const result = lib.insertAtCursor('abc', '   ', 1, 'generic');
  assert.strictEqual(result.inserted, false);
  assert.strictEqual(result.reason, 'empty');
  assert.strictEqual(result.text, 'abc');
});

test('插入不修改传入的原文字符串', () => {
  const original = '少女';
  lib.insertAtCursor(original, '柔光', 2, 'generic');
  assert.strictEqual(original, '少女');
});

test('flux 档案使用句点与空格作为连接符', () => {
  const result = lib.insertAtCursor('a girl', 'soft light', 6, 'flux_flux2');
  assert.strictEqual(result.text, 'a girl. soft light');
});

/* ---------------------------------------------------------- buildChipList */

test('筛选 Chip 按固定顺序生成并带可读文案', () => {
  const chips = lib.buildChipList(
    { q: '雨夜', dimension: 'subject', favorite: '1', sort: 'most_used' },
    { dimension: { subject: '主体与外观' } }
  );
  assert.deepStrictEqual(chips.map((chip) => chip.key), ['q', 'dimension', 'favorite', 'sort']);
  assert.strictEqual(chips[0].text, '搜索：雨夜');
  assert.strictEqual(chips[1].text, '维度：主体与外观');
  assert.strictEqual(chips[2].text, '收藏：仅收藏');
  assert.strictEqual(chips[3].text, '排序：使用最多');
});

test('默认值不生成 Chip', () => {
  assert.strictEqual(lib.buildChipList({}).length, 0);
  assert.strictEqual(lib.buildChipList({ sort: 'recommended' }).length, 0);
  assert.strictEqual(lib.buildChipList({ favorite: '' }).length, 0);
  assert.strictEqual(lib.buildChipList({ favorite: false }).length, 0);
  assert.strictEqual(lib.buildChipList({ q: '' }).length, 0);
});

test('removeFilter 单项移除且不修改入参', () => {
  const original = { q: '雨', dimension: 'subject', sort: 'recent' };
  const next = lib.removeFilter(original, 'dimension');
  assert.strictEqual(next.dimension, '');
  assert.strictEqual(next.q, '雨');
  assert.strictEqual(next.sort, 'recent');
  assert.strictEqual(original.dimension, 'subject');
});

test('移除排序后回到空值，避免残留非默认排序', () => {
  const next = lib.removeFilter({ sort: 'recent' }, 'sort');
  assert.strictEqual(next.sort, '');
});

/* ---------------------------------------------------------- mediaFiltersKey */

test('筛选状态按媒体类型分 key 存储', () => {
  assert.strictEqual(lib.mediaFiltersKey('image'), 'aibar:lib:filters:image');
  assert.strictEqual(lib.mediaFiltersKey('video'), 'aibar:lib:filters:video');
  assert.strictEqual(lib.mediaFiltersKey('music'), 'aibar:lib:filters:music');
  assert.strictEqual(lib.mediaFiltersKey(''), 'aibar:lib:filters:image');
  assert.strictEqual(lib.mediaFiltersKey(), 'aibar:lib:filters:image');
  assert.notStrictEqual(lib.mediaFiltersKey('image'), lib.mediaFiltersKey('video'));
});

/* ---------------------------------------------------------- 词条编辑：表单校验 */

test('validateEntryInput：正文够长且其他字段没超限时通过', () => {
  const r = lib.validateEntryInput({
    prompt_text: '逆光形成金色轮廓光',
    title: '逆光',
    description: '适合人像',
    negative_text: '模糊',
    tags: ['光影']
  });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.field, '');
  assert.strictEqual(r.message, '');
});

test('validateEntryInput：正文太短被拦下并指明字段', () => {
  ['', '  ', '光', '光影'].slice(0, 3).forEach((text) => {
    const r = lib.validateEntryInput({ prompt_text: text });
    assert.strictEqual(r.ok, false, JSON.stringify(text) + ' 不该通过');
    assert.strictEqual(r.field, 'prompt_text');
    assert.ok(r.message.length > 0);
  });
  // 下限与后端 MIN_TEXT_LEN 一致：4 个字符才放行
  assert.strictEqual(lib.validateEntryInput({ prompt_text: '光影' }).ok, false);
  assert.strictEqual(lib.validateEntryInput({ prompt_text: '逆光形成' }).ok, true);
});

test('validateEntryInput：正文首尾空格不算长度', () => {
  assert.strictEqual(lib.validateEntryInput({ prompt_text: '  逆光形成  ' }).ok, true);
  assert.strictEqual(lib.validateEntryInput({ prompt_text: '  光  ' }).ok, false);
});

test('validateEntryInput：各字段超长时分别报出对应字段', () => {
  const limits = lib.ENTRY_LIMITS;
  assert.strictEqual(limits.minText, 4);
  assert.strictEqual(limits.maxText, 200);
  assert.strictEqual(limits.maxTitle, 60);
  assert.strictEqual(limits.maxDesc, 300);
  assert.strictEqual(limits.maxNegative, 300);
  assert.strictEqual(limits.maxTags, 20);

  assert.strictEqual(
    lib.validateEntryInput({ prompt_text: '逆光形成', title: 'x'.repeat(61) }).field,
    'title'
  );
  assert.strictEqual(
    lib.validateEntryInput({ prompt_text: '逆光形成', description: 'x'.repeat(301) }).field,
    'description'
  );
  assert.strictEqual(
    lib.validateEntryInput({ prompt_text: '逆光形成', negative_text: 'x'.repeat(301) }).field,
    'negative_text'
  );
  assert.strictEqual(
    lib.validateEntryInput({ prompt_text: '逆光形成', tags: new Array(21).fill('a') }).field,
    'tags'
  );
});

test('validateEntryInput：空入参不抛异常', () => {
  [undefined, null, {}].forEach((payload) => {
    const r = lib.validateEntryInput(payload);
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.field, 'prompt_text');
  });
});

test('validateEntryInput：requireDimension 只在新建时强制维度', () => {
  const withDim = { prompt_text: '逆光形成金色轮廓光', dimension: 'subject' };
  const noDim = { prompt_text: '逆光形成金色轮廓光' };

  // 新建：后端 insert_entry 拿维度算指纹并校验合法性，缺了直接 400
  assert.strictEqual(lib.validateEntryInput(withDim, { requireDimension: true }).ok, true);
  const missing = lib.validateEntryInput(noDim, { requireDimension: true });
  assert.strictEqual(missing.ok, false);
  assert.strictEqual(missing.field, 'dimension');

  // 编辑：不传该选项、或显式关掉都应放行——PATCH 只改提交的字段，维度留空即"不改"
  assert.strictEqual(lib.validateEntryInput(noDim).ok, true);
  assert.strictEqual(lib.validateEntryInput(noDim, { requireDimension: false }).ok, true);
});

test('validateEntryInput：维度校验排在正文之后，正文错误优先报', () => {
  // 两个都错时先说正文太短：让用户先改最要紧的那一项，别绕一圈才发现还得改正文
  const r = lib.validateEntryInput({ prompt_text: '光' }, { requireDimension: true });
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.field, 'prompt_text');
});

/* ---------------------------------------------------------- 词条编辑：标签解析 */

test('parseTags：中英文逗号、顿号、分号与空白都能当分隔符', () => {
  assert.deepStrictEqual(lib.parseTags('构图，人物、节奏;氛围'), ['构图', '人物', '节奏', '氛围']);
  assert.deepStrictEqual(lib.parseTags('a, b  c'), ['a', 'b', 'c']);
  assert.deepStrictEqual(lib.parseTags('  逆光  '), ['逆光']);
});

test('parseTags：自动去重且保持输入顺序', () => {
  assert.deepStrictEqual(lib.parseTags('人物，构图，人物'), ['人物', '构图']);
});

test('parseTags：空输入返回空数组', () => {
  assert.deepStrictEqual(lib.parseTags(''), []);
  assert.deepStrictEqual(lib.parseTags('   '), []);
  assert.deepStrictEqual(lib.parseTags(null), []);
  assert.deepStrictEqual(lib.parseTags(undefined), []);
  assert.deepStrictEqual(lib.parseTags('，，，'), []);
});

/* ---------------------------------------------------------- 双环境导出（编辑相关） */

test('编辑相关纯函数同时导出到 module.exports 与 AIBAR.logic', () => {
  assert.strictEqual(globalThis.AIBAR.logic.validateEntryInput, lib.validateEntryInput);
  assert.strictEqual(globalThis.AIBAR.logic.parseTags, lib.parseTags);
  assert.deepStrictEqual(globalThis.AIBAR.logic.ENTRY_LIMITS, lib.ENTRY_LIMITS);
});
