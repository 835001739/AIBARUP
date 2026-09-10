/* 通用序列帧连播播放器（M14 组图 / M15 视频序列帧共用）。
 *
 * 为什么抽成独立组件
 * ------------------
 * 「把一批有序图片像 GIF 一样播起来」这个需求，组图要、视频序列帧也要。
 * 各写一份不仅重复，更糟的是**两边会慢慢漂移**——组图修了「速度档位不匹配
 * 自定义 interval」的 bug，视频那边照样踩。抽成一份，修一次两边都好。
 *
 * 关键实现点（都是从组图连播的真实 bug 里沉淀出来的，别改回去）：
 * 1. **全量预加载后只切 src** —— 逐帧 new Image() 会闪白；
 * 2. **速度档围绕数据本身的 interval 动态生成** —— 写死 2000/1000/500/300
 *    会让「用户设了 320ms」的组图默认选中 2000ms，比预期慢 6 倍；
 * 3. **自定义属性叫 .checkbox 不叫 .control** —— HTMLLabelElement 有只读的
 *    .control，同名自定义属性会被静默忽略；
 * 4. **关闭时解绑 keydown** —— 否则弹窗关了方向键还在抢事件。
 */

(function (root) {
  'use strict';

  var doc = root.document;
  var AIBAR = root.AIBAR || (root.AIBAR = {});

  function ui() {
    return AIBAR.ui || {};
  }

  function icon(name, size) {
    if (AIBAR.icons && AIBAR.icons.get) return AIBAR.icons.get(name, size || 14);
    return '';
  }

  /**
   * 打开连播播放器。
   *
   * @param {Object} opts
   * @param {string} opts.title      弹窗标题（如「连播 · 打斗动作」）
   * @param {Array}  opts.items      [{url, label}]，按数组顺序播放
   * @param {number} opts.interval   每帧毫秒数（默认 300）
   * @param {boolean} opts.loop      是否循环（默认 true）
   * @param {string} opts.emptyHint  无帧时的提示文案
   * @param {boolean} opts.autoplay  打开即播（默认 true；单帧时忽略）
   */
  function open(opts) {
    opts = opts || {};
    var items = opts.items || [];
    if (!items.length) {
      if (ui().toast) {
        ui().toast({ message: opts.emptyHint || '还没有可播放的帧', type: 'warning' });
      }
      return;
    }

    var body = ui().el('div', 'shot-player');

    // 舞台：固定比例容器，切帧时不会因图片尺寸变化而跳动
    var stage = ui().el('div', 'shot-stage');
    var img = ui().el('img', 'shot-stage-img');
    img.alt = (opts.title || '序列帧') + ' 连播';
    stage.appendChild(img);
    body.appendChild(stage);

    var caption = ui().el('p', 'shot-caption clamp-2', items[0].label || '');
    body.appendChild(caption);

    // 进度条：既是进度指示，也是可拖动的定位器
    var slider = ui().el('input', 'shot-slider');
    slider.type = 'range';
    slider.min = '0';
    slider.max = String(Math.max(0, items.length - 1));
    slider.value = '0';
    slider.setAttribute('aria-label', '帧定位');
    body.appendChild(slider);

    var controls = ui().el('div', 'shot-controls');

    var prevBtn = ui().el('button', 'btn btn-secondary btn-sm');
    prevBtn.type = 'button';
    prevBtn.innerHTML = icon('chevronLeft', 16);
    prevBtn.title = '上一帧';
    controls.appendChild(prevBtn);

    var playBtn = ui().el('button', 'btn btn-primary btn-sm shot-play-btn');
    playBtn.type = 'button';
    controls.appendChild(playBtn);

    var nextBtn = ui().el('button', 'btn btn-secondary btn-sm');
    nextBtn.type = 'button';
    nextBtn.innerHTML = icon('chevronRight', 16);
    nextBtn.title = '下一帧';
    controls.appendChild(nextBtn);

    var counter = ui().el('span', 'shot-counter', '1 / ' + items.length);
    controls.appendChild(counter);

    controls.appendChild(ui().el('span', 'grow'));

    // 速度档：值就是 setInterval 的毫秒数，围绕数据本身的 interval 生成
    var baseInterval = Math.max(20, opts.interval || 300);
    var speeds = [
      { label: '0.5×（慢）', value: String(Math.max(80, Math.round(baseInterval * 2))) },
      { label: '1×（原速）', value: String(baseInterval) },
      { label: '2×', value: String(Math.max(20, Math.round(baseInterval / 2))) },
      { label: '4×（快）', value: String(Math.max(20, Math.round(baseInterval / 4))) }
    ];
    var speedSel = ui().select(speeds, String(baseInterval));
    var speedWrap = ui().el('label', 'field field-inline');
    speedWrap.appendChild(ui().el('span', 'field-label', '速度'));
    speedWrap.appendChild(speedSel);
    controls.appendChild(speedWrap);

    var loopWrap = ui().checkbox('循环', opts.loop !== false);
    controls.appendChild(loopWrap);

    body.appendChild(controls);

    /* ---- 播放状态：全量预加载后只切 src，避免逐帧闪白 ---- */
    var idx = 0;
    var timer = null;
    var playing = false;
    var preloaded = items.map(function (it) {
      var im = new root.Image();
      im.src = it.url;
      return im;
    });

    function show(i) {
      idx = ((i % items.length) + items.length) % items.length;
      img.src = preloaded[idx].src;
      caption.textContent = (idx + 1) + ' / ' + items.length + (items[idx].label ? '  ' + items[idx].label : '');
      counter.textContent = (idx + 1) + ' / ' + items.length;
      slider.value = String(idx);
    }

    function stop() {
      playing = false;
      if (timer) { root.clearInterval(timer); timer = null; }
      renderPlayBtn();
    }

    function start() {
      if (items.length < 2) return;
      playing = true;
      renderPlayBtn();
      timer = root.setInterval(function () {
        if (idx >= items.length - 1 && !loopWrap.checkbox.checked) { show(idx); stop(); return; }
        show(idx + 1);
      }, parseInt(speedSel.value, 10) || baseInterval);
    }

    function renderPlayBtn() {
      playBtn.innerHTML = '';
      var ic = ui().el('span', 'btn-icon');
      ic.innerHTML = playing ? icon('close', 16) : icon('play', 16);
      playBtn.appendChild(ic);
      playBtn.appendChild(ui().el('span', null, playing ? '暂停' : '播放'));
    }

    playBtn.addEventListener('click', function () { playing ? stop() : start(); });
    prevBtn.addEventListener('click', function () { stop(); show(idx - 1); });
    nextBtn.addEventListener('click', function () { stop(); show(idx + 1); });
    slider.addEventListener('input', function () { stop(); show(parseInt(slider.value, 10) || 0); });
    speedSel.addEventListener('change', function () {
      // 改速度时保持播放状态：重启定时器即可，不必打断观看
      if (playing) { root.clearInterval(timer); start(); }
    });
    loopWrap.checkbox.addEventListener('change', function () {
      if (playing && loopWrap.checkbox.checked) { root.clearInterval(timer); start(); }
    });

    show(0);
    if (items.length > 1 && opts.autoplay !== false) start();

    var entry = ui().modal({
      title: '连播 · ' + (opts.title || '序列帧'),
      desc: '共 ' + items.length + ' 帧，已全部预加载；空格键播放/暂停，← → 逐帧。',
      body: body,
      size: 'lg',
      actions: [{ label: '关闭', variant: 'ghost', onClick: function () { return true; } }],
      onClose: function () {
        stop();
        if (keyHandler) doc.removeEventListener('keydown', keyHandler);
      }
    });

    // 键盘：空格播放/暂停，左右方向键逐帧
    var keyHandler = function (ev) {
      if (!entry || !entry.root || !entry.root.parentNode) return;
      if (ev.key === ' ') { ev.preventDefault(); playing ? stop() : start(); }
      else if (ev.key === 'ArrowLeft') { ev.preventDefault(); stop(); show(idx - 1); }
      else if (ev.key === 'ArrowRight') { ev.preventDefault(); stop(); show(idx + 1); }
    };
    doc.addEventListener('keydown', keyHandler);
  }

  AIBAR.player = { open: open };

  // 双环境导出：浏览器挂 window.AIBAR，Node 下供 tests/js 断言模块契约
  if (typeof module !== 'undefined' && module.exports) module.exports = AIBAR.player;
})(typeof window !== 'undefined' ? window : globalThis);
