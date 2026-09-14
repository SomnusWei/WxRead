/*
 * WxReadAssistant Docker v2.4.0 — 阅读报告渲染（ECharts 5）
 * 按设计 §12.3 的 13 行图表映射表渲染；数据层与渲染层解耦。
 * word_cloud 不引 echarts-wordcloud 插件，退化为 Top 词横向柱状图。
 */
(function () {
  'use strict';

  /* 蓝绿系 12 色循环（对齐 Win 端 CHART_PALETTE 用法，避开粉紫） */
  var PALETTE = [
    '#0d9488', '#2563eb', '#0891b2', '#14b8a6', '#3b82f6', '#06b6d4',
    '#2dd4bf', '#60a5fa', '#0f766e', '#1d4ed8', '#22d3ee', '#67e8f9',
  ];
  var INK = '#64748b';
  var LINE = '#e6edef';
  var GRID = { left: 10, right: 18, top: 44, bottom: 8, containLabel: true };

  var instances = {};

  function hasEcharts() { return typeof window.echarts !== 'undefined'; }

  function chart(id) {
    var dom = document.getElementById(id);
    if (!dom || !hasEcharts()) return null;
    if (!instances[id]) instances[id] = window.echarts.init(dom);
    return instances[id];
  }

  function disposeAll() {
    Object.keys(instances).forEach(function (k) {
      try { instances[k].dispose(); } catch (e) { /* ignore */ }
      delete instances[k];
    });
  }

  function resizeAll() {
    Object.keys(instances).forEach(function (k) {
      try { instances[k].resize(); } catch (e) { /* ignore */ }
    });
  }

  function title(text) {
    return { text: text, left: 12, top: 8, textStyle: { fontSize: 14, fontWeight: 600, color: '#0f766e' } };
  }

  function catAxis(data, opts) {
    return Object.assign({
      type: 'category',
      data: data,
      axisLine: { lineStyle: { color: LINE } },
      axisTick: { show: false },
      axisLabel: { color: INK, fontSize: 11, interval: 0 },
    }, opts || {});
  }

  function valAxis(name, opts) {
    return Object.assign({
      type: 'value',
      name: name || '',
      nameTextStyle: { color: INK, fontSize: 11 },
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: INK, fontSize: 11 },
      splitLine: { lineStyle: { color: LINE, type: 'dashed' } },
    }, opts || {});
  }

  function emptyTip(id, t, msg) {
    var c = chart(id);
    if (!c) return;
    c.setOption({
      color: PALETTE,
      title: [
        t,
        { text: msg || '暂无数据', left: 'center', top: 'middle',
          textStyle: { fontSize: 13, color: '#94a3b8', fontWeight: 400 } },
      ],
    }, true);
  }

  function truncate(s, n) {
    s = String(s == null ? '' : s);
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  function toHours(sec) { return Math.round((Number(sec) || 0) / 36) / 100; }
  function toMinutes(sec) { return Math.round((Number(sec) || 0) / 60); }

  function fmtDur(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0));
    var h = Math.floor(sec / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (h > 0) return h + ' 小时 ' + m + ' 分';
    return m + ' 分';
  }

  /* ---------- 1. 近 24 月趋势：柱状 ---------- */
  function renderMonthly(d) {
    var id = 'chart-monthly', rows = d.monthly || [];
    if (!rows.length) return emptyTip(id, title('近 24 月阅读趋势'));
    var c = chart(id);
    c.setOption({
      color: PALETTE,
      title: title('近 24 月阅读趋势（小时）'),
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var row = rows[ps[0].dataIndex];
          return row.ym + '<br/>' + ps[0].marker + '时长：' + fmtDur(row.seconds)
            + '<br/>有效天数：' + (row.days || 0) + ' 天';
        },
      },
      grid: GRID,
      xAxis: catAxis(rows.map(function (r) { return (r.ym || '').slice(2); }),
        { axisLabel: { color: INK, fontSize: 10, rotate: 45, interval: 1 } }),
      yAxis: valAxis('小时'),
      series: [{
        name: '阅读时长', type: 'bar', barMaxWidth: 18,
        itemStyle: { borderRadius: [4, 4, 0, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#2dd4bf' }, { offset: 1, color: '#0d9488' }]) },
        data: rows.map(function (r) { return toHours(r.seconds); }),
      }],
    }, true);
  }

  /* ---------- 2. 周节律：柱状 ---------- */
  function renderWeekly(d) {
    var id = 'chart-weekly', rows = d.weekly_rhythm || [];
    if (!rows.length) return emptyTip(id, title('周节律'));
    var c = chart(id);
    c.setOption({
      color: ['#2563eb'],
      title: title('周节律（周一～周日 · 分钟）'),
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var row = rows[ps[0].dataIndex];
          return row.weekday_zh + '<br/>' + ps[0].marker + '时长：' + fmtDur(row.seconds)
            + '<br/>天数：' + (row.day_count || 0);
        },
      },
      grid: GRID,
      xAxis: catAxis(rows.map(function (r) { return r.weekday_zh; })),
      yAxis: valAxis('分钟'),
      series: [{
        name: '分钟', type: 'bar', barMaxWidth: 30,
        itemStyle: { borderRadius: [5, 5, 0, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#60a5fa' }, { offset: 1, color: '#2563eb' }]) },
        data: rows.map(function (r) { return toMinutes(r.seconds); }),
      }],
    }, true);
  }

  /* ---------- 3. 小时节律：面积折线 ---------- */
  function renderDaily(d) {
    var id = 'chart-daily', rows = d.daily_rhythm || [];
    if (!rows.length) return emptyTip(id, title('小时节律'));
    var c = chart(id);
    c.setOption({
      color: ['#0891b2'],
      title: title('一天 24 小时阅读节律（分钟）'),
      tooltip: { trigger: 'axis' },
      grid: { left: 10, right: 24, top: 44, bottom: 8, containLabel: true },
      xAxis: catAxis(rows.map(function (r) { return String(r.hour) + ':00'; }),
        { axisLabel: { color: INK, fontSize: 10, interval: 1 } }),
      yAxis: valAxis('分钟'),
      series: [{
        name: '分钟', type: 'line', smooth: true, showSymbol: false,
        areaStyle: { color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(8,145,178,.35)' }, { offset: 1, color: 'rgba(8,145,178,.02)' }]) },
        lineStyle: { width: 2.5 },
        data: rows.map(function (r) { return toMinutes(r.seconds); }),
      }],
    }, true);
  }

  /* ---------- 4. 读最久 Top10：横向条形 ---------- */
  function renderLongest(d) {
    var id = 'chart-longest', rows = (d.read_longest || []).slice(0, 10);
    if (!rows.length) return emptyTip(id, title('读得最久 Top10'));
    var ordered = rows.slice().reverse();
    var c = chart(id);
    c.setOption({
      color: ['#0d9488'],
      title: title('读得最久 Top10（分钟）'),
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var row = ordered[ps[0].dataIndex];
          return '<b>' + row.title + '</b>' + (row.author ? ' · ' + row.author : '')
            + '<br/>' + ps[0].marker + fmtDur(row.seconds)
            + '<br/>进度：' + (row.percent != null ? row.percent + '%' : '-');
        },
      },
      grid: { left: 10, right: 26, top: 44, bottom: 8, containLabel: true },
      xAxis: valAxis('分钟'),
      yAxis: catAxis(ordered.map(function (r) { return truncate(r.title, 12); }),
        { axisLabel: { color: INK, fontSize: 11 } }),
      series: [{
        name: '分钟', type: 'bar', barMaxWidth: 16,
        itemStyle: { borderRadius: [0, 4, 4, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#0f766e' }, { offset: 1, color: '#2dd4bf' }]) },
        label: { show: true, position: 'right', color: INK, fontSize: 10 },
        data: ordered.map(function (r) { return toMinutes(r.seconds); }),
      }],
    }, true);
  }

  /* ---------- 5. 分类占比：环形饼 ---------- */
  function renderCategories(d) {
    var id = 'chart-categories', rows = d.categories || [];
    if (!rows.length) return emptyTip(id, title('分类占比'));
    var c = chart(id);
    c.setOption({
      color: PALETTE,
      title: title('分类占比'),
      tooltip: {
        trigger: 'item',
        formatter: function (p) {
          var row = rows[p.dataIndex];
          return '<b>' + row.name + '</b><br/>' + p.marker + fmtDur(row.seconds)
            + '（' + (row.percent != null ? row.percent : p.percent) + '%）<br/>藏书：' +
            (row.books_count != null ? row.books_count : '-') + ' 本';
        },
      },
      legend: { type: 'scroll', bottom: 0, textStyle: { color: INK, fontSize: 11 } },
      series: [{
        name: '分类', type: 'pie', radius: ['38%', '62%'], center: ['50%', '48%'],
        itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 4 },
        label: { formatter: '{b}\n{d}%', color: INK, fontSize: 10 },
        data: rows.map(function (r) { return { name: r.name, value: Math.round(r.seconds / 60) }; }),
      }],
    }, true);
  }

  /* ---------- 6. Top 作者：横向条形 ---------- */
  function renderAuthors(d) {
    var id = 'chart-authors', rows = (d.top_authors || []).slice(0, 10);
    if (!rows.length) return emptyTip(id, title('Top 作者'));
    var ordered = rows.slice().reverse();
    var c = chart(id);
    c.setOption({
      color: ['#2563eb'],
      title: title('Top 作者（藏书数）'),
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var row = ordered[ps[0].dataIndex];
          return '<b>' + row.name + '</b><br/>' + ps[0].marker + '藏书：' + row.books_count + ' 本'
            + (row.seconds != null ? '<br/>时长：' + fmtDur(row.seconds) : '');
        },
      },
      grid: { left: 10, right: 30, top: 44, bottom: 8, containLabel: true },
      xAxis: valAxis('本'),
      yAxis: catAxis(ordered.map(function (r) { return truncate(r.name, 10); })),
      series: [{
        name: '本', type: 'bar', barMaxWidth: 15,
        itemStyle: { borderRadius: [0, 4, 4, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#1d4ed8' }, { offset: 1, color: '#60a5fa' }]) },
        label: { show: true, position: 'right', color: INK, fontSize: 10 },
        data: ordered.map(function (r) { return r.books_count || 0; }),
      }],
    }, true);
  }

  /* ---------- 7. Top 出版社：横向条形 ---------- */
  function renderPublishers(d) {
    var id = 'chart-publishers', rows = (d.top_publishers || []).slice(0, 10);
    if (!rows.length) return emptyTip(id, title('Top 出版社'));
    var ordered = rows.slice().reverse();
    var c = chart(id);
    c.setOption({
      color: ['#0891b2'],
      title: title('Top 出版社（藏书数）'),
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 10, right: 30, top: 44, bottom: 8, containLabel: true },
      xAxis: valAxis('本'),
      yAxis: catAxis(ordered.map(function (r) { return truncate(r.name, 12); })),
      series: [{
        name: '本', type: 'bar', barMaxWidth: 15,
        itemStyle: { borderRadius: [0, 4, 4, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#155e75' }, { offset: 1, color: '#22d3ee' }]) },
        label: { show: true, position: 'right', color: INK, fontSize: 10 },
        data: ordered.map(function (r) { return r.books_count || 0; }),
      }],
    }, true);
  }

  /* ---------- 8/9. 书架双环图（状态 + 可见性，中心显示 _shelf_total） ---------- */
  function renderShelfPie(id, t, rows, total, centerText) {
    if (!rows.length) return emptyTip(id, title(t));
    var c = chart(id);
    c.setOption({
      color: ['#16a34a', '#2563eb', '#94a3b8', '#0d9488', '#0891b2'],
      title: [
        title(t),
        { text: String(total != null ? total : ''), left: 'center', top: '38%',
          textStyle: { fontSize: 24, fontWeight: 700, color: '#0f766e' } },
        { text: centerText, left: 'center', top: '55%',
          textStyle: { fontSize: 11, color: INK, fontWeight: 400 } },
      ],
      tooltip: { trigger: 'item', formatter: '{b}：{c} 本（{d}%）' },
      legend: { type: 'scroll', bottom: 0, textStyle: { color: INK, fontSize: 11 } },
      series: [{
        name: t, type: 'pie', radius: ['52%', '70%'], center: ['50%', '48%'],
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: { show: false }, labelLine: { show: false },
        emphasis: { scale: true, scaleSize: 4 },
        data: rows.map(function (r) { return { name: r.label, value: r.count }; }),
      }],
    }, true);
  }

  /* ---------- 10. 进度分布 6 档：柱状 ---------- */
  function renderProgressHist(d) {
    var id = 'chart-progress-hist', rows = d.progress_hist || [];
    if (!rows.length) return emptyTip(id, title('进度分布'));
    var c = chart(id);
    c.setOption({
      color: ['#14b8a6'],
      title: title('进度分布（6 档）'),
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) { return ps[0].axisValue + '% 档：' + ps[0].data + ' 本'; } },
      grid: GRID,
      xAxis: catAxis(rows.map(function (r) { return r.bucket; })),
      yAxis: valAxis('本'),
      series: [{
        name: '本', type: 'bar', barMaxWidth: 34,
        itemStyle: { borderRadius: [5, 5, 0, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: '#2dd4bf' }, { offset: 1, color: '#0f766e' }]) },
        label: { show: true, position: 'top', color: INK, fontSize: 10 },
        data: rows.map(function (r) { return r.count || 0; }),
      }],
    }, true);
  }

  /* ---------- 11. 进度 × 章节数：散点 ---------- */
  function renderProgressScatter(d) {
    var id = 'chart-progress-scatter', rows = d.progress_scatter || [];
    if (!rows.length) return emptyTip(id, title('进度 × 章节数散点'));
    var c = chart(id);
    c.setOption({
      color: ['#2563eb'],
      title: title('进度 × 章节数散点（悬浮看书名）'),
      tooltip: {
        trigger: 'item',
        formatter: function (p) {
          return '<b>' + (p.data[2] || '') + '</b><br/>进度：' + p.data[0]
            + '%<br/>章节数：' + p.data[1];
        },
      },
      grid: { left: 10, right: 24, top: 44, bottom: 24, containLabel: true },
      xAxis: Object.assign(valAxis('进度 %', { min: 0, max: 100 }),
        { splitLine: { lineStyle: { color: LINE, type: 'dashed' } } }),
      yAxis: valAxis('章节数'),
      series: [{
        name: '书籍', type: 'scatter', symbolSize: 9,
        itemStyle: { color: '#2563eb', opacity: .72, borderColor: '#1d4ed8' },
        data: rows.map(function (r) { return [r.progress || 0, r.chapter_count || 0, r.title || '']; }),
      }],
    }, true);
  }

  /* ---------- 12a. 笔记 Top10：横向条形 ---------- */
  function renderNoteStats(d) {
    var id = 'chart-notes-rank', rows = (d.note_stats || []).slice(0, 10);
    if (!rows.length) return emptyTip(id, title('笔记 Top10'));
    var ordered = rows.slice().reverse();
    var c = chart(id);
    c.setOption({
      color: ['#0d9488'],
      title: title('笔记 Top10（条）'),
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var row = ordered[ps[0].dataIndex];
          return '<b>' + row.book_title + '</b><br/>' + ps[0].marker + '笔记：' + row.count + ' 条';
        } },
      grid: { left: 10, right: 30, top: 44, bottom: 8, containLabel: true },
      xAxis: valAxis('条'),
      yAxis: catAxis(ordered.map(function (r) { return truncate(r.book_title, 12); })),
      series: [{
        name: '条', type: 'bar', barMaxWidth: 15,
        itemStyle: { borderRadius: [0, 4, 4, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#0f766e' }, { offset: 1, color: '#5eead4' }]) },
        label: { show: true, position: 'right', color: INK, fontSize: 10 },
        data: ordered.map(function (r) { return r.count || 0; }),
      }],
    }, true);
  }

  /* ---------- 12b. 笔记 90 天时间线：折线 ---------- */
  function renderNotesTimeline(d) {
    var id = 'chart-notes-timeline', rows = d.notes_timeline || [];
    if (!rows.length) return emptyTip(id, title('笔记近 90 天时间线'));
    var c = chart(id);
    c.setOption({
      color: ['#0891b2'],
      title: title('笔记近 90 天时间线'),
      tooltip: { trigger: 'axis',
        formatter: function (ps) { return rows[ps[0].dataIndex].date + '<br/>笔记：' + ps[0].data + ' 条'; } },
      grid: { left: 10, right: 24, top: 44, bottom: 24, containLabel: true },
      xAxis: catAxis(rows.map(function (r) { return String(r.date || '').slice(5); }),
        { axisLabel: { color: INK, fontSize: 10, interval: 6 } }),
      yAxis: valAxis('条', { minInterval: 1 }),
      series: [{
        name: '条', type: 'line', smooth: true, showSymbol: false,
        areaStyle: { color: new window.echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(8,145,178,.32)' }, { offset: 1, color: 'rgba(8,145,178,.02)' }]) },
        lineStyle: { width: 2.5 },
        data: rows.map(function (r) { return r.count || 0; }),
      }],
    }, true);
  }

  /* ---------- 13. 词云退化：高频词横向柱状（不引 wordcloud 插件） ---------- */
  function renderWordCloud(d) {
    var id = 'chart-wordcloud', rows = (d.word_cloud || []).slice(0, 30);
    if (!rows.length) return emptyTip(id, title('高频词 Top30'));
    var ordered = rows.slice().reverse();
    var c = chart(id);
    c.setOption({
      color: ['#0d9488'],
      title: title('高频词 Top30（按权重，词云的柱状退化视图）'),
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: function (ps) {
          var row = ordered[ps[0].dataIndex];
          return '<b>' + row.word + '</b><br/>权重：' + row.weight;
        } },
      grid: { left: 10, right: 40, top: 44, bottom: 8, containLabel: true },
      xAxis: valAxis('权重'),
      yAxis: catAxis(ordered.map(function (r) { return r.word; }),
        { axisLabel: { color: INK, fontSize: 11 } }),
      series: [{
        name: '权重', type: 'bar', barMaxWidth: 13,
        itemStyle: { borderRadius: [0, 4, 4, 0],
          color: new window.echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: '#134e4a' }, { offset: .55, color: '#0d9488' }, { offset: 1, color: '#5eead4' }]) },
        label: { show: true, position: 'right', color: INK, fontSize: 10 },
        data: ordered.map(function (r) { return r.weight || 0; }),
      }],
    }, true);
  }

  function renderAll(data) {
    if (!hasEcharts() || !data) return;
    var jobs = [
      function () { renderMonthly(data); },
      function () { renderWeekly(data); },
      function () { renderDaily(data); },
      function () { renderLongest(data); },
      function () { renderCategories(data); },
      function () { renderAuthors(data); },
      function () { renderPublishers(data); },
      function () {
        renderShelfPie('chart-shelf-status', '书架 · 阅读状态',
          data.shelf_pie || [], data._shelf_total, '本藏书');
      },
      function () {
        var visRows = data.shelf_visibility || [];
        var visTotal = visRows.reduce(function (s, r) { return s + (r.count || 0); }, 0);
        renderShelfPie('chart-shelf-visibility', '书架 · 可见性',
          visRows, data._shelf_total != null ? data._shelf_total : visTotal, '本藏书');
      },
      function () { renderProgressHist(data); },
      function () { renderProgressScatter(data); },
      function () { renderNoteStats(data); },
      function () { renderNotesTimeline(data); },
      function () { renderWordCloud(data); },
    ];
    jobs.forEach(function (fn) {
      try { fn(); } catch (e) {
        /* 单图失败不影响其余图表 */
        if (window && window.console) console.warn('报告图表渲染失败：', e);
      }
    });
    resizeAll();
  }

  window.WxReport = {
    renderAll: renderAll,
    resizeAll: resizeAll,
    disposeAll: disposeAll,
  };
})();
