/*
 * WxReadAssistant Docker v2.4.0 — Web 面板主逻辑（Alpine.js 3 单根组件）
 * 纯静态、无构建工具；所有请求走同源相对路径，统一 Bearer Token。
 */

/* 配置键中文标签与说明（未列出的键回退为键名 + 通用说明） */
const FIELD_META = {
  reading: {
    min_hours: { label: '每日最少小时数', hint: '每日 00:00 在上下限之间随机生成当日目标（小时，步进 0.5，改动次日生效）' },
    max_hours: { label: '每日最多小时数', hint: '必须 ≥ 最少小时数（小时，步进 0.5）' },
    min_interval_sec: { label: '单页停留下限', hint: '两次翻页请求之间的最小间隔（秒）' },
    max_interval_sec: { label: '单页停留上限', hint: '两次翻页请求之间的最大间隔（秒）' },
    chapter_read_min: { label: '单章最少阅读次数', hint: '同一章节上报的最少次数' },
    chapter_read_max: { label: '单章最多阅读次数', hint: '同一章节上报的最多次数' },
    startup_delay_min_sec: { label: '启动随机延迟下限', hint: '点启动后到首次请求的随机延迟（秒）' },
    startup_delay_max_sec: { label: '启动随机延迟上限', hint: '点启动后到首次请求的随机延迟（秒）' },
    health_check_first_min: { label: '首次健康巡检间隔', hint: '启动后首次 Cookie/风控巡检（分钟）' },
    health_check_min: { label: '健康巡检间隔', hint: '之后每隔多少分钟巡检一次（调度器启动时缓存，需重启调度器生效）' },
    long_rest_every: { label: '长休息频次', hint: '每阅读 N 次进入一次长休息' },
    long_rest_min_sec: { label: '长休息下限', hint: '长休息最短时长（秒）' },
    long_rest_max_sec: { label: '长休息上限', hint: '长休息最长时长（秒）' },
    fail_cooldown_min_sec: { label: '失败冷却下限', hint: '请求失败后的随机冷却下限（秒）' },
    fail_cooldown_max_sec: { label: '失败冷却上限', hint: '请求失败后的随机冷却上限（秒）' },
  },
  risk: {
    empty_read_threshold: { label: '空响应软风控阈值', hint: 'read 端点连续返回空 {} 达到此次数即判定软风控' },
    fail_streak_threshold: { label: '第一级连续失败阈值', hint: '连续失败达此次数：告警 + 指数冷却，冷却结束后自动恢复' },
    fail_pause_threshold: { label: '第二级自动暂停阈值', hint: '连续失败达此次数则真正暂停（不自动恢复）；0 = 禁用第二级；须为 0 或 ≥ 第一级阈值' },
    soft_cooldown_min: { label: '软风控冷却基准时长', hint: '命中软风控后停止上报的基准时长（分钟）' },
    cooldown_max_min: { label: '冷却退避上限', hint: '指数冷却的最大时长（分钟）' },
    alert_cooldown_min: { label: '告警推送节流', hint: '同类风控推送的最小间隔（分钟）' },
  },
  skill: {
    api_key: { label: 'Skill API Key', hint: 'wrk- 前缀的 Bearer Token；修改后热生效，无需重启' },
    version: { label: 'Skill 协议版本', hint: '默认 1.0.5' },
    summary_cache_ttl: { label: '阅读统计缓存时长', hint: 'Skill 阅读统计缓存秒数（默认 180）' },
    refresh_interval_min: { label: 'Skill 数据刷新间隔', hint: 'Scheduler 每轮动态读取（分钟），修改后实时生效' },
  },
  push: {
    wxpusher_spt: { label: 'WxPusher SPT（文本通道）', hint: '极简推送凭证，用于文字通知；扫码获取：wxpusher.zjiecode.com 首页' },
    wxpusher_app_token: { label: 'WxPusher AppToken（图片通道）', hint: '标准推送凭证 AT_xxx；SPT 只能发文字，推送登录二维码图片必须配置：wxpusher.zjiecode.com/admin 创建应用后获取' },
    wxpusher_uid: { label: 'WxPusher UID（图片通道）', hint: '你的微信 UID（UID_xxx）：关注公众号「wxpusher」→ 我的 → 我的UID；注意需先扫码关注你创建的应用才能收到图片' },
    notify_daily_start: { label: '每日首次开始通知', hint: '每日首次开始阅读时推送（含今日目标），当日仅一次' },
    notify_cookie_fail: { label: 'Cookie 失效通知', hint: 'Cookie 硬失效/软失效时推送，1 小时节流' },
    notify_login_qr: { label: '失效后自动推扫码二维码', hint: '检测到 Cookie 失效时自动唤起扫码，把登录二维码推送到微信，手机长按识别即可重新登录，登录后自动恢复运行；图片需配置上方 AppToken/UID，仅配 SPT 时只收文字提醒' },
    auto_qr_cooldown_min: { label: '自动扫码冷却(分钟)', hint: '两次自动扫码推送的最小间隔，防止登录态抖动时消息轰炸（默认 30）' },
    notify_daily_done: { label: '任务完成通知', hint: '今日目标达成时推送（今日已读/目标），当日仅一次' },
    notify_login_success: { label: '登录成功通知', hint: '扫码登录成功后推送（Cookie 数量 / wr_skey 长度），默认关闭' },
    notify_risk_control: { label: '阅读风控告警', hint: '空响应 / 连续失败进入冷却时推送' },
    notify_auto_pause: { label: '自动暂停通知', hint: '连续失败达第二级阈值被自动暂停时立即推送，提示需手动恢复' },
  },
  app: {
    log_retention_days: { label: '日志保留天数', hint: '默认 7，可选 3 / 7 / 14 / 30；下一次清理周期生效' },
  },
};

const CONFIG_SECTIONS = ['reading', 'risk', 'skill', 'push', 'app'];
const PASSWORD_KEYS = { 'skill.api_key': true, 'push.wxpusher_spt': true, 'push.wxpusher_app_token': true };

function wxPanel() {
  return {
    /* ---------- 鉴权 ---------- */
    token: '',
    needToken: false,
    tokenInput: '',

    /* ---------- 全局 UI ---------- */
    tab: 'overview',
    toasts: [],
    _tid: 0,
    version: '',

    /* ---------- 授权 ---------- */
    license: { licensed: false, expired: false, trial: true, minutes_left: 0, hours_left: 0, first_run_iso: '', masked_code: '', adv_mode: false },
    licenseCode: '',
    activating: false,

    /* ---------- 调度器 / KPI ---------- */
    status: null,
    nowTs: Date.now(),
    lastStatusAt: '',
    acting: {},

    /* ---------- 登录 ---------- */
    login: { logged_in: false, wr_vid: '', wr_skey_prefix: '', wr_skey_len: 0 },
    qr: { busy: false, sid: '', img: '', msg: '', phase: '', ended: false },
    qrES: null,
    injectOpen: false,
    injectText: '',
    injectBusy: false,

    /* ---------- 同步 ---------- */
    shelfSync: { busy: false, idx: 0, total: 0, logs: [] },
    statsBusy: false,
    progressBusy: false,

    /* ---------- 书架 ---------- */
    shelfKw: '',
    shelfFilter: 'all',
    shelfPage: 1,
    shelfPageSize: 50,
    shelfItems: [],
    shelfTotal: 0,
    shelfLoading: false,
    shelfTimer: null,
    blackOpen: false,
    blackItems: [],
    blackLoaded: false,
    blackLoading: false,
    maintBusy: false,

    /* ---------- 配置 ---------- */
    cfg: null,
    defaults: null,
    form: {},
    cfgSnap: {},
    savingCfg: false,
    showKey: {},
    testSkill: { busy: false, msg: '', ok: null },
    testPush: { busy: false, msg: '', ok: null },

    /* ---------- 日志 ---------- */
    logFiles: [],
    logDate: '',
    logLines: [],
    logLevel: 'INFO',
    follow: true,
    logES: null,
    logLoading: false,
    logEof: false,
    logOffset: 0,
    logTotal: 0,

    /* ---------- 报告 ---------- */
    report: null,
    reportLoading: false,
    reportRefreshing: false,
    reportLoaded: false,

    /* ---------- 定时器 ---------- */
    _tick: null,
    _poll: null,
    _licPoll: null,

    /* ================================================================
     * 初始化
     * ================================================================ */
    async init() {
      this.token = localStorage.getItem('wxread_token') || '';
      this.$watch('tab', (v) => this.onTabChange(v));
      window.addEventListener('resize', () => {
        if (this.tab === 'report' && window.WxReport) window.WxReport.resizeAll();
      });

      await Promise.allSettled([
        this.loadVersion(),
        this.loadLicense(),
        this.refreshStatus(),
        this.refreshLogin(),
        this.loadConfig(),
        this.loadLogFiles(),
      ]);
      this.loadShelf();

      this._tick = setInterval(() => { this.nowTs = Date.now(); }, 1000);
      this._poll = setInterval(() => { this.refreshStatus(true); }, 3000);
      this._licPoll = setInterval(() => { this.loadLicense(true); }, 30000);
    },

    onTabChange(v) {
      if (v === 'logs') {
        if (!this.logDate && this.logFiles.length) this.selectLogDate(this.logFiles[0].date);
        else if (this.isLogToday() && !this.logES) this.openTodayLog();
      } else {
        this.closeLogStream();
      }
      if (v === 'report' && !this.reportLoaded && !this.reportLoading) this.loadReport(false);
      if (v === 'report' && this.reportLoaded && window.WxReport) {
        this.$nextTick(() => setTimeout(() => window.WxReport.resizeAll(), 60));
      }
    },

    /* ================================================================
     * 通用：fetch 封装 / Token / Toast / 格式化
     * ================================================================ */
    async api(path, opts = {}) {
      const headers = {};
      if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
      if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
      let res;
      try {
        res = await fetch(path, {
          method: opts.method || 'GET',
          headers,
          body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
          signal: opts.signal,
        });
      } catch (e) {
        throw new Error('网络错误：' + e.message);
      }
      if (res.status === 401) {
        this.needToken = true;
        throw new Error('未授权，请输入访问令牌');
      }
      let json = null;
      try { json = await res.json(); } catch (e) { /* 非 JSON */ }
      if (!res.ok || !json) {
        const e = new Error((json && json.msg) || ('请求失败（HTTP ' + res.status + '）'));
        e.code = json && json.code;
        throw e;
      }
      if (json.ok === false) {
        const e = new Error(json.msg || '请求失败');
        e.code = json.code;
        throw e;
      }
      return json;
    },

    sseUrl(path) {
      const sep = path.indexOf('?') >= 0 ? '&' : '?';
      return path + sep + 'access_token=' + encodeURIComponent(this.token || '');
    },

    saveToken() {
      const t = (this.tokenInput || '').trim();
      if (!t) { this.toast('请输入访问令牌', 'err'); return; }
      localStorage.setItem('wxread_token', t);
      location.reload();
    },

    toast(msg, type = 'info') {
      const id = ++this._tid;
      this.toasts.push({ id, msg: String(msg || ''), type });
      setTimeout(() => {
        this.toasts = this.toasts.filter((x) => x.id !== id);
      }, 3000);
    },

    fmtDur(sec) {
      sec = Math.max(0, Math.floor(Number(sec) || 0));
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      if (h > 0) return h + ' 小时 ' + m + ' 分';
      return m + ' 分';
    },

    fmtTrialLeft() {
      const mins = Math.max(0, parseInt(this.license.minutes_left, 10) || 0);
      if (mins >= 60) return Math.floor(mins / 60) + ' 小时 ' + (mins % 60) + ' 分';
      return mins + ' 分钟';
    },

    fmtSize(n) {
      n = Number(n) || 0;
      if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
      if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
      return n + ' B';
    },

    esc(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    },

    /* ================================================================
     * 版本 / 授权
     * ================================================================ */
    async loadVersion() {
      try {
        const r = await this.api('/api/version');
        this.version = (r.data && r.data.version) || '';
      } catch (e) { /* 忽略 */ }
    },

    async loadLicense(silent) {
      try {
        const r = await this.api('/api/license/status');
        this.license = Object.assign({}, this.license, r.data || {});
      } catch (e) {
        if (!silent && e.message.indexOf('未授权') < 0) this.toast('授权状态获取失败：' + e.message, 'err');
      }
    },

    get licenseGated() {
      return !!(this.license.expired && !this.license.licensed);
    },

    async activateLicense() {
      const code = (this.licenseCode || '').trim();
      if (!code) { this.toast('请输入注册码', 'err'); return; }
      this.activating = true;
      try {
        const r = await this.api('/api/license/activate', { method: 'POST', body: { code } });
        this.toast(r.msg || '激活成功', 'ok');
        this.licenseCode = '';
        await Promise.all([this.loadLicense(), this.refreshStatus()]);
      } catch (e) {
        this.toast(e.message, 'err');
      } finally {
        this.activating = false;
      }
    },

    /* ================================================================
     * 调度器状态 / 操作
     * ================================================================ */
    async refreshStatus(silent) {
      try {
        const r = await this.api('/api/scheduler/status');
        this.status = r.data;
        this.lastStatusAt = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      } catch (e) {
        if (!silent && e.message.indexOf('未授权') < 0) this.toast('状态获取失败：' + e.message, 'err');
      }
    },

    statSec(key) {
      return (this.status && this.status.stats && this.status.stats[key]) || 0;
    },

    nextRunText() {
      if (!this.status) return '—';
      if (this.status.auto_paused) return '等待人工恢复';
      if (!this.status.running) return '—';
      if (this.status.paused) return '已暂停';
      if (this.status.next_run_at == null) return '即将';
      const s = Math.max(0, Math.floor(this.status.next_run_at - this.nowTs / 1000));
      if (s <= 1) return '即将';
      const mm = String(Math.floor(s / 60)).padStart(2, '0');
      const ss = String(s % 60).padStart(2, '0');
      return mm + ':' + ss;
    },

    async schedAction(act) {
      if (this.acting[act]) return;
      this.acting[act] = true;
      try {
        const r = await this.api('/api/scheduler/' + act, { method: 'POST' });
        this.toast(r.msg || '操作成功', 'ok');
      } catch (e) {
        /* 后端 403/409 返回 {code,msg}（如 LICENSE_REQUIRED / AUTO_PAUSED / NO_SHELF），直接展示 msg */
        this.toast(e.message, 'err');
      } finally {
        this.acting[act] = false;
        await this.refreshStatus(true);
      }
    },

    async regeneratePlan() {
      if (this.acting.regen) return;
      this.acting.regen = true;
      try {
        const r = await this.api('/api/scheduler/regenerate-plan', { method: 'POST' });
        this.toast(r.msg || '今日目标将重新生成', 'ok');
        await this.refreshStatus(true);
      } catch (e) {
        this.toast(e.message, 'err');
      } finally {
        this.acting.regen = false;
      }
    },

    /* ================================================================
     * 登录态 / 扫码 SSE / Cookie 粘贴
     * ================================================================ */
    async refreshLogin() {
      try {
        const r = await this.api('/api/login/status');
        this.login = Object.assign({}, this.login, r.data || {});
      } catch (e) { /* 忽略 */ }
    },

    closeQRES() {
      if (this.qrES) { try { this.qrES.close(); } catch (e) {} this.qrES = null; }
    },

    cancelQR() {
      this.closeQRES();
      this.qr.busy = false;
      this.qr.ended = true;
      this.qr.phase = '';
      this.qr.msg = '已取消，可重新获取二维码';
    },

    async startQR() {
      if (this.qr.busy) return;
      this.closeQRES();
      this.qr = { busy: true, sid: '', img: '', msg: '正在启动扫码会话（首次启动浏览器约需数秒）…', phase: 'starting', ended: false };
      try {
        const r = await this.api('/api/login/qr', { method: 'POST' });
        this.qr.sid = r.session_id;
        const es = new EventSource(this.sseUrl('/api/login/qr/stream?session_id=' + encodeURIComponent(r.session_id)));
        this.qrES = es;
        es.onmessage = (e) => {
          let ev;
          try { ev = JSON.parse(e.data); } catch (err) { return; }
          this.onQREvent(ev);
        };
        es.onerror = () => {
          if (es.readyState === EventSource.CLOSED && !this.qr.ended) {
            this.qr.ended = true;
            if (this.qr.phase !== 'login_ok') {
              this.qr.msg = '连接已断开，请重新获取二维码';
              this.toast('扫码连接已断开', 'err');
            }
          }
        };
      } catch (e) {
        this.qr.ended = true;
        this.qr.msg = e.message;
        this.toast(e.message, 'err');
      } finally {
        this.qr.busy = false;
      }
    },

    onQREvent(ev) {
      switch (ev.event) {
        case 'starting':
          this.qr.phase = 'starting';
          this.qr.msg = ev.msg || '正在启动浏览器…';
          break;
        case 'status':
          this.qr.phase = 'status';
          this.qr.msg = ev.msg || '准备中…';
          break;
        case 'qr':
          this.qr.phase = 'qr';
          this.qr.img = ev.image || '';
          this.qr.msg = '二维码已生成，请使用微信「扫一扫」';
          break;
        case 'login_ok':
          this.qr.phase = 'login_ok';
          this.qr.msg = '扫码成功，正在写入登录态…';
          this.toast('微信确认登录成功', 'ok');
          break;
        case 'applied':
          this.qr.ended = true;
          this.qr.msg = ev.scheduler_started ? '登录成功，调度器已自动启动' : '登录成功（调度器未自动启动，可手动点启动）';
          this.toast(this.qr.msg, 'ok');
          this.closeQRES();
          this.refreshLogin();
          this.refreshStatus();
          break;
        case 'error':
          this.qr.ended = true;
          this.qr.msg = ev.msg || '扫码失败';
          this.toast(this.qr.msg, 'err');
          this.closeQRES();
          break;
        case 'expired':
          this.qr.ended = true;
          this.qr.msg = ev.msg || '二维码已过期，请重新获取';
          this.toast(this.qr.msg, 'err');
          this.closeQRES();
          break;
        default:
          if (ev.msg) this.qr.msg = ev.msg;
      }
    },

    async submitInject() {
      const raw = (this.injectText || '').trim();
      if (!raw) { this.toast('请先粘贴 Cookie JSON', 'err'); return; }
      let obj;
      try {
        obj = JSON.parse(raw);
      } catch (e) {
        this.toast('JSON 解析失败：' + e.message, 'err');
        return;
      }
      if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
        this.toast('内容必须是 JSON 对象', 'err');
        return;
      }
      /* 顶层含 cookies → 原样提交；否则把整个对象当 cookies dict */
      const body = (obj.cookies && typeof obj.cookies === 'object' && !Array.isArray(obj.cookies))
        ? obj
        : { cookies: obj };
      this.injectBusy = true;
      try {
        const r = await this.api('/api/login/inject', { method: 'POST', body });
        this.toast(r.msg || 'Cookie 已写入', 'ok');
        this.injectText = '';
        this.injectOpen = false;
        await Promise.all([this.refreshLogin(), this.refreshStatus()]);
      } catch (e) {
        this.toast(e.message, 'err');
      } finally {
        this.injectBusy = false;
      }
    },

    async logout() {
      if (!window.confirm('确定要登出吗？\n将停止调度器并清除本机 Cookie，随后需要重新扫码登录（无需重启容器）。')) return;
      this.maintBusy = true;
      try {
        const r = await this.api('/api/maintenance/clear-cookie', { method: 'POST', body: { confirm: true } });
        this.toast(r.msg || '已登出', 'ok');
        await Promise.all([this.refreshLogin(), this.refreshStatus()]);
      } catch (e) {
        this.toast(e.message, 'err');
      } finally {
        this.maintBusy = false;
      }
    },

    /* ================================================================
     * 数据同步（书架 SSE 用 fetch + ReadableStream）
     * ================================================================ */
    async syncShelf() {
      if (this.shelfSync.busy) return;
      this.shelfSync = { busy: true, idx: 0, total: 0, logs: [] };
      this.pushShelfLog('开始同步书架…', '');
      try {
        const headers = {};
        if (this.token) headers['Authorization'] = 'Bearer ' + this.token;
        const res = await fetch('/api/sync/shelf', { method: 'POST', headers });
        if (res.status === 401) { this.needToken = true; return; }
        if (!res.ok || !res.body) {
          let j = {};
          try { j = await res.json(); } catch (e) { /* ignore */ }
          throw new Error(j.msg || ('同步启动失败（HTTP ' + res.status + '）'));
        }
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let p;
          while ((p = buf.indexOf('\n\n')) >= 0) {
            const chunk = buf.slice(0, p);
            buf = buf.slice(p + 2);
            this.handleShelfChunk(chunk);
          }
        }
        if (buf.trim()) this.handleShelfChunk(buf);
        await Promise.all([this.loadShelf(), this.refreshStatus(true)]);
      } catch (e) {
        this.pushShelfLog(e.message, 'err');
        this.toast(e.message, 'err');
      } finally {
        this.shelfSync.busy = false;
      }
    },

    handleShelfChunk(chunk) {
      let data = '';
      chunk.split('\n').forEach((ln) => {
        const t = ln.trim();
        if (t.indexOf('data:') === 0) data += t.slice(5).trim();
      });
      if (!data) return;
      let ev;
      try { ev = JSON.parse(data); } catch (e) { return; }
      switch (ev.event) {
        case 'starting':
          this.pushShelfLog(ev.msg || '开始同步…', '');
          break;
        case 'shelf_ok':
          this.pushShelfLog(ev.msg || ('书架 ' + (ev.count || 0) + ' 本已写入'), 'ok');
          break;
        case 'chapter':
          this.shelfSync.idx = ev.idx || 0;
          this.shelfSync.total = ev.total || 0;
          this.pushShelfLog(ev.msg || ('拉取目录 ' + ev.idx + '/' + ev.total), '');
          break;
        case 'warn':
          this.pushShelfLog('⚠ ' + (ev.msg || ev.title || '部分书籍拉取失败'), 'warn');
          break;
        case 'done':
          this.pushShelfLog(ev.msg || '同步完成', 'ok');
          this.toast(ev.msg || '书架同步完成', 'ok');
          break;
        case 'error':
          this.pushShelfLog('✕ ' + (ev.msg || '同步失败'), 'err');
          this.toast(ev.msg || '书架同步失败', 'err');
          break;
        default:
          if (ev.msg) this.pushShelfLog(ev.msg, '');
      }
    },

    pushShelfLog(text, cls) {
      this.shelfSync.logs.push({ text, cls: cls || '' });
      if (this.shelfSync.logs.length > 120) this.shelfSync.logs.shift();
    },

    shelfSyncPct() {
      if (!this.shelfSync.total) return 0;
      return Math.min(100, Math.round(this.shelfSync.idx / this.shelfSync.total * 100));
    },

    async syncStats() {
      this.statsBusy = true;
      try {
        const r = await this.api('/api/sync/stats', { method: 'POST' });
        this.toast('阅读统计已同步', 'ok');
        if (r.data && r.data.today_seconds !== undefined && this.status) {
          this.status.stats = {
            today_seconds: r.data.today_seconds || 0,
            week_seconds: r.data.weekly_seconds || 0,
            month_seconds: r.data.monthly_seconds || 0,
            total_seconds: r.data.total_seconds || 0,
            updated_at: r.data.updated_at || '',
          };
        }
      } catch (e) {
        this.toast(e.message, 'err');
      } finally {
        this.statsBusy = false;
        await this.refreshStatus(true);
      }
    },

    async syncProgress() {
      this.progressBusy = true;
      try {
        const r = await this.api('/api/sync/progress', { method: 'POST' });
        this.toast('当前书进度已同步（' + (r.data ? r.data.progress : 0) + '%）', 'ok');
        await this.loadShelf();
      } catch (e) {
        /* 409 NO_CURRENT_BOOK 等：直接 toast 后端 msg */
        this.toast(e.message, 'err');
      } finally {
        this.progressBusy = false;
      }
    },

    /* ================================================================
     * 书架 / 黑名单
     * ================================================================ */
    async loadShelf() {
      this.shelfLoading = true;
      try {
        const q = '?keyword=' + encodeURIComponent(this.shelfKw || '')
          + '&filter=' + encodeURIComponent(this.shelfFilter)
          + '&page=' + this.shelfPage
          + '&page_size=' + this.shelfPageSize;
        const r = await this.api('/api/shelf' + q);
        this.shelfItems = r.data || [];
        this.shelfTotal = r.total || 0;
      } catch (e) {
        if (e.message.indexOf('未授权') < 0) this.toast('书架加载失败：' + e.message, 'err');
      } finally {
        this.shelfLoading = false;
      }
    },

    onShelfKw() {
      clearTimeout(this.shelfTimer);
      this.shelfTimer = setTimeout(() => {
        this.shelfPage = 1;
        this.loadShelf();
      }, 350);
    },

    setShelfFilter(f) {
      if (this.shelfFilter === f) return;
      this.shelfFilter = f;
      this.shelfPage = 1;
      this.loadShelf();
    },

    shelfPages() {
      return Math.max(1, Math.ceil(this.shelfTotal / this.shelfPageSize));
    },

    gotoShelfPage(p) {
      if (p < 1 || p > this.shelfPages()) return;
      this.shelfPage = p;
      this.loadShelf();
    },

    async onBlackToggle(open) {
      this.blackOpen = open;
      if (open && !this.blackLoaded) {
        this.blackLoading = true;
        try {
          const r = await this.api('/api/books/blacklist');
          this.blackItems = r.data || [];
          this.blackLoaded = true;
        } catch (e) {
          this.toast('黑名单加载失败：' + e.message, 'err');
        } finally {
          this.blackLoading = false;
        }
      }
    },

    /* ================================================================
     * 数据维护
     * ================================================================ */
    async resetToday() {
      if (!window.confirm('确定清零今日本地计时吗？\n（仅影响本地估算，同步 Skill 统计后以服务端为准）')) return;
      this.maintBusy = true;
      try {
        const r = await this.api('/api/maintenance/reset-today', { method: 'POST', body: { confirm: true } });
        this.toast(r.msg || '今日本地计时已清零', 'ok');
        await this.refreshStatus(true);
      } catch (e) {
        this.toast(e.message, 'err');
      } finally {
        this.maintBusy = false;
      }
    },

    /* ================================================================
     * 配置中心
     * ================================================================ */
    async loadConfig() {
      try {
        const r = await this.api('/api/config');
        this.cfg = r.data || {};
        this.defaults = r.defaults || {};
        this.buildForm();
      } catch (e) {
        if (e.message.indexOf('未授权') < 0) this.toast('配置加载失败：' + e.message, 'err');
      }
    },

    async reloadConfig() {
      await this.loadConfig();
      this.toast('已恢复为服务端当前配置', 'info');
    },

    buildForm() {
      const form = {};
      const snap = {};
      CONFIG_SECTIONS.forEach((sec) => {
        const merged = Object.assign(
          {},
          this.defaults[sec] || {},
          (this.cfg[sec] && typeof this.cfg[sec] === 'object') ? this.cfg[sec] : {}
        );
        form[sec] = JSON.parse(JSON.stringify(merged));
        snap[sec] = JSON.stringify(form[sec]);
      });
      this.form = form;
      this.cfgSnap = snap;
    },

    fields(sec) {
      const defs = (this.defaults && this.defaults[sec]) || {};
      return Object.keys(defs).map((key) => {
        const dv = defs[key];
        const meta = (FIELD_META[sec] && FIELD_META[sec][key]) || null;
        let kind = 'text';
        if (typeof dv === 'boolean') kind = 'bool';
        else if (typeof dv === 'number') kind = 'number';
        return {
          key,
          kind,
          password: !!PASSWORD_KEYS[sec + '.' + key],
          label: meta ? meta.label : key,
          hint: meta ? meta.hint : '配置项 ' + sec + '.' + key,
          step: (typeof dv === 'number' && !Number.isInteger(dv)) ? '0.5' : '1',
        };
      });
    },

    dirtySections() {
      if (!this.cfg) return [];
      const out = [];
      CONFIG_SECTIONS.forEach((sec) => {
        if (this.cfgSnap[sec] === undefined) return;
        if (JSON.stringify(this.form[sec]) !== this.cfgSnap[sec]) out.push(sec);
      });
      return out;
    },

    normalizeSection(sec, values) {
      const defs = (this.defaults && this.defaults[sec]) || {};
      const out = {};
      Object.keys(defs).forEach((k) => {
        const dv = defs[k];
        let v = values[k];
        if (typeof dv === 'number') {
          v = Number(v);
          if (!Number.isFinite(v)) v = dv;
        } else if (typeof dv === 'boolean') {
          v = !!v;
        } else {
          v = v == null ? '' : String(v);
        }
        out[k] = v;
      });
      return out;
    },

    async saveConfig() {
      const dirty = this.dirtySections();
      if (!dirty.length) { this.toast('没有需要保存的修改', 'info'); return; }

      const rd = this.form.reading;
      if (Number(rd.max_hours) < Number(rd.min_hours)) {
        this.toast('校验失败：每日最多小时数必须 ≥ 最少小时数', 'err');
        return;
      }
      const fpt = Number(this.form.risk.fail_pause_threshold);
      const fst = Number(this.form.risk.fail_streak_threshold);
      if (!(fpt === 0 || fpt >= fst)) {
        this.toast('校验失败：第二级暂停阈值必须为 0（禁用）或 ≥ 第一级阈值（' + fst + '）', 'err');
        return;
      }

      const patch = {};
      dirty.forEach((sec) => { patch[sec] = this.normalizeSection(sec, this.form[sec]); });

      this.savingCfg = true;
      try {
        const r = await this.api('/api/config', { method: 'PUT', body: patch });
        this.toast(r.msg || '配置已保存', 'ok');
        await this.loadConfig();
      } catch (e) {
        /* 受保护段/未知段后端返回 400 */
        this.toast(e.message, 'err');
      } finally {
        this.savingCfg = false;
      }
    },

    async verifySkill() {
      this.testSkill = { busy: true, msg: '校验中…', ok: null };
      try {
        const r = await this.api('/api/skill/verify', { method: 'POST' });
        this.testSkill = { busy: false, msg: r.msg || (r.ok ? '有效' : '校验失败'), ok: !!r.ok };
        this.toast(r.msg || '校验完成', r.ok ? 'ok' : 'err');
      } catch (e) {
        this.testSkill = { busy: false, msg: e.message, ok: false };
        this.toast(e.message, 'err');
      }
    },

    async testPush() {
      this.testPush = { busy: true, msg: '发送中…', ok: null };
      try {
        const r = await this.api('/api/push/test', { method: 'POST' });
        this.testPush = { busy: false, msg: r.msg || (r.ok ? '已发送' : '发送失败'), ok: !!r.ok };
        this.toast(r.msg || '测试完成', r.ok ? 'ok' : 'err');
      } catch (e) {
        this.testPush = { busy: false, msg: e.message, ok: false };
        this.toast(e.message, 'err');
      }
    },

    /* ================================================================
     * 日志
     * ================================================================ */
    async loadLogFiles() {
      try {
        const r = await this.api('/api/logs/files');
        this.logFiles = r.data || [];
        /* 默认选今天；不立即拉内容，等进入日志 Tab，避免无谓 SSE */
        if (!this.logDate && this.logFiles.length) this.logDate = this.logFiles[0].date;
      } catch (e) { /* 忽略 */ }
    },

    isLogToday() {
      const f = this.logFiles.find((x) => x.date === this.logDate);
      return !!(f && f.current);
    },

    async selectLogDate(d) {
      if (this.logDate === d && this.logLines.length) return;
      this.logDate = d;
      this.logLines = [];
      this.closeLogStream();
      if (this.isLogToday()) {
        await this.openTodayLog();
      } else {
        await this.loadHistory(false);
      }
    },

    async openTodayLog() {
      try {
        const r = await this.api('/api/logs/tail?lines=500');
        const content = (r.data && r.data.content) || '';
        this.logLines = content.split(/\r?\n/).filter((x) => x !== '').map((ln) => ({
          cls: this.classifyLevel(ln, 'INFO'),
          text: ln,
        }));
        this.scrollLog();
      } catch (e) { /* 忽略 */ }
      this.openLogStream();
    },

    openLogStream() {
      this.closeLogStream();
      const es = new EventSource(this.sseUrl('/api/logs/stream?level=' + this.logLevel));
      this.logES = es;
      es.onmessage = (e) => {
        let ev;
        try { ev = JSON.parse(e.data); } catch (err) { return; }
        /* tail 已拉过 500 行，服务端补发的 200 行 backlog 跳过，避免重复 */
        if (ev.backlog) return;
        if (!ev.msg) return;
        this.appendLog(ev.msg, this.classifyLevel(ev.msg, ev.level || 'INFO'));
      };
      /* EventSource 会自动重连；服务端关闭/401 时浏览器退避重试，无需额外处理 */
    },

    closeLogStream() {
      if (this.logES) { try { this.logES.close(); } catch (e) {} this.logES = null; }
    },

    onLogLevelChange() {
      if (this.isLogToday()) this.openLogStream();
    },

    classifyLevel(text, fallback) {
      const m = String(text || '').match(/\b(CRITICAL|ERROR|WARNING|DEBUG|INFO)\b/);
      return m ? m[1] : (fallback || 'INFO');
    },

    appendLog(text, cls) {
      this.logLines.push({ cls: cls || 'INFO', text: String(text) });
      if (this.logLines.length > 2000) this.logLines.splice(0, this.logLines.length - 2000);
      if (this.follow) this.scrollLog();
    },

    scrollLog() {
      this.$nextTick(() => {
        const el = document.getElementById('logConsole');
        if (el && this.follow) el.scrollTop = el.scrollHeight;
      });
    },

    renderLogLines() {
      return this.logLines.map((l) => '<div class="ln ' + l.cls + '">' + this.esc(l.text) + '</div>').join('');
    },

    async loadHistory(append) {
      if (this.logLoading || !this.logDate) return;
      this.logLoading = true;
      try {
        const offset = append ? this.logOffset : 0;
        const r = await this.api('/api/logs/file?date=' + encodeURIComponent(this.logDate)
          + '&offset=' + offset + '&limit=12000');
        const d = r.data || {};
        this.logOffset = d.next_offset || 0;
        this.logEof = !!d.eof;
        this.logTotal = d.total_size || 0;
        const lines = String(d.content || '').split(/\r?\n/);
        if (!append) this.logLines = [];
        lines.forEach((ln) => {
          if (ln === '') return;
          this.logLines.push({ cls: this.classifyLevel(ln, 'INFO'), text: ln });
        });
      } catch (e) {
        this.toast(e.message, 'err');
      } finally {
        this.logLoading = false;
      }
    },

    logDownloadUrl() {
      if (!this.logDate) return '#';
      return this.sseUrl('/api/logs/file?date=' + encodeURIComponent(this.logDate) + '&download=1');
    },

    /* ================================================================
     * 报告
     * ================================================================ */
    async loadReport(refresh) {
      if (refresh) this.reportRefreshing = true;
      else this.reportLoading = true;
      try {
        const r = await this.api('/api/report' + (refresh ? '?refresh=1' : ''));
        this.report = r.data || null;
        this.reportLoaded = true;
        if (window.WxReport) window.WxReport.disposeAll();
        this.$nextTick(() => setTimeout(() => {
          if (this.tab === 'report' && this.report && window.WxReport) window.WxReport.renderAll(this.report);
        }, 60));
      } catch (e) {
        this.toast('报告加载失败：' + e.message, 'err');
      } finally {
        this.reportLoading = false;
        this.reportRefreshing = false;
      }
    },

    refreshReport() {
      if (this.reportLoading || this.reportRefreshing) return;
      this.loadReport(true);
    },

    kpiText(key) {
      const k = this.report && this.report.kpi;
      if (!k) return '-';
      if (k[key]) return k[key];
      if (key === 'total_seconds_fmt') return this.fmtDur(k.total_seconds);
      if (key === 'read_longest_fmt') return this.fmtDur(k.read_longest_seconds);
      return '-';
    },
  };
}
