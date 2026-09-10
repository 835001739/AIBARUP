/*
 * ui.js::pagerModel —— 分页控件的纯计算部分。
 *
 * 为什么要单独测它：工作流/案例库/词库共用这一个模型，页码越界、总数变小后
 * 页码没夹回来这类错误会直接表现为「列表空白但没有任何报错」，最难排查。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert');

const ui = require('../../static/js/ui.js');
const pagerModel = ui.pagerModel;

test('pagerModel：页码与总页数按每页条数算出', () => {
  const m = pagerModel(1, 100, 24);
  assert.strictEqual(m.totalPages, 5, '100 条 / 每页 24 = 5 页（向上取整）');
  assert.strictEqual(m.page, 1);
  assert.strictEqual(m.hasPrev, false);
  assert.strictEqual(m.hasNext, true);
});

test('pagerModel：最后一页没有下一页', () => {
  const m = pagerModel(5, 100, 24);
  assert.strictEqual(m.hasNext, false);
  assert.strictEqual(m.hasPrev, true);
});

test('pagerModel：总数变化时把当前页夹回合法区间', () => {
  // 典型场景：停在第 9 页时改了筛选条件，结果只剩 3 页。
  // 不夹的话请求会带 page=9 打到后端，返回空列表，界面上一片空白却没有任何报错。
  assert.strictEqual(pagerModel(9, 60, 24).page, 3);
  assert.strictEqual(pagerModel(40, 10, 24).page, 1);
});

test('pagerModel：空结果集也自洽（1 页，无上下页）', () => {
  const m = pagerModel(3, 0, 24);
  assert.strictEqual(m.total, 0);
  assert.strictEqual(m.totalPages, 1);
  assert.strictEqual(m.page, 1);
  assert.strictEqual(m.hasPrev, false);
  assert.strictEqual(m.hasNext, false);
});

test('pagerModel：非法入参不产生 NaN / Infinity', () => {
  [undefined, null, NaN, 0, -5, 1.9, '7'].forEach((bad) => {
    const m = pagerModel(bad, 100, bad);
    assert.ok(Number.isFinite(m.page) && m.page >= 1, `page=${bad} 应夹成有限正数`);
    assert.ok(Number.isFinite(m.totalPages) && m.totalPages >= 1, `pageSize=${bad} 应退化成 1`);
  });
  // 每页条数非法时退化为 1，而不是除零产生 Infinity
  assert.strictEqual(pagerModel(1, 100, 0).totalPages, 100);
});

test('pagerModel：非数字总数按 0 处理', () => {
  ['abc', {}, undefined].forEach((bad) => {
    const m = pagerModel(1, bad, 24);
    assert.strictEqual(m.total, 0);
    assert.strictEqual(m.totalPages, 1);
  });
});

test('pagerModel：恰好整除时不多出一页', () => {
  // 48 条 / 每页 24 = 正好 2 页，ceil 不能算成 3 页
  assert.strictEqual(pagerModel(1, 48, 24).totalPages, 2);
  assert.strictEqual(pagerModel(2, 48, 24).hasNext, false);
});

test('pagerModel：如实反映案例库的真实规模', () => {
  // 案例库实际 2697 条，此前前端写死 page=1&page_size=60，用户只能看到 2%
  const m = pagerModel(1, 2697, 24);
  assert.strictEqual(m.totalPages, 113);
  assert.strictEqual(m.hasNext, true);
});
