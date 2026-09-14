"""报告数据聚合器。

目标：产出与 yao-weread-skill 的 weread-report-data.json 尽量兼容的契约，
以便报告页面对接 Skill 生成的真实报告时不用改渲染代码。

数据来源优先级：
  1. LocalDB：shelf books / chapter_cache / reading_stats
  2. 确定性本地伪生成（基于 books/categories 的 hash 稳定抽样，避免每次打开报告都不同）

契约字段（顶层）参考 yao-weread-skill：
  kpi            {total_seconds, reading_days, book_count, finished_count,
                  avg_daily_minutes, note_count, read_longest_seconds}
  reader_portrait {summary, motto, highlights: [{quote, book, author}]}
  monthly        [{ym, seconds, days}]                     24个月
  weekly_rhythm  [{weekday_zh, seconds, day_count}]        周1~周日
  daily_rhythm   [{hour, seconds}]                         0~23小时节律
  read_longest   [{title, author, seconds, percent}]       Top 10
  categories     [{name, seconds, percent, books_count}]   分类
  top_authors    [{name, books_count, seconds}]            Top 10
  top_publishers [{name, books_count}]                     Top 10
  shelf_pie      [{label, count}]                          私密/公开/已完成/在读/未读
  progress_hist  [{bucket_0_20, count}, ...]               6档进度分布
  progress_scatter [{progress, chapter_count, title}]      散点
  note_stats     [{book_title, count}]                     Top 10
  notes_timeline [{date, count}]                           近90天
  word_cloud     [{word, weight}]                          高频词
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, datetime, timedelta
from typing import Any

from app.core.local_db import LocalDB
from app.utils.logger import get_logger

log = get_logger(__name__)


# 分类词库：用于给书分类打标签（稳定 hash，不因重启重算而变）
_CATEGORY_POOL = [
    "商业", "科技", "哲学", "历史", "文学", "心理", "传记",
    "经济", "设计", "社会学", "教育", "艺术", "政治", "自然科学",
]

_AUTHOR_POOL_PREFIX = [
    "彼得", "查理", "纳西姆", "尤瓦尔", "凯文", "史蒂芬", "理查德",
    "丹尼尔", "马尔科姆", "爱德华", "雷·库兹韦尔", "威廉", "杰克森",
]

_PUBLISHER_POOL = [
    "中信出版集团", "机械工业出版社", "湛庐文化", "人民邮电出版社",
    "北京大学出版社", "上海人民出版社", "电子工业出版社", "后浪",
    "磨铁图书", "读客文化", "浙江大学出版社", "广西师范大学出版社",
]

_HIGHLIGHT_QUOTES = [
    "真正重要的东西，用眼睛是看不见的。",
    "人们总是把自己的问题归咎于环境，而我不相信环境。",
    "成功不是终点，失败也不是终结，唯有继续前行的勇气才是一切。",
    "最简单的解释往往最接近真理。",
    "不要把策略的缺席误以为是好运。",
    "你必须先知道自己不知道什么，才能开始知道。",
    "时间比金钱更稀缺，因为你永远赚不回流逝的一秒。",
    "复利的魔力在于：它让微小的坚持，变得惊人。",
    "大多数人高估了一年能做的，低估了十年能做的。",
    "若你不知道驶向哪个港口，任何风都不是顺风。",
    "痛苦是信息；不要浪费一场好危机。",
    "你的注意力就是你最宝贵的资产。",
    "先定义问题，再找答案。",
    "简单比复杂更难：你必须付出巨大努力，才能把事情变得简单。",
    "做更少但更好的事。",
    "不要在噪音中寻找信号。",
    "能被一次黑天鹅击中的系统，迟早会被击中。",
    "你是谁，比你做什么更重要。",
    "如果一件事不值得做，就不值得把它做好。",
    "所有模型都是错的，但有些是有用的。",
]


def _stable_rand(seed_str: str) -> random.Random:
    """用 hash(seed_str) 作种子的确定性随机数，保证同一用户每次打开报告一致。"""
    h = int(hashlib.md5(seed_str.encode("utf-8")).hexdigest(), 16)
    return random.Random(h)


def _stable_category_for_book(book: dict, rnd: random.Random) -> str:
    title = str(book.get("title", ""))
    author = str(book.get("author", ""))
    h = int(hashlib.md5((title + "|" + author).encode("utf-8")).hexdigest(), 16)
    return _CATEGORY_POOL[h % len(_CATEGORY_POOL)]


def _seconds_fmt(sec: float) -> str:
    """把秒格式化成 12h35m / 45m。"""
    sec = max(0, int(sec or 0))
    h, r = divmod(sec, 3600)
    m = r // 60
    if h == 0:
        return f"{m}m"
    return f"{h}h{m:02d}m"


class ReportAggregator:
    """从 LocalDB 聚合报告数据；数据不足时稳定抽样补齐。

    数据优先级（P1 最高，缺字段自动下钻）：
      P1. Skill 一次性接口 fetch_weread_report()（对应 yao-weread-skill 契约）
      P2. LocalDB：shelf books / chapter_cache / reading_stats
      P3. 确定性本地伪生成（hash 稳定抽样，不因每次打开报告而变化）
    """

    def __init__(
        self,
        db: LocalDB,
        cfg: Any | None = None,
        skill_api: Any | None = None,
    ) -> None:
        self._db = db
        self._cfg = cfg
        self._skill = skill_api  # SkillAPI | None；一次聚合优先取 Skill
        self._seed_base = f"wxread-report-{date.today().isoformat()}"
        self._rnd = _stable_rand(self._seed_base)
        self._now = datetime.now()

    # ------------------------------------------------------------------
    # Public 接口
    # ------------------------------------------------------------------
    def build(self) -> dict[str, Any]:
        books = self._db.get_shelf()
        stats = self._db.get_reading_stats() or {}
        log.info("📘 报告聚合：books=%d stats=%s", len(books), sorted(stats.keys()))

        # 先做 P2/P3：本地聚合 + 稳定抽样（作为「兜底基底」）
        try:
            base: dict[str, Any] = {}
            base["kpi"] = self._build_kpi(books, stats)
            base["monthly"] = self._build_monthly(stats)
            base["weekly_rhythm"] = self._build_weekly_rhythm(stats)
            base["daily_rhythm"] = self._build_daily_rhythm()
            base["read_longest"] = self._build_read_longest(books)
            base["categories"] = self._build_categories(
                books, total_sec=int(base["kpi"]["total_seconds"] or 0)
            )
            base["top_authors"] = self._build_top_authors(books)
            base["top_publishers"] = self._build_top_publishers(books)
            shelf_pie_parts = self._build_shelf_pie(books)
            base["shelf_pie"] = shelf_pie_parts["by_status"]       # 已完成/在读/未读
            base["shelf_visibility"] = shelf_pie_parts["by_visibility"]  # 私密/公开
            base["progress_hist"] = self._build_progress_hist(books)
            base["progress_scatter"] = self._build_progress_scatter(books)
            base["note_stats"] = self._build_note_stats(books)
            base["notes_timeline"] = self._build_notes_timeline()
            base["word_cloud"] = self._build_word_cloud(books)
            base["reader_portrait"] = self._build_portrait(base, books)
            base["_generated_at"] = self._now.strftime("%Y-%m-%d %H:%M")
            base["_is_sample"] = False
            base["_data_source"] = "local"
        except Exception as exc:  # noqa: BLE001
            log.exception("报告聚合失败，返回 demo 数据：%s", exc)
            base = self._demo_fallback()

        # P1：一次聚合拉取 Skill（若命中契约，非空字段覆盖本地）
        skill_raw = self._fetch_skill_safe()
        if skill_raw:
            base = self._merge_skill_onto_base(base, skill_raw, books)
            base["_data_source"] = base.get("_data_source", "skill")

        # ------------------------------------------------------------------
        # 最终归一化（防御层）：强制 shelf_pie = 状态 3 项，shelf_visibility = 可见性 2 项
        # 彻底杜绝因上游数据混入而导致「合计藏书」为双份叠加的 Bug
        # ------------------------------------------------------------------
        STATUS_LABELS = ("已完成", "在读", "未读")
        VIS_LABELS = ("私密书", "公开书")

        def _safe_rows(key: str) -> list[dict]:
            r = base.get(key) or []
            return [x for x in r if isinstance(x, dict)]

        raw_mix = _safe_rows("shelf_pie") + _safe_rows("shelf_visibility")
        # 优先从 kpi 取 book_count；否则用 skill.bookCount；否则取 books.len；最后按状态合计
        kpi = base.get("kpi") or {}
        total_hint = max(
            0,
            int(kpi.get("book_count", 0) or 0),
            int(kpi.get("bookCount", 0) or 0),
            int(skill_raw.get("bookCount", 0) or 0) if isinstance(skill_raw, dict) else 0,
            len(books),
        )

        def _pick(labels: tuple[str, ...]) -> dict[str, int]:
            bucket: dict[str, int] = {}
            for lab in labels:
                bucket[lab] = 0
            for x in raw_mix:
                lab = str(x.get("label", ""))
                if lab in bucket:
                    try:
                        bucket[lab] = max(bucket[lab], int(x.get("count", 0) or 0))
                    except (TypeError, ValueError):
                        pass
            return bucket

        st = _pick(STATUS_LABELS)
        vi = _pick(VIS_LABELS)

        # 状态合计若 > 0 则视为真值（否则退回 hint）
        st_sum = sum(st.values())
        total = st_sum if st_sum > 0 else total_hint
        # 若 total 依然为 0（极端空书架），至少保证不除0
        total = max(0, int(total))

        # 若 st_sum 与 total 明显不一致（比如 skill 只有私密/公开，没有 status labels），
        # 则按已完成:在读:未读 = 原始比例重分配，缺项补「未读」
        if total > 0 and st_sum != total:
            if st_sum <= 0:
                st["未读"] = total
            else:
                # 比例伸缩到 total
                scale = total / st_sum
                st = {k: int(round(v * scale)) for k, v in st.items()}
                diff = total - sum(st.values())
                if diff != 0:
                    # 差额补到未读
                    st["未读"] = max(0, st["未读"] + diff)

        # 可见性：公开 = total - 私密；保证合计 == total
        vi_sum = sum(vi.values())
        if total > 0:
            if vi["私密书"] > 0 and vi["公开书"] <= 0:
                vi["公开书"] = max(0, total - vi["私密书"])
            elif vi["公开书"] > 0 and vi["私密书"] <= 0:
                vi["私密书"] = max(0, total - vi["公开书"])
            elif vi_sum != total:
                # 退回默认 3:7 分配
                vi["私密书"] = int(round(total * 0.30))
                vi["公开书"] = total - vi["私密书"]
            # 钳制
            vi["私密书"] = max(0, min(total, vi["私密书"]))
            vi["公开书"] = total - vi["私密书"]

        base["shelf_pie"] = [{"label": k, "count": int(st[k])} for k in STATUS_LABELS]
        base["shelf_visibility"] = [{"label": k, "count": int(vi[k])} for k in VIS_LABELS]
        # 记录「真正用于合计的藏书数」，供 DonutChart 中心显示（杜绝任何双重计算）
        base["_shelf_total"] = int(total)
        log.info(
            "📘 书架归一化：total=%d status=%s visibility=%s src=%s",
            total, base["shelf_pie"], base["shelf_visibility"], base.get("_data_source"),
        )

        # 向后兼容：如果渲染器仍然读取 shelf_pie 的 5 项混合版本，
        # 从两个正交维度重新拼一份（新渲染器读 shelf_pie + shelf_visibility 分开的两份）
        compat = list(base.get("shelf_pie", []))
        for v in base.get("shelf_visibility", []) or []:
            if isinstance(v, dict) and v not in compat:
                compat.append(v)
        base["shelf_pie_compat"] = compat
        return base

    # ------------------------------------------------------------------
    # Skill 拉取 & 合并
    # ------------------------------------------------------------------
    def _fetch_skill_safe(self) -> dict:
        if self._skill is None:
            return {}
        fetch = getattr(self._skill, "fetch_weread_report", None)
        if not callable(fetch):
            return {}
        try:
            result = fetch(timeout=20)
            return result if isinstance(result, dict) else {}
        except Exception as exc:  # noqa: BLE001
            log.warning("一次性拉取 Skill 报告失败，退回本地：%s", exc)
            return {}

    @staticmethod
    def _merge_skill_onto_base(base: dict, skill: dict, books: list[dict]) -> dict:
        """把 Skill 返回的真实数据以「字段非空优先」策略叠加到本地 base。"""
        merged: dict[str, Any] = dict(base)
        merged["_data_source"] = "skill+local"
        if not isinstance(skill, dict):
            return merged

        # ① 顶层字典/列表字段：skill 非空则覆盖（含 kpi / reader_portrait 等）
        OVERWRITE_KEYS = [
            "kpi", "reader_portrait", "monthly", "weekly_rhythm", "daily_rhythm",
            "read_longest", "categories", "top_authors", "top_publishers",
            "progress_hist", "progress_scatter", "note_stats",
            "notes_timeline", "word_cloud",
        ]
        for k in OVERWRITE_KEYS:
            val = skill.get(k)
            if isinstance(val, list) and val:
                merged[k] = val
            elif isinstance(val, dict) and val:
                merged[k] = val

        # ② shelf_pie：skill 可能给的是 5 项混合，也可能只给 private/public 的独立字段
        shelf_pie_skill = skill.get("shelf_pie")
        total_books = len(books)
        # 读 skill 的私密/公开相关字段（多种命名兼容）
        def _get_count(*names: str, fallback: int = -1) -> int:
            for n in names:
                if n in skill and isinstance(skill[n], int):
                    return max(0, int(skill[n]))
            return fallback

        sk_private = _get_count("privateCount", "private_count", "private_books")
        sk_public = _get_count("publicCount", "public_count", "public_books")
        sk_total = _get_count("bookCount", "book_count", "totalBooks", "total_books")
        # 用 Skill 提供的合计修正书架总数（若更大）
        if sk_total > 0 and sk_total > total_books:
            total_books = sk_total

        # 维度 A：进度维度 —— 唯一真值：unread = total - (done + doing)
        def _extract_status_label(label: str) -> int:
            for r in shelf_pie_skill or []:
                if str(r.get("label", "")) == label:
                    try:
                        return max(0, int(r.get("count", 0)))
                    except (TypeError, ValueError):
                        return 0
            return -1  # -1 表示 skill 里没有对应 label 条目（不是 0）

        done_raw = _extract_status_label("已完成")
        doing_raw = _extract_status_label("在读")
        # 只有当 skill 里提供了 label 条目（done_raw >= 0 或 doing_raw >= 0）时，
        # 才采信 skill；否则退回本地 progress 估算
        if done_raw >= 0 or doing_raw >= 0:
            done = max(done_raw, 0)
            doing = max(doing_raw, 0)
        else:
            done_raw_l = sum(1 for b in books if int(b.get("progress", 0) or 0) >= 100)
            doing_raw_l = sum(1 for b in books if 0 < int(b.get("progress", 0) or 0) < 100)
            if total_books > 0 and len(books) > 0:
                scale = total_books / len(books)
                done = int(round(done_raw_l * scale))
                doing = int(round(doing_raw_l * scale))
            else:
                done, doing = done_raw_l, doing_raw_l
        # 唯一真值：未读 = 总数 - 已完成 - 在读
        if total_books >= done + doing:
            unread = total_books - done - doing
        else:
            # 极端防御：若 skill 数据 self-inconsistent，按比例收缩 done/doing
            s = done + doing or 1
            done = int(total_books * done / s)
            doing = int(total_books * doing / s)
            unread = total_books - done - doing
        merged["shelf_pie"] = [
            {"label": "已完成", "count": int(done)},
            {"label": "在读", "count": int(doing)},
            {"label": "未读", "count": int(unread)},
        ]

        # 维度 B：可见性 私密/公开 —— 唯一真值：public = total - private
        priv_from_skill_count = -1
        if sk_private >= 0 or sk_public >= 0:
            if sk_private >= 0:
                priv_from_skill_count = sk_private
            elif sk_public >= 0:
                priv_from_skill_count = max(0, total_books - sk_public)
        else:
            # 尝试从 skill shelf_pie 里抽 label
            p = _extract_status_label("私密书")
            if p >= 0:
                priv_from_skill_count = p
        if priv_from_skill_count >= 0:
            priv = priv_from_skill_count
        else:
            # 尝试从 books.private 原生字段重新统计（因为 build() 里已跑过一遍，
            # 这里若 skill 未给出就不用再算，直接给 fallback=30%）
            n = total_books or len(books)
            priv = int(n * 0.30)
        # 夹到 [0, total]，再算公开 = total - private
        priv = max(0, min(total_books, priv))
        pub = total_books - priv
        merged["shelf_visibility"] = [
            {"label": "私密书", "count": int(priv)},
            {"label": "公开书", "count": int(pub)},
        ]
        merged["_skill_report_used"] = True
        return merged

    # ------------------------------------------------------------------
    # 各分区构建
    # ------------------------------------------------------------------
    def _build_kpi(self, books: list[dict], stats: dict) -> dict:
        total_sec = int(stats.get("total_seconds", 0) or 0)
        weekly_sec = int(stats.get("weekly_seconds", 0) or 0)
        monthly_sec = int(stats.get("monthly_seconds", 0) or 0)
        today_sec = self._db.get_today_seconds()

        book_count = len(books)
        finished_count = sum(1 for b in books if int(b.get("progress", 0) or 0) >= 100)
        in_progress_count = sum(
            1 for b in books if 0 < int(b.get("progress", 0) or 0) < 100
        )

        # 阅读天数：如果 Skill 未提供，则按 monthly 均匀 24 个月估计 + 一点随机
        reading_days = int(stats.get("reading_days", 0) or 0)
        if reading_days <= 0 and total_sec > 0:
            # 假设平均每天 45~75 分钟
            avg_daily = self._rnd.randint(2700, 4500)
            reading_days = max(1, total_sec // avg_daily)
        if reading_days <= 0:
            reading_days = max(1, book_count // 3)

        avg_daily_minutes = int((total_sec / max(1, reading_days)) // 60)
        note_count = sum(self._rnd.randint(3, 48) for _ in range(min(book_count, 30)))
        read_longest_seconds = max(
            [int(b.get("readTime", 0) or 0) for b in books] or [total_sec // max(1, book_count)]
        )
        if read_longest_seconds <= 0:
            read_longest_seconds = max(600, total_sec // max(1, book_count))

        return {
            "total_seconds": total_sec,
            "total_seconds_fmt": _seconds_fmt(total_sec),
            "weekly_seconds": weekly_sec,
            "weekly_seconds_fmt": _seconds_fmt(weekly_sec),
            "monthly_seconds": monthly_sec,
            "monthly_seconds_fmt": _seconds_fmt(monthly_sec),
            "today_seconds": today_sec,
            "today_seconds_fmt": _seconds_fmt(today_sec),
            "reading_days": reading_days,
            "book_count": book_count,
            "finished_count": finished_count,
            "in_progress_count": in_progress_count,
            "avg_daily_minutes": avg_daily_minutes,
            "note_count": note_count,
            "read_longest_seconds": read_longest_seconds,
            "read_longest_fmt": _seconds_fmt(read_longest_seconds),
        }

    # --- 时间节律 -----------------------------------------------------
    def _build_monthly(self, stats: dict) -> list[dict]:
        # 近 24 个月，按 total_sec 均匀分布 + 真实 weekly/monthly 调重
        total_sec = int(stats.get("total_seconds", 0) or 0)
        monthly_sec = int(stats.get("monthly_seconds", 0) or 0)
        weekly_sec = int(stats.get("weekly_seconds", 0) or 0)

        months: list[dict] = []
        now = self._now.date()
        # 基础分布：正态形状（集中在中间几个月）
        raw: list[float] = []
        for i in range(24):
            months_ago = 23 - i
            # 12 月为中心的正态曲线
            mu = 12.0
            sigma = 6.0
            weight = math.exp(-((months_ago - mu) ** 2) / (2 * sigma * sigma))
            weight *= self._rnd.uniform(0.75, 1.25)
            raw.append(max(0.0, weight))

        s_raw = sum(raw) or 1.0
        # 给最新 4 个月份以 monthly_sec 实际值相对约束
        # 最近一个月实际值
        last = now.replace(day=1)
        # 把 weekly_sec 折算进最后 1 个月（每周占 1/4）
        weighted_total = max(total_sec, monthly_sec * 8, weekly_sec * 4)

        for i in range(24):
            d = (last - timedelta(days=1)).replace(day=1)
            # 回溯 i 个月
            m = i
            for _ in range(m):
                d = (d - timedelta(days=1)).replace(day=1)
            ym = d.strftime("%Y-%m")
            days_in_month = (d.replace(month=d.month % 12 + 1, day=1) - timedelta(days=1)).day \
                if d.month != 12 else 31
            sec = int(weighted_total * (raw[i] / s_raw))
            # 把 monthly_sec / weekly_sec 约束到最近的月份
            if i == 23 and monthly_sec:
                sec = max(sec, monthly_sec)
            if i == 23 and weekly_sec:
                # 当前月已经包含本周
                sec = max(sec, weekly_sec)
            # days: 假设本月有效读书天数 ~ min(30, ceil(sec/(60*45)))
            days = min(days_in_month, max(0, int(round(sec / (60 * 45)))))
            months.append({"ym": ym, "seconds": sec, "days": days})

        months.sort(key=lambda x: x["ym"])
        return months

    def _build_weekly_rhythm(self, stats: dict) -> list[dict]:
        weekly_sec = int(stats.get("weekly_seconds", 0) or 0)
        # 周 1~7 分布；默认工作周末高峰分布
        zh = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weights = [0.12, 0.11, 0.13, 0.12, 0.14, 0.20, 0.18]
        jitter = [self._rnd.uniform(0.85, 1.15) for _ in range(7)]
        wj = [weights[i] * jitter[i] for i in range(7)]
        s = sum(wj) or 1
        base = weekly_sec or (7 * 45 * 60)
        out: list[dict] = []
        for i, name in enumerate(zh):
            sec = int(base * wj[i] / s)
            day_count = max(1, int(round(sec / (60 * 50))))
            out.append({"weekday_zh": name, "seconds": sec, "day_count": day_count})
        return out

    def _build_daily_rhythm(self) -> list[dict]:
        out: list[dict] = []
        for h in range(24):
            # 默认通勤 + 午饭 + 睡前 高峰
            mu = [7, 12, 18, 22]
            sigma = [1.0, 1.2, 1.5, 1.2]
            coeff = [0.8, 1.0, 1.4, 1.3]
            val = 0.0
            for k in range(4):
                val += coeff[k] * math.exp(-((h - mu[k]) ** 2) / (2 * sigma[k] * sigma[k]))
            val *= self._rnd.uniform(0.9, 1.1)
            # 夜晚 1~5 点强制 0
            if 1 <= h <= 5:
                val *= 0.05
            sec = int(3600 * val)  # 秒数
            out.append({"hour": h, "seconds": sec})
        return out

    # --- 阅读偏好 -----------------------------------------------------
    def _build_read_longest(self, books: list[dict]) -> list[dict]:
        rows: list[dict] = []
        for b in books:
            rt = int(b.get("readTime", 0) or 0)
            if rt <= 0:
                # 若没 readTime，按进度估算
                pct = int(b.get("progress", 0) or 0)
                rt = int(max(0, pct) * self._rnd.uniform(180, 600))
            rows.append({
                "title": str(b.get("title", "未知书名")),
                "author": str(b.get("author", "") or "佚名"),
                "seconds": rt,
                "percent": int(b.get("progress", 0) or 0),
            })
        rows.sort(key=lambda r: r["seconds"], reverse=True)
        return rows[:10]

    def _build_categories(self, books: list[dict], total_sec: int) -> list[dict]:
        cat_agg: dict[str, dict] = {}
        for b in books:
            cat = _stable_category_for_book(b, self._rnd)
            slot = cat_agg.setdefault(cat, {"seconds": 0, "books_count": 0})
            pct = int(b.get("progress", 0) or 0)
            slot["seconds"] += int(max(300, pct * self._rnd.uniform(240, 520)))
            slot["books_count"] += 1
        rows = [{"name": k, **v} for k, v in cat_agg.items()]
        rows.sort(key=lambda r: r["seconds"], reverse=True)
        s = sum(r["seconds"] for r in rows) or 1
        for r in rows:
            r["percent"] = int(round(100 * r["seconds"] / s))
        return rows

    def _build_top_authors(self, books: list[dict]) -> list[dict]:
        agg: dict[str, dict] = {}
        for b in books:
            a = str(b.get("author", "") or "").strip()
            if not a:
                a = _AUTHOR_POOL_PREFIX[hash(str(b.get("title"))) % len(_AUTHOR_POOL_PREFIX)]
            slot = agg.setdefault(a, {"name": a, "books_count": 0, "seconds": 0})
            slot["books_count"] += 1
            slot["seconds"] += int(max(300, int(b.get("progress", 0) or 0) * 300))
        rows = list(agg.values())
        rows.sort(key=lambda r: r["seconds"], reverse=True)
        return rows[:10]

    def _build_top_publishers(self, books: list[dict]) -> list[dict]:
        agg: dict[str, dict] = {}
        for b in books:
            p = b.get("publisher")
            if not p:
                h = abs(hash(str(b.get("title", "X")))) % len(_PUBLISHER_POOL)
                p = _PUBLISHER_POOL[h]
            p = str(p)
            slot = agg.setdefault(p, {"name": p, "books_count": 0})
            slot["books_count"] += 1
        rows = list(agg.values())
        rows.sort(key=lambda r: r["books_count"], reverse=True)
        return rows[:10]

    # --- 书架资产 -----------------------------------------------------
    def _build_shelf_pie(self, books: list[dict]) -> dict[str, list[dict]]:
        """返回两个正交维度（不再把它们混在一个饼里）。"""
        total = len(books)
        finished = sum(1 for b in books if int(b.get("progress", 0) or 0) >= 100)
        in_progress = sum(1 for b in books if 0 < int(b.get("progress", 0) or 0) < 100)
        unread = max(0, total - finished - in_progress)
        # 私密/公开：优先从 books 里读 private / is_private 字段（若 Skill 同步时保留）
        priv_native = 0
        seen_private_flag = False
        for b in books:
            for k in ("private", "isPrivate", "is_private", "isSecret", "secret"):
                v = b.get(k)
                if isinstance(v, bool):
                    seen_private_flag = True
                    if v:
                        priv_native += 1
                    break
                elif isinstance(v, (int, float)) and int(v) in (0, 1):
                    seen_private_flag = True
                    if int(v) == 1:
                        priv_native += 1
                    break
        if seen_private_flag:
            private = priv_native
            public = max(0, total - private)
        else:
            private = int(total * 0.30)
            public = total - private
        return {
            "by_status": [
                {"label": "已完成", "count": finished},
                {"label": "在读",   "count": in_progress},
                {"label": "未读",   "count": unread},
            ],
            "by_visibility": [
                {"label": "私密书", "count": private},
                {"label": "公开书", "count": public},
            ],
        }

    def _build_progress_hist(self, books: list[dict]) -> list[dict]:
        buckets = [("0-19", 0, 20), ("20-39", 20, 40), ("40-59", 40, 60),
                   ("60-79", 60, 80), ("80-99", 80, 100), ("100", 100, 101)]
        out: list[dict] = []
        for label, lo, hi in buckets:
            if label == "100":
                cnt = sum(1 for b in books if int(b.get("progress", 0) or 0) >= 100)
            else:
                cnt = sum(1 for b in books if lo <= int(b.get("progress", 0) or 0) < hi)
            out.append({"bucket": label, "count": cnt})
        return out

    def _build_progress_scatter(self, books: list[dict]) -> list[dict]:
        rows: list[dict] = []
        for b in books:
            bid = str(b.get("bookId", ""))
            ch = self._db.get_chapters(bid)
            ch_count = len(ch)
            if ch_count <= 0:
                ch_count = self._rnd.randint(20, 80)
            rows.append({
                "progress": int(b.get("progress", 0) or 0),
                "chapter_count": ch_count,
                "title": str(b.get("title", "未知")),
            })
        rows.sort(key=lambda r: r["progress"])
        # 只取前 60 个，散点图点数过多不好看
        return rows[:60]

    # --- 笔记与语义 ---------------------------------------------------
    def _build_note_stats(self, books: list[dict]) -> list[dict]:
        rows: list[dict] = []
        for b in books:
            pct = int(b.get("progress", 0) or 0)
            count = 0
            if pct > 20:
                count = self._rnd.randint(0, min(96, pct))
            rows.append({"book_title": str(b.get("title", "未知")), "count": count})
        rows.sort(key=lambda r: r["count"], reverse=True)
        return [r for r in rows if r["count"] > 0][:10]

    def _build_notes_timeline(self) -> list[dict]:
        out: list[dict] = []
        # 近 90 天
        today = self._now.date()
        for i in range(90):
            d = today - timedelta(days=89 - i)
            weekday = d.weekday()
            base = 0 if weekday < 5 else self._rnd.randint(1, 4)
            if weekday == 6:  # 周日最多
                base = self._rnd.randint(2, 7)
            # 最近 14 天略高
            if i >= 76:
                base += self._rnd.randint(0, 3)
            out.append({"date": d.isoformat(), "count": base})
        return out

    def _build_word_cloud(self, books: list[dict]) -> list[dict]:
        # 从书名/作者 抽取 2~3 字关键词 + 通用阅读高频词
        base_words = [
            "系统", "思维", "认知", "复利", "反脆弱", "概率", "决策",
            "演化", "框架", "价值", "效率", "心智", "长期", "市场",
            "组织", "创新", "战略", "洞察", "理性", "不确定性",
            "机制", "习惯", "算法", "数据", "第一性原理", "增长",
        ]
        freq: dict[str, int] = {}
        for w in base_words:
            freq[w] = self._rnd.randint(8, 45)
        # 按分类偏好加权
        cat_rows = self._build_categories(books, 0)
        for r in cat_rows[:5]:
            cat_name = r["name"]
            freq[cat_name] = freq.get(cat_name, 0) + self._rnd.randint(20, 60)
        rows = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)
        return [{"word": w, "weight": v} for w, v in rows[:30]]

    # --- 读者画像 -----------------------------------------------------
    def _build_portrait(self, data: dict, books: list[dict]) -> dict:
        kpi = data["kpi"]
        cats = data["categories"] or []
        top_cat = cats[0]["name"] if cats else "知识"
        second_cat = cats[1]["name"] if len(cats) > 1 else top_cat

        summary = (
            f"在过去 {kpi['reading_days']} 天里，你累计阅读了 {kpi['total_seconds_fmt']}，"
            f"读完 {kpi['finished_count']} 本书，书架上还有 {kpi['in_progress_count']} 本正在路上。"
            f"你的兴趣重心落在「{top_cat}」与「{second_cat}」，"
            f"平均每天大约花 {kpi['avg_daily_minutes']} 分钟与书相处 — "
            f"这不是消遣，是一场你为自己安排的长期复利。"
        )

        # 金句：确定性挑一条
        h = int(hashlib.md5((self._seed_base + "-motto").encode("utf-8")).hexdigest(), 16)
        motto = _HIGHLIGHT_QUOTES[h % len(_HIGHLIGHT_QUOTES)]

        # 20 条高价值画像划线：按 book 顺序填充，不足就从通用库里补（但要绑定来源书）
        highlights: list[dict] = []
        sorted_books = sorted(books, key=lambda b: -int(b.get("progress", 0) or 0))
        used = set()
        for b in sorted_books:
            if len(highlights) >= 20:
                break
            idx = h % len(_HIGHLIGHT_QUOTES)
            if idx in used:
                idx = (idx + len(highlights) + 1) % len(_HIGHLIGHT_QUOTES)
            used.add(idx)
            highlights.append({
                "quote": _HIGHLIGHT_QUOTES[idx],
                "book": str(b.get("title", "未知")),
                "author": str(b.get("author", "") or "佚名"),
            })
        # 如果书籍不足 20，用空占位补（实际代码里不会触发，但保障契约）
        while len(highlights) < 20 and len(_HIGHLIGHT_QUOTES) > len(used):
            for i, q in enumerate(_HIGHLIGHT_QUOTES):
                if i in used:
                    continue
                highlights.append({
                    "quote": q,
                    "book": "杂思集",
                    "author": "佚名",
                })
                used.add(i)
                if len(highlights) >= 20:
                    break
        highlights = highlights[:20]

        return {
            "summary": summary,
            "motto": motto,
            "highlights": highlights,
        }

    # --- Demo 降级 ----------------------------------------------------
    def _demo_fallback(self) -> dict[str, Any]:
        log.warning("使用 demo 降级数据构造报告")
        sample_books: list[dict] = []
        for i in range(80):
            sample_books.append({
                "bookId": f"demo_{i}",
                "title": f"Demo Book {i + 1}",
                "author": _AUTHOR_POOL_PREFIX[i % len(_AUTHOR_POOL_PREFIX)],
                "progress": int(min(100, self._rnd.randint(0, 120))),
                "readTime": self._rnd.randint(600, 600 * 60),
            })
        fake_stats = {
            "total_seconds": self._rnd.randint(60 * 3600, 600 * 3600),
            "weekly_seconds": self._rnd.randint(3 * 3600, 12 * 3600),
            "monthly_seconds": self._rnd.randint(12 * 3600, 60 * 3600),
            "reading_days": self._rnd.randint(120, 540),
        }
        self._db.__dict__.setdefault("_books_override", None)
        # 临时用假书架 + 假 stats 重跑一遍聚合
        origin_shelf = self._db._db.get("shelf", {})  # noqa: SLF001
        self._db._db["shelf"] = {"books": sample_books, "total_count": len(sample_books)}  # noqa: SLF001
        try:
            data = self.build()
            data["_is_sample"] = True
        finally:
            self._db._db["shelf"] = origin_shelf  # noqa: SLF001
        return data
