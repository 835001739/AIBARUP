/* 组图模块（M14）接线与契约测试
   运行：node --test tests/js/

   组图是「跨 4 个文件接线」的模块：index.html 的导航/面板/脚本、app.js 的
   PAGES/ORDER/onEnter、groups.js 的导出、pages.css 的专用类。少接任何一处
   都是**静默失败**——页面能起来但 tab 点不动或样式全裸，跑起服务才看得见。
   这里用静态断言把接线钉死，避免「改了 A 忘了 B」的回归。
*/

'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', '..', 'static');

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const INDEX_HTML = read('index.html');
const APP_JS = read('js/app.js');
const PAGES_CSS = read('css/pages.css');

/* ---------------------------------------------------------- 模块导出契约 */

test('groups.js 在无 DOM 的 Node 环境下可安全加载并导出模块接口', () => {
  const groups = require('../../static/js/groups.js');
  ['init', 'onEnter', 'refresh', 'openPlayer'].forEach((fn) => {
    assert.strictEqual(typeof groups[fn], 'function', '缺少导出：' + fn);
  });
  assert.strictEqual(typeof globalThis.AIBAR.groups, 'object', '未挂到 AIBAR.groups');
});

test('init/onEnter 在容器缺失时静默返回，绝不抛异常（app.js 靠 try 兜底但不该靠）', () => {
  const groups = require('../../static/js/groups.js');
  assert.doesNotThrow(() => groups.init());
  assert.doesNotThrow(() => groups.onEnter('groups'));
  assert.doesNotThrow(() => groups.onEnter('comic'));
});

/* ---------------------------------------------------------- index.html 接线 */

test('index.html 注册了组图导航项，且图标存在', () => {
  assert.match(INDEX_HTML, /data-tab="groups"/, '缺少组图导航项');
  const block = INDEX_HTML.match(/<button type="button" class="nav-item" data-tab="groups">[\s\S]*?<\/button>/);
  assert.ok(block, '组图导航项结构异常');
  assert.match(block[0], /data-icon="film"/, '组图导航图标缺失');
  assert.match(block[0], /组图/);
  assert.ok(read('js/icons.js').includes('film:'), 'icons.js 缺少 film 图标');
});

test('index.html 注册了组图面板容器', () => {
  assert.match(INDEX_HTML, /data-panel="groups"/, '缺少组图面板');
  assert.match(INDEX_HTML, /id="groups-root"/, '缺少组图挂载点');
});

test('groups.js 在 app.js 之前加载（否则 app.js boot 时 AIBAR.groups 未定义）', () => {
  const groupsAt = INDEX_HTML.indexOf('/static/js/groups.js');
  const appAt = INDEX_HTML.indexOf('/static/js/app.js');
  assert.ok(groupsAt > -1, '未加载 groups.js');
  assert.ok(groupsAt < appAt, 'groups.js 必须在 app.js 之前');
});

/* ---------------------------------------------------------- app.js 接线 */

test('app.js 的 PAGES 注册了组图标题与说明', () => {
  assert.match(APP_JS, /groups:\s*\{\s*title:\s*'组图'/, 'PAGES 缺少 groups 条目');
  assert.match(APP_JS, /desc:\s*'用强制预设提示词/, 'PAGES.groups 缺少说明');
});

test('app.js 的 ORDER 与 onEnter / init 都覆盖了组图', () => {
  assert.match(APP_JS, /ORDER = \[[^\]]*'groups'/, 'ORDER 未包含 groups（hash 路由会拒绝进入）');
  assert.match(APP_JS, /AIBAR\.groups && AIBAR\.groups\.onEnter/, '缺少 groups.onEnter 调用');
  assert.match(APP_JS, /AIBAR\.groups\.init\(\)/, '缺少 groups.init 调用');
});

/* ---------------------------------------------------------- 样式隔离 */

test('组图专用修饰类定义在 .card-grid 之后（同优先级后定义者胜）', () => {
  const baseAt = PAGES_CSS.indexOf('.card-grid {');
  const modAt = PAGES_CSS.indexOf('.card-grid--shots {');
  assert.ok(baseAt > -1, '缺少 .card-grid 定义');
  assert.ok(modAt > -1, '缺少 .card-grid--shots 定义');
  assert.ok(modAt > baseAt, '.card-grid--shots 必须定义在 .card-grid 之后才能覆盖');
});

test('组图只新增专用类，不改动公共类（避免波及漫画工作室与演员库）', () => {
  // 公共类仍应是原始列宽：260px
  const baseBlock = PAGES_CSS.match(/\.card-grid \{[\s\S]*?\}/);
  assert.ok(baseBlock, '未找到 .card-grid 定义');
  assert.match(baseBlock[0], /minmax\(260px, 1fr\)/, '.card-grid 公共列宽被改动了');
  // 演员库专用修饰类仍应是 320px
  const wideBlock = PAGES_CSS.match(/\.card-grid--wide \{[\s\S]*?\}/);
  assert.ok(wideBlock, '未找到 .card-grid--wide');
  assert.match(wideBlock[0], /minmax\(320px, 1fr\)/, '.card-grid--wide 被改动了');
});

test('连播播放器关键样式齐全（缺一个就会画面跳动或操作看不见）', () => {
  ['.shot-stage', '.shot-stage-img', '.shot-controls', '.shot-slider', '.shot-play-veil', '.shot-actions']
    .forEach((cls) => {
      assert.ok(PAGES_CSS.includes(cls + ' {'), '缺少样式：' + cls);
    });
});

test('动作帧逐帧卡片关键样式齐全', () => {
  [
    '.shot-frame-card', '.shot-frame-thumb', '.shot-frame-thumb-img', '.shot-frame-thumb-empty',
    '.shot-frame-meta', '.shot-frame-head', '.shot-frame-action', '.shot-frame-edit',
    '.shot-frame-error', '.shot-frame-actions', '.shot-frame-btn', '.shot-frame-btn-danger',
    '.shot-frames-toolbar', '.shot-frames-grid', '.shot-bulk-wrap', '.shot-bulk-foot'
  ].forEach((cls) => {
    assert.ok(PAGES_CSS.includes(cls + ' {'), '缺少动作帧样式：' + cls);
  });
});

/* ---------------------------------------------------------- 后端契约 */

test('后端注册了组图与帧的全部路由', () => {
  const routes = fs.readFileSync(path.join(__dirname, '..', '..', 'comic', 'routes.py'), 'utf8');
  [
    '/groups',
    '/groups/<int:group_id>',
    '/groups/<int:group_id>/frames',
    '/groups/<int:group_id>/refresh-prompts',
    '/groups/<int:group_id>/generate',
    '/groups/<int:group_id>/cancel',
    '/groups/<int:group_id>/progress',
    '/groups/<int:group_id>/playlist',
    '/frames/<int:frame_id>',
    '/frames/<int:frame_id>/regenerate'
  ].forEach((rule) => {
    assert.ok(routes.includes('"' + rule + '"'), '缺少路由：' + rule);
  });
});

test('前端调用的每个组图接口在后端都有对应路由（防止两边漂移）', () => {
  const js = read('js/groups.js');
  const routes = fs.readFileSync(path.join(__dirname, '..', '..', 'comic', 'routes.py'), 'utf8');

  // 源码里 URL 多是拼接的（'/api/comic/groups/' + g.id + '/progress'），
  // 先把拼接胶水抽掉，剩下的字面量才是一个完整路径：
  //   1) ' + x + '  → ''（中间拼接）
  //   2) ' + x      → ''（尾部拼接，后面没有配对的引号）
  const collapsed = js
    .replace(/'\s*\+\s*[^'\n]*?\+\s*'/g, '')
    .replace(/'\s*\+\s*[A-Za-z_$][\w.$]*/g, '');

  const normalize = (raw) => {
    let u = raw.split(',')[0].trim();   // 截断到第一个逗号（', payload' 是请求体）
    u = u.replace(/^\/api\/comic/, '');
    // **group_id 与 frame_id 是两种不同路径参数**：/groups/ 路径用 group_id，
    // 其余（含 /frames/...）用 frame_id——否则会把两条独立规则当成同一条匹配。
    if (u.indexOf('/groups/') >= 0) {
      u = u.replace(/\/{2,}/g, '/<int:group_id>/');
      if (u.endsWith('/')) u += '<int:group_id>';
    } else {
      u = u.replace(/\/{2,}/g, '/<int:frame_id>/');
      if (u.endsWith('/')) u += '<int:frame_id>';
    }
    return u;
  };

  // 提取时以引号 / 换行 / 括号为止：URL 里不含这些字符，
  // 而 `api().get(...)` 的右括号正好是表达式结束的自然边界。
  const called = [...collapsed.matchAll(/\/api\/comic\/[^'"\n()]*/g)]
    .map((m) => normalize(m[0]))
    .filter((u) => u.includes('groups') || u.includes('frames'));

  const uniq = [...new Set(called)];
  assert.ok(uniq.length >= 7, '组图接口调用过少：' + uniq.length + ' → ' + uniq.join(', '));
  uniq.forEach((u) => {
    assert.ok(
      routes.includes('"' + u + '"') || routes.includes("'" + u + "'"),
      '前端调用了后端没有的路由：' + u
    );
  });
});

test('后端注册的每个 /groups 与 /frames 路由前端都有调用（抓死端点）', () => {
  const js = read('js/groups.js');
  const routes = fs.readFileSync(path.join(__dirname, '..', '..', 'comic', 'routes.py'), 'utf8');

  // 取出 comic blueprint 下 /groups 与 /frames 相关路由
  const rules = [...routes.matchAll(/@bp\.\w+\(\"((?:\/groups|\/frames)[^\"]*)\"\)/g)]
    .map((m) => m[1]);
  assert.ok(rules.length >= 10, '预期至少 10 条路由，拿到 ' + rules.length);

  // 同样归一化：拼接字面量 → 完整路径。
  // 注意：**group_id 与 frame_id 是两种不同的路径参数**——`/groups/<int:group_id>` 与
  // `/frames/<int:frame_id>` 是两个独立路由，归一化时不能把它们当成同一个占位符。
  const collapsed = js
    .replace(/'\s*\+\s*[^'\n]*?\+\s*'/g, '')
    .replace(/'\s*\+\s*[A-Za-z_$][\w.$]*/g, '');
  const calledSet = new Set(
    [...collapsed.matchAll(/\/api\/comic\/[^'"\n()]*/g)]
      .map((m) => {
        let u = m[0].split(',')[0].trim().replace(/^\/api\/comic/, '');
        // 按段分别替换：`/groups/...` 用 group_id，其余（含 /frames/...）用 frame_id
        if (u.indexOf('/groups/') >= 0) {
          u = u.replace(/\/{2,}/g, '/<int:group_id>/');
          if (u.endsWith('/')) u += '<int:group_id>';
        } else {
          u = u.replace(/\/{2,}/g, '/<int:frame_id>/');
          if (u.endsWith('/')) u += '<int:frame_id>';
        }
        return u;
      })
  );

  const dead = rules.filter((rule) => !calledSet.has(rule));
  assert.deepStrictEqual(dead, [], '后端注册但前端没有入口的死端点：' + dead.join(', '));
});
