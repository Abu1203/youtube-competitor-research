"""YouTube 竞品视频：自动采集、历史台账、参考格式 Excel 导出。

运行：python 竞品红人视频挖掘.py
默认用 yt-dlp 提取公开网页；官方 API 可选。
"""

from __future__ import annotations

import json
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from tkinter import END, BOTH, LEFT, RIGHT, VERTICAL, X, Y, StringVar, Tk, Text, messagebox
from tkinter import ttk


APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if (APP_DIR / "lib").is_dir():
    sys.path.insert(0, str(APP_DIR / "lib"))
from youtube_ledger import Ledger, channel_details, page_metrics, export_reference, table_values, local_stamp, HEADERS
STATE_PATH = APP_DIR / "竞品红人挖掘_工作数据.json"
API_BASE = "https://www.googleapis.com/youtube/v3/"
YEARS = (2024, 2025, 2026)
EXCEL_HEADERS = [
    "视频内容", "视频链接", "发布时间", "观看量", "点赞量", "评论量",
    "频道名称", "网红频道链接", "粉丝数", "地区", "类型",
]
COOP_WORDS = (
    "sponsored", "paid partnership", "paid promotion", "partnered with",
    "thanks to", "thank you to", "provided by", "sent me", "gifted by",
    "affiliate", "use code", "discount code", "#ad", "#sponsored",
    "赞助", "合作", "推广", "提供产品", "测评合作",
)
TYPE_RULES = (
    ("Review", ("review", "test", "comparison", "vs ", "测评", "评测", "开箱", "对比")),
    ("DIY", ("diy", "install", "setup", "build", "安装", "改装", "自制")),
    ("Tutorial", ("how to", "tutorial", "guide", "教程", "使用方法")),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_brands(text: str) -> list[dict[str, str]]:
    brands = []
    names = set()
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) != 3 or not parts[0] or not parts[2]:
            raise ValueError(f"第 {line_no} 行须为：品牌名 | 官方频道链接或@账号 | 搜索词")
        name, official, terms = parts
        key = name.casefold()
        if key in names:
            raise ValueError(f"品牌重复：{name}")
        names.add(key)
        words = [word.strip() for word in re.split(r"[,，;；]", terms) if word.strip()]
        if not words:
            raise ValueError(f"{name} 没有有效搜索词")
        brands.append({"name": name, "official": official, "query": "|".join(words), "words": words})
    if not brands:
        raise ValueError("请至少填写一个品牌")
    return brands


def account_filter(account: str) -> tuple[str, str] | None:
    value = account.strip()
    if not value:
        return None
    if value.startswith("UC") and re.fullmatch(r"UC[\w-]{22}", value):
        return "id", value
    if value.startswith("@") and re.fullmatch(r"@[\w.\-]+", value):
        return "forHandle", value
    parsed = urllib.parse.urlparse(value if "://" in value else "https://" + value)
    if parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        raise ValueError(f"无效的 YouTube 官方频道：{account}")
    path = urllib.parse.unquote(parsed.path).strip("/")
    if path.startswith("channel/"):
        channel_id = path.split("/")[1]
        if re.fullmatch(r"UC[\w-]{22}", channel_id):
            return "id", channel_id
    if path.startswith("@"):
        return "forHandle", path.split("/")[0]
    if path.startswith("user/"):
        return "forUsername", path.split("/")[1]
    raise ValueError(f"请使用 /channel/UC…、/@账号 或 /user/…：{account}")


def quarter_windows(year: int, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    for month in (1, 4, 7, 10):
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        if start > now:
            break
        next_start = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 10 else datetime(year, month + 3, 1, tzinfo=timezone.utc)
        # 相邻季度允许边界处重复，后续按 video_id 去重；避免漏掉边界秒内的视频。
        end = min(next_start, now)
        yield start, end


def infer_type(title: str) -> str:
    lowered = re.sub(r"#\S+", " ", title).casefold()
    for label, words in TYPE_RULES:
        for word in words:
            if re.search(r"[a-z]", word):
                matched = re.search(r"\b" + re.escape(word.strip()) + r"\b", lowered)
            else:
                matched = word in lowered
            if matched:
                return label
    return "Other"


def evidence_for(title: str, description: str, words: list[str], paid: bool | None) -> str:
    title_low = title.casefold()
    description_low = description.casefold()
    hits = [word for word in words if word.casefold() in title_low or word.casefold() in description_low]
    parts = []
    if hits:
        parts.append("品牌词：" + "、".join(hits[:4]))
    if paid is True:
        parts.append("视频标记了付费推广（未指明品牌）")
    disclosures = [word for word in COOP_WORDS if word in description_low or word in title_low]
    if disclosures:
        parts.append("合作提示词：" + "、".join(disclosures[:4]))
    if not parts:
        parts.append("搜索结果匹配")
    return "；".join(parts)


def safe_int(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(message)


class YouTubeApi:
    def __init__(self, key: str, stop: threading.Event):
        self.key = key
        self.stop = stop
        self.search_calls = 0

    def get(self, endpoint: str, **params) -> dict:
        params["key"] = self.key
        url = API_BASE + endpoint + "?" + urllib.parse.urlencode(params)
        for attempt in range(3):
            if self.stop.is_set():
                raise InterruptedError("采集已停止")
            request = urllib.request.Request(url, headers={"User-Agent": "CompetitorCreatorResearch/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read(2048).decode("utf-8", "replace")
                try:
                    message = json.loads(body)["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    message = body[:250] or str(exc)
                if exc.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                message = message.replace(self.key, "[REDACTED]") if self.key else message
                raise ApiError(exc.code, f"YouTube API {exc.code}: {message}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                message = str(exc).replace(self.key, "[REDACTED]") if self.key else str(exc)
                raise ApiError(0, f"网络连接失败：{message}") from None
        raise ApiError(0, "请求失败")

    def resolve_official(self, account: str) -> str | None:
        filt = account_filter(account)
        if not filt:
            return None
        result = self.get("channels", part="id", **{filt[0]: filt[1]})
        items = result.get("items", [])
        if not items:
            raise ValueError(f"找不到官方频道：{account}")
        return items[0]["id"]

    def search(self, query: str, start: datetime, end: datetime, page_token: str = "") -> dict:
        params = {
            "part": "snippet", "type": "video", "q": query, "maxResults": 50,
            "publishedAfter": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "publishedBefore": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "safeSearch": "none",
        }
        if page_token:
            params["pageToken"] = page_token
        self.search_calls += 1
        return self.get("search", **params)

    def videos(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        data = self.get("videos", part="snippet,statistics,paidProductPlacementDetails", id=",".join(ids))
        return {item["id"]: item for item in data.get("items", [])}

    def channels(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        data = self.get("channels", part="snippet,statistics", id=",".join(ids))
        return {item["id"]: item for item in data.get("items", [])}


def candidates_from_page(brand: dict, official_id: str | None, api: YouTubeApi, items: list[dict]) -> list[dict]:
    ids = []
    seen = set()
    for item in items:
        video_id = (item.get("id") or {}).get("videoId")
        if video_id and video_id not in seen:
            seen.add(video_id)
            ids.append(video_id)
    videos = api.videos(ids)
    channel_ids = list({item.get("snippet", {}).get("channelId") for item in videos.values()} - {None, official_id})
    channels = api.channels(channel_ids)
    collected_at = utc_now()
    rows = []
    for video_id in ids:
        video = videos.get(video_id)
        if not video:
            continue
        snippet = video.get("snippet", {})
        channel_id = snippet.get("channelId", "")
        if not channel_id or channel_id == official_id:
            continue
        channel = channels.get(channel_id, {})
        channel_snippet = channel.get("snippet", {})
        channel_stats = channel.get("statistics", {})
        stats = video.get("statistics", {})
        title = snippet.get("title", "")
        description = snippet.get("description", "")
        paid = video.get("paidProductPlacementDetails", {}).get("hasPaidProductPlacement")
        rows.append({
            "key": brand["name"] + "::" + video_id,
            "brand": brand["name"], "video_id": video_id,
            "title": title, "description": description,
            "video_url": "https://www.youtube.com/watch?v=" + video_id,
            "published_at": snippet.get("publishedAt", ""),
            "views": safe_int(stats.get("viewCount")),
            "likes": safe_int(stats.get("likeCount")),
            "comments": safe_int(stats.get("commentCount")),
            "channel_id": channel_id,
            "channel_name": channel_snippet.get("title") or snippet.get("channelTitle", ""),
            "channel_url": "https://www.youtube.com/channel/" + channel_id,
            "subscribers": None if channel_stats.get("hiddenSubscriberCount") else safe_int(channel_stats.get("subscriberCount")),
            "region": channel_snippet.get("country", ""),
            "video_type": infer_type(title),
            "evidence": evidence_for(title, description, brand["words"], paid),
            "paid_promotion": paid is True,
            "collected_at": collected_at,
        })
    return rows


class WebCollector:
    """无 Key 的公开网页采集，只读取元数据，不下载视频。"""

    def __init__(self, stop: threading.Event, log, ledger=None, refresh=False):
        import yt_dlp
        self.yt_dlp = yt_dlp
        self.stop = stop
        self.log = log
        self.ledger = ledger
        self.refresh = refresh
        self.channel_cache = {}
        self.options = {
            "quiet": True, "skip_download": True, "ignore_no_formats_error": True,
            "socket_timeout": 20, "retries": 1, "extractor_retries": 1,
            "cachedir": False, "logger": self,
            "extractor_args": {"youtube": {"skip": ["hls", "dash"], "player_skip": ["js"]}},
            "sleep_interval_requests": 1,
        }

    def debug(self, message):
        pass

    def warning(self, message):
        # 格式/JavaScript 警告与本工具不下载视频的用途无关；关键错误仍呈现。
        if not any(word in message for word in ("JavaScript runtime", "formats may", "formats have")):
            self.log("网页提取提示：" + message)

    def error(self, message):
        self.log("网页提取错误：" + message)

    def check_stop(self):
        if self.stop.is_set():
            raise InterruptedError("采集已停止")

    def extract(self, url: str, flat=False):
        self.check_stop()
        opts = dict(self.options)
        if flat:
            opts.update({"extract_flat": True, "playlistend": 1})
        with self.yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def resolve_official(self, account: str) -> str | None:
        parsed = account_filter(account)
        if not parsed:
            return None
        if parsed[0] == "id":
            return parsed[1]
        url = "https://www.youtube.com/" + (parsed[1] if parsed[0] == "forHandle" else "user/" + parsed[1])
        data = self.extract(url + "/videos", flat=True)
        channel_id = data.get("channel_id") or data.get("id")
        if not channel_id or not channel_id.startswith("UC"):
            raise ValueError(f"无法识别官方频道，请改填 /channel/UC… 链接：{account}")
        return channel_id

    def search(self, query: str, limit: int):
        # 用网页的正常搜索分页；年份关键词只是扩充线索，详情日期才是筛选依据。
        options = dict(self.options, extract_flat=True, lazy_playlist=True)
        with self.yt_dlp.YoutubeDL(options) as ydl:
            data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            for item in (data or {}).get("entries", []):
                self.check_stop()
                if item and item.get("id"):
                    yield item["id"]

    def video(self, brand: dict, video_id: str) -> dict | None:
        data = self.extract("https://www.youtube.com/watch?v=" + video_id)
        if not data:
            return None
        raw_date = data.get("upload_date") or ""
        date_text = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if re.fullmatch(r"\d{8}", raw_date) else ""
        title = data.get("title") or ""
        description = data.get("description") or ""
        channel_id = data.get("channel_id") or ""
        row = {
            "key": brand["name"] + "::" + video_id, "brand": brand["name"],
            "video_id": video_id, "title": title, "description": description,
            "video_url": "https://www.youtube.com/watch?v=" + video_id,
            "published_at": date_text,
            "views": safe_int(data.get("view_count")), "likes": safe_int(data.get("like_count")),
            "comments": safe_int(data.get("comment_count")),
            "channel_id": channel_id, "channel_name": data.get("channel") or data.get("uploader") or "",
            "channel_url": data.get("channel_url") or ("https://www.youtube.com/channel/" + channel_id if channel_id else ""),
            "subscribers": safe_int(data.get("channel_follower_count")),
            "region": "", "video_type": infer_type(title),
            "evidence": evidence_for(title, description, brand["words"], None),
            "collected_at": utc_now(), "source": "网页 / yt-dlp",
        }
        return self.enrich_row(row)

    def enrich_row(self, row):
        row = dict(row)
        channel_id = row.get("channel_id")
        if channel_id:
            info = self.channel_cache.get(channel_id)
            if info is None and self.ledger and not self.refresh:
                info = self.ledger.channel(channel_id)
            if info is None:
                self.check_stop()
                try:
                    info = channel_details(channel_id)
                    if self.ledger:
                        self.ledger.store_channel(channel_id, info)
                except Exception as exc:
                    self.log(f"频道地区暂未取得：{channel_id} · {str(exc)[:160]}")
                    info = {"region": "获取失败"}
            self.channel_cache[channel_id] = info
            if not row.get("_manual", {}).get("region"):
                if row.get("region") in (None, "", "未公开", "获取失败") or self.refresh:
                    row["region"] = info.get("region", "未公开")
                    row["region_source"] = info.get("region_source", "")
            if row.get("subscribers") is None:
                row["subscribers"] = info.get("subscribers")
            if row.get("video_type") in (None, "", "Other", "待分类") and info.get("channel_description"):
                row["video_type"] = infer_type(info["channel_description"])
        if any(row.get(field) is None for field in ("views", "likes", "comments")) or not row.get("published_at"):
            if not row.get("_page_fallback_done") or self.refresh:
                self.check_stop()
                try:
                    values = page_metrics(row["video_id"])
                    for field, value in values.items():
                        if row.get(field) in (None, "") and value not in (None, ""):
                            row[field] = value
                    row["_page_fallback_done"] = True
                except Exception as exc:
                    self.log(f"视频补充字段暂未取得：{row['video_id']} · {str(exc)[:160]}")
        if not row.get("video_type") or row.get("video_type") == "待分类":
            row["video_type"] = infer_type(row.get("title", ""))
        return row

    def channel_videos(self, channel_id: str, limit: int):
        options = dict(self.options, extract_flat=True, lazy_playlist=True, playlistend=limit)
        for tab in ("videos", "shorts", "streams"):
            self.check_stop()
            try:
                with self.yt_dlp.YoutubeDL(options) as ydl:
                    data = ydl.extract_info(f"https://www.youtube.com/channel/{channel_id}/{tab}", download=False)
                    for item in (data or {}).get("entries", []):
                        self.check_stop()
                        if item and item.get("id"):
                            yield item["id"]
            except Exception as exc:
                text = str(exc).lower()
                if "tab" in text and ("does not have" in text or "unavailable" in text):
                    self.log(f"{channel_id} 没有 {tab} 分页，跳过。")
                else:
                    raise

    def pause(self):
        if self.stop.wait(random.uniform(1.5, 3)):
            raise InterruptedError("采集已停止")



from tkinter import BooleanVar, Canvas, HORIZONTAL

DB_PATH = APP_DIR / "竞品视频台账.sqlite3"
TEMPLATE_PATH = APP_DIR / "竞品youtube视频数据.xlsx"


def export_excel(rows, path):
    export_reference(rows, path, TEMPLATE_PATH)


class App:
    def __init__(self, root, ledger=None):
        self.root = root
        self.root.title("YouTube 竞品视频采集与历史台账")
        width = min(1480, self.root.winfo_screenwidth() - 70)
        height = min(940, self.root.winfo_screenheight() - 90)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(1160, width), min(760, height))
        self.ledger = ledger or Ledger(DB_PATH, STATE_PATH)
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.worker = None
        self.current_run = self.ledger.setting("last_run")
        self.brand_rows = []
        self.collect_mode = StringVar(value="网页采集（免 Key）")
        self.api_key = StringVar()
        self.search_limit = StringVar(value="100")
        self.batch_limit = StringVar(value="500")
        self.refresh_existing = BooleanVar(value=False)
        self.year_vars = {year: BooleanVar(value=True) for year in YEARS}
        self.history_brand = StringVar(value="全部品牌")
        self.history_year = StringVar(value="全部年份")
        self.history_batch = StringVar(value="全部历史")
        self.history_keyword = StringVar()
        self.show_deleted = BooleanVar(value=False)
        self.region_edit = StringVar()
        self.type_edit = StringVar()
        self.status_text = StringVar(value="就绪。结果自动加入台账；再次搜索会复用历史详情。")
        self.current_rows = []
        self.history_rows = []
        self.run_map = {}
        self.build_ui()
        self.refresh_tables()
        self.root.after(120, self.process_events)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=BOTH, expand=True)
        ttk.Label(outer, text="YouTube 竞品视频采集", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(outer, text="输入品牌 · 自动收集 · 查看台账 · 一键导出当次 Excel", foreground="#53616E").pack(anchor="w", pady=(3, 10))

        setup = ttk.LabelFrame(outer, text="品牌设置", padding=8)
        setup.pack(fill=X)
        labels = ttk.Frame(setup)
        labels.pack(fill=X)
        for col, (text, weight) in enumerate((("品牌名", 2), ("官方 YouTube 账号或链接（可选）", 4), ("补充搜索词（可选，用逗号分隔）", 4))):
            labels.columnconfigure(col, weight=weight, uniform="brands")
            ttk.Label(labels, text=text).grid(row=0, column=col, sticky="w", padx=3)
        ttk.Button(labels, text="＋ 新增品牌", command=self.add_brand, width=14).grid(row=0, column=3, padx=3)
        self.brand_canvas = Canvas(setup, height=104, highlightthickness=0)
        bar = ttk.Scrollbar(setup, orient=VERTICAL, command=self.brand_canvas.yview)
        self.brand_canvas.configure(yscrollcommand=bar.set)
        self.brand_canvas.pack(side=LEFT, fill=X, expand=True)
        bar.pack(side=RIGHT, fill=Y)
        self.brand_container = ttk.Frame(self.brand_canvas)
        window = self.brand_canvas.create_window((0, 0), window=self.brand_container, anchor="nw")
        self.brand_container.bind("<Configure>", lambda _e: self.brand_canvas.configure(scrollregion=self.brand_canvas.bbox("all")))
        self.brand_canvas.bind("<Configure>", lambda e: self.brand_canvas.itemconfigure(window, width=e.width))
        saved = self.ledger.setting("brands", [{"name": "", "official": "", "terms": ""}])
        for brand in saved:
            self.add_brand(brand)

        controls = ttk.Frame(outer)
        controls.pack(fill=X, pady=(9, 5))
        mode = ttk.Combobox(controls, values=("网页采集（免 Key）", "官方 API"), textvariable=self.collect_mode, state="readonly", width=20)
        mode.pack(side=LEFT)
        mode.bind("<<ComboboxSelected>>", self.mode_changed)
        self.depth_label = ttk.Label(controls, text="每词搜索条数")
        self.depth_label.pack(side=LEFT, padx=(12, 5))
        ttk.Spinbox(controls, from_=1, to=1000, textvariable=self.search_limit, width=6).pack(side=LEFT)
        self.limit_label = ttk.Label(controls, text="当次详情采集上限")
        self.limit_label.pack(side=LEFT, padx=(12, 5))
        ttk.Spinbox(controls, from_=1, to=10000, textvariable=self.batch_limit, width=6).pack(side=LEFT)
        for year in YEARS:
            ttk.Checkbutton(controls, text=str(year), variable=self.year_vars[year]).pack(side=LEFT, padx=7)
        ttk.Checkbutton(controls, text="刷新历史指标", variable=self.refresh_existing).pack(side=LEFT, padx=10)
        self.key_frame = ttk.Frame(outer)
        ttk.Label(self.key_frame, text="YouTube API Key").pack(side=LEFT)
        ttk.Entry(self.key_frame, textvariable=self.api_key, show="•", width=50).pack(side=LEFT, padx=8)

        buttons = ttk.Frame(outer)
        buttons.pack(fill=X, pady=(4, 9))
        self.start_button = ttk.Button(buttons, text="开始采集", command=self.start_search)
        self.start_button.pack(side=LEFT)
        self.channel_button = ttk.Button(buttons, text="补扫历史红人频道", command=lambda: self.start_search(True))
        self.channel_button.pack(side=LEFT, padx=8)
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop_search, state="disabled")
        self.stop_button.pack(side=LEFT)
        ttk.Button(buttons, text="一键导出当次 Excel", command=self.export_current).pack(side=LEFT, padx=14)
        self.current_count = StringVar()
        ttk.Label(buttons, textvariable=self.current_count).pack(side=RIGHT)

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill=BOTH, expand=True)
        current = ttk.Frame(self.tabs, padding=8)
        history = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(current, text="当次采集数据")
        self.tabs.add(history, text="历史台账")
        current_bar = ttk.Frame(current)
        current_bar.pack(fill=X, pady=(0, 6))
        ttk.Label(current_bar, text="当次结果按视频 ID 去重，自动纳入台账。", foreground="#53616E").pack(side=LEFT)
        ttk.Button(current_bar, text="删除选中数据", command=self.delete_selected).pack(side=RIGHT)
        self.current_table = self.make_table(current)

        filters = ttk.Frame(history)
        filters.pack(fill=X, pady=(0, 5))
        self.brand_filter = ttk.Combobox(filters, textvariable=self.history_brand, state="readonly", width=18)
        self.brand_filter.pack(side=LEFT)
        self.year_filter = ttk.Combobox(filters, textvariable=self.history_year, values=("全部年份", "2024", "2025", "2026"), state="readonly", width=12)
        self.year_filter.pack(side=LEFT, padx=6)
        self.batch_filter = ttk.Combobox(filters, textvariable=self.history_batch, state="readonly", width=32)
        self.batch_filter.pack(side=LEFT)
        for combo in (self.brand_filter, self.year_filter, self.batch_filter):
            combo.bind("<<ComboboxSelected>>", lambda _e: self.refresh_history())
        ttk.Entry(filters, textvariable=self.history_keyword, width=24).pack(side=LEFT, padx=6)
        ttk.Button(filters, text="搜索台账", command=self.refresh_history).pack(side=LEFT)
        ttk.Checkbutton(filters, text="查看已删除", variable=self.show_deleted, command=self.refresh_history).pack(side=RIGHT)
        hist_buttons = ttk.Frame(history)
        hist_buttons.pack(fill=X, pady=(0, 6))
        ttk.Button(hist_buttons, text="导出当前历史视图", command=self.export_history).pack(side=LEFT)
        self.fill_button = ttk.Button(hist_buttons, text="补齐历史缺失字段", command=self.start_fill)
        self.fill_button.pack(side=LEFT, padx=8)
        ttk.Button(hist_buttons, text="删除选中", command=self.delete_selected).pack(side=LEFT)
        ttk.Button(hist_buttons, text="恢复选中", command=self.restore_selected).pack(side=LEFT, padx=8)
        self.history_count = StringVar()
        ttk.Label(hist_buttons, textvariable=self.history_count).pack(side=RIGHT)
        self.history_table = self.make_table(history)
        self.tabs.bind("<<NotebookTabChanged>>", lambda _e: self.show_selected())

        edit = ttk.Frame(outer)
        edit.pack(fill=X, pady=(8, 5))
        ttk.Label(edit, text="地区").pack(side=LEFT)
        ttk.Entry(edit, textvariable=self.region_edit, width=13).pack(side=LEFT, padx=(5, 14))
        ttk.Label(edit, text="类型").pack(side=LEFT)
        ttk.Combobox(edit, textvariable=self.type_edit, values=("Review", "DIY", "Tutorial", "Unboxing", "Demo", "Other"), width=15).pack(side=LEFT, padx=5)
        ttk.Button(edit, text="保存修改", command=self.save_edits).pack(side=LEFT, padx=8)
        ttk.Button(edit, text="打开视频", command=self.open_video).pack(side=LEFT)
        self.details = Text(outer, height=4, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.details.pack(fill=X)
        self.logs = Text(outer, height=3, wrap="word", state="disabled", font=("Consolas", 9))
        self.logs.pack(fill=X, pady=(6, 0))
        ttk.Label(outer, textvariable=self.status_text).pack(anchor="w", pady=(6, 0))

    def add_brand(self, values=None):
        values = values if isinstance(values, dict) else {}
        frame = ttk.Frame(self.brand_container)
        frame.pack(fill=X, pady=2)
        name = StringVar(value=values.get("name", ""))
        official = StringVar(value=values.get("official", ""))
        terms = StringVar(value=values.get("terms", ""))
        for col, (variable, weight) in enumerate(((name, 2), (official, 4), (terms, 4))):
            frame.columnconfigure(col, weight=weight, uniform="brands")
            ttk.Entry(frame, textvariable=variable, width=1).grid(row=0, column=col, sticky="ew", padx=3)
        row = (frame, name, official, terms)
        ttk.Button(frame, text="移除品牌框", command=lambda: self.remove_brand(row), width=14).grid(row=0, column=3, padx=3)
        self.brand_rows.append(row)

    def remove_brand(self, row):
        row[0].destroy()
        self.brand_rows.remove(row)

    def brand_settings(self):
        settings = []
        seen = set()
        for _frame, name_var, official_var, terms_var in self.brand_rows:
            name, official, terms = (v.get().strip() for v in (name_var, official_var, terms_var))
            if not any((name, official, terms)):
                continue
            if not name:
                raise ValueError("每个品牌框需填写品牌名")
            if name.casefold() in seen:
                raise ValueError(f"品牌名重复：{name}")
            seen.add(name.casefold())
            account_filter(official)
            settings.append({"name": name, "official": official, "terms": terms})
        if not settings:
            raise ValueError("请至少填写一个品牌名")
        return settings

    def collect_brands(self):
        settings = self.brand_settings()
        self.ledger.set_setting("brands", settings)
        return [{"name": item["name"], "official": item["official"],
                 "words": list(dict.fromkeys([item["name"]] + [word.strip() for word in re.split(r"[,，;；]", item["terms"]) if word.strip()])),
                 "query": "|".join(list(dict.fromkeys([item["name"]] + [word.strip() for word in re.split(r"[,，;；]", item["terms"]) if word.strip()]))) }
                for item in settings]

    def make_table(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill=BOTH, expand=True)
        table = ttk.Treeview(frame, columns=tuple(range(11)), show="headings", selectmode="extended")
        widths = (320, 260, 108, 90, 85, 85, 155, 290, 95, 85, 95)
        for i, (header, width) in enumerate(zip(HEADERS, widths)):
            table.heading(i, text=header)
            table.column(i, width=width, minwidth=65, stretch=False)
        ybar = ttk.Scrollbar(frame, orient=VERTICAL, command=table.yview)
        xbar = ttk.Scrollbar(frame, orient=HORIZONTAL, command=table.xview)
        table.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        table.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        table.bind("<<TreeviewSelect>>", self.show_selected)
        table.bind("<Double-1>", lambda _e: self.open_video())
        return table

    def mode_changed(self, _event=None):
        web = self.collect_mode.get().startswith("网页")
        self.depth_label.configure(text="每词搜索条数" if web else "搜索页数/季度")
        self.limit_label.configure(text="当次详情采集上限" if web else "当次搜索调用上限")
        self.search_limit.set("100" if web else "2")
        self.batch_limit.set("500" if web else "80")
        if web:
            self.key_frame.pack_forget()
        else:
            self.key_frame.pack(fill=X, pady=5, before=self.tabs)

    def active_table(self):
        return self.current_table if self.tabs.index(self.tabs.select()) == 0 else self.history_table

    def selected_rows(self):
        return [self.ledger.get_video(video_id) for video_id in self.active_table().selection()]

    def show_selected(self, _event=None):
        rows = self.selected_rows()
        row = rows[0] if rows else None
        self.region_edit.set(row.get("region", "") if row else "")
        self.type_edit.set(row.get("video_type", "") if row else "")
        content = "选中视频可查看简介与来源，或修改地区和类型。"
        if row:
            content = (f"关联品牌：{'、'.join(row.get('brands', [row.get('brand', '')]))}    地区来源：{row.get('region_source', '频道公开信息/人工填写')}\n"
                       f"{row.get('evidence', '')}\n{row.get('description', '')[:900]}")
        self.details.configure(state="normal")
        self.details.delete("1.0", END)
        self.details.insert("1.0", content)
        self.details.configure(state="disabled")

    def fill_table(self, table, rows):
        selected = set(table.selection())
        table.delete(*table.get_children())
        for row in rows:
            table.insert("", END, iid=row["video_id"], values=table_values(row))
        table.selection_set([video_id for video_id in selected if table.exists(video_id)])

    def refresh_tables(self):
        self.current_rows = self.ledger.rows(self.current_run) if self.current_run else []
        self.fill_table(self.current_table, self.current_rows)
        self.current_count.set(f"当次 {len(self.current_rows)} 条 · 台账 {len(self.ledger.rows())} 条")
        brands = sorted({name for row in self.ledger.rows() for name in row.get("brands", [row.get("brand", "")]) if name})
        self.brand_filter.configure(values=["全部品牌"] + brands)
        self.run_map = {}
        for run in self.ledger.runs():
            label = f"{run['run_id'][:15]} · {run['label']} · {run['run_id'][-6:]}"
            self.run_map[label] = run["run_id"]
        self.batch_filter.configure(values=["全部历史"] + list(self.run_map))
        self.refresh_history()

    def refresh_history(self):
        run_id = self.run_map.get(self.history_batch.get())
        rows = self.ledger.rows(run_id, deleted=self.show_deleted.get())
        name, year, keyword = self.history_brand.get(), self.history_year.get(), self.history_keyword.get().strip().casefold()
        self.history_rows = [row for row in rows
            if (name == "全部品牌" or name in row.get("brands", [row.get("brand")]))
            and (year == "全部年份" or row.get("published_at", "").startswith(year))
            and (not keyword or keyword in (row.get("title", "") + " " + row.get("channel_name", "") + " " + row.get("video_id", "")).casefold())]
        self.fill_table(self.history_table, self.history_rows)
        self.history_count.set(f"当前历史视图 {len(self.history_rows)} 条")

    def delete_selected(self):
        ids = self.active_table().selection()
        if ids:
            self.ledger.set_deleted(ids)
            self.refresh_tables()
            self.status_text.set(f"已删除 {len(ids)} 条；台账保留删除标记，后续采集会跳过。可在历史台账恢复。")

    def restore_selected(self):
        if self.tabs.index(self.tabs.select()) != 1 or not self.show_deleted.get():
            messagebox.showinfo("恢复数据", "在历史台账勾选“查看已删除”，再选择需要恢复的数据。")
            return
        ids = self.history_table.selection()
        if ids:
            self.ledger.set_deleted(ids, False)
            self.refresh_tables()

    def save_edits(self):
        rows = self.selected_rows()
        if len(rows) != 1:
            messagebox.showinfo("选择视频", "请选择一条视频再修改地区和类型。")
            return
        self.ledger.edit(rows[0]["video_id"], self.region_edit.get(), self.type_edit.get())
        self.refresh_tables()

    def open_video(self):
        rows = self.selected_rows()
        if rows:
            webbrowser.open(rows[0]["video_url"])

    def do_export(self, rows, name):
        if not rows:
            messagebox.showinfo("没有可导出数据", "当前没有数据。采集期间也可导出已经收集到的记录。")
            return
        directory = APP_DIR / "导出文件"
        directory.mkdir(exist_ok=True)
        path = directory / f"{name}_{local_stamp()}_{len(rows)}条.xlsx"
        try:
            export_excel(rows, path)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self.status_text.set(f"已导出 {len(rows)} 条：{path}")
        messagebox.showinfo("导出完成", f"共 {len(rows)} 条，11 列与参考 Excel 一致。\n{path}")

    def export_current(self):
        self.do_export(self.ledger.rows(self.current_run) if self.current_run else [], "当次采集")

    def export_history(self):
        self.do_export(self.history_rows, "历史台账")

    def begin_run(self, config, label):
        self.stop.clear()
        self.current_run = self.ledger.create_run(config, label)
        for button in (self.start_button, self.channel_button, self.fill_button):
            button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.tabs.select(0)
        self.refresh_tables()

    def start_search(self, channel_scan=False):
        if self.worker and self.worker.is_alive():
            return
        try:
            brands = self.collect_brands()
            years = [year for year in YEARS if self.year_vars[year].get()]
            depth, limit = int(self.search_limit.get()), int(self.batch_limit.get())
            web = self.collect_mode.get().startswith("网页")
            if not years or not 1 <= depth <= (1000 if web else 10) or not 1 <= limit <= (10000 if web else 100):
                raise ValueError("请选择年份并填写有效的搜索深度与当次上限。")
            if web:
                __import__("yt_dlp")
            elif not self.api_key.get().strip():
                raise ValueError("官方 API 模式需要 API Key；网页模式无需 Key。")
            if channel_scan and not web:
                raise ValueError("补扫红人频道请使用网页模式。")
            if channel_scan and not any(name == brand["name"] for row in self.ledger.rows() for name in row.get("brands", [row.get("brand")]) for brand in brands):
                raise ValueError("所填品牌还没有历史红人频道，请先采集视频。")
        except (ValueError, ImportError) as exc:
            messagebox.showerror("设置有误", str(exc))
            return
        config = {"brands": brands, "years": years, "mode": self.collect_mode.get()}
        self.begin_run(config, "频道补扫" if channel_scan else "关键词采集")
        args = (brands, years, depth, limit, self.current_run, self.refresh_existing.get(), channel_scan)
        target = self.run_web if web else self.run_api
        if not web:
            args = (self.api_key.get().strip(),) + args[:-1]
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    def start_fill(self):
        if self.worker and self.worker.is_alive():
            return
        rows = [row for row in self.history_rows if any(row.get(field) is None for field in ("views", "likes", "comments", "subscribers")) or row.get("region") in (None, "", "获取失败")]
        if not rows or self.show_deleted.get():
            messagebox.showinfo("字段补齐", "当前历史视图没有需要补齐的字段。未公开的字段会保留说明。")
            return
        self.begin_run({}, "历史字段补齐")
        self.worker = threading.Thread(target=self.run_fill, args=(rows, self.current_run), daemon=True)
        self.worker.start()

    def stop_search(self):
        self.stop.set()
        self.status_text.set("正在停止；当前网络请求结束后生效，已采集记录已保存。")

    def log(self, message):
        self.events.put(("log", message))

    def publish(self, row, run_id):
        if self.ledger.add_video(row, run_id):
            self.events.put(("changed", None))

    def run_web(self, brands, years, depth, limit, run_id, refresh=False, channel_scan=False):
        attempted = 0
        seen_pairs, details = set(), {}
        try:
            collector = WebCollector(self.stop, self.log, self.ledger, refresh)
            history = self.ledger.rows()
            for brand in brands:
                official_id = collector.resolve_official(brand["official"])
                if channel_scan:
                    queries = list(dict.fromkeys(row.get("channel_id") for row in history if row.get("channel_id") and brand["name"] in row.get("brands", [row.get("brand")])))
                else:
                    queries = list(dict.fromkeys(query for word in brand["words"] for query in [word] + [f"{word} {year}" for year in years]))
                for query in queries:
                    collector.check_stop()
                    self.log(f"{'补扫频道' if channel_scan else '搜索'}：{query}")
                    found = collector.channel_videos(query, depth) if channel_scan else collector.search(query, depth)
                    for video_id in found:
                        pair = (brand["name"], video_id)
                        if pair in seen_pairs:
                            continue
                        seen_pairs.add(pair)
                        saved = self.ledger.get_video(video_id)
                        if saved and saved.get("_deleted"):
                            continue
                        row = details.get(video_id) or (saved if not refresh else None)
                        fetched = False
                        try:
                            if not row:
                                if attempted >= limit:
                                    self.log(f"达到当次详情上限 {limit}；已保存进度，下次可继续。")
                                    return
                                attempted += 1
                                fetched = True
                                row = collector.video(brand, video_id)
                            elif row.get("region") in (None, "", "获取失败") or not row.get("_page_fallback_done") and any(row.get(field) is None for field in ("views", "likes", "comments", "subscribers")):
                                row = collector.enrich_row(row)
                            if not row:
                                continue
                            row = dict(row, brand=brand["name"])
                            details[video_id] = row
                            self.ledger.store_cache(row)
                        except Exception as exc:
                            if self.stop.is_set():
                                raise InterruptedError("采集已停止") from exc
                            if any(word in str(exc).lower() for word in ("429", "not a bot", "too many requests", "验证访问")):
                                raise RuntimeError("YouTube 网页限流或要求验证；已保存数据，请稍后继续。") from exc
                            self.log(f"{video_id} 未取得详情：{str(exc)[:180]}")
                            collector.pause()
                            continue
                        date = row.get("published_at", "")
                        if row.get("channel_id") == official_id:
                            continue
                        if not date or date[:4] not in [str(year) for year in years]:
                            if fetched:
                                collector.pause()
                            continue
                        if channel_scan and not any(word.casefold() in (row.get("title", "") + " " + row.get("description", "")).casefold() for word in brand["words"]):
                            if fetched:
                                collector.pause()
                            continue
                        self.publish(row, run_id)
                        self.log(f"{'新读取' if fetched else '复用台账'} · {brand['name']} · {row.get('channel_name','')} · {row.get('title','')[:65]}")
                        if fetched:
                            collector.pause()
        except Exception as exc:
            if not isinstance(exc, InterruptedError):
                self.events.put(("error", str(exc)))
        finally:
            self.ledger.finish_run(run_id)
            self.events.put(("finished", run_id))

    def run_api(self, key, brands, years, depth, limit, run_id, refresh=False):
        api = YouTubeApi(key, self.stop)
        try:
            enricher = WebCollector(self.stop, self.log, self.ledger, refresh)
            for brand in brands:
                official = api.resolve_official(brand["official"])
                for year in years:
                    for start, end in quarter_windows(year):
                        token = ""
                        for _page in range(depth):
                            if api.search_calls >= limit:
                                return
                            result = api.search(brand["query"], start, end, token)
                            fresh = []
                            for item in result.get("items", []):
                                video_id = item.get("id", {}).get("videoId")
                                if not video_id:
                                    continue
                                old = self.ledger.get_video(video_id)
                                if old and old.get("_deleted"):
                                    continue
                                if old and not refresh:
                                    if old.get("channel_id") != official and old.get("published_at", "")[:4] in [str(value) for value in years]:
                                        self.publish(dict(enricher.enrich_row(old), brand=brand["name"]), run_id)
                                else:
                                    fresh.append(item)
                            for row in candidates_from_page(brand, official, api, fresh):
                                self.publish(enricher.enrich_row(row), run_id)
                            token = result.get("nextPageToken", "")
                            if not token:
                                break
        except Exception as exc:
            if not isinstance(exc, InterruptedError):
                self.events.put(("error", str(exc)))
        finally:
            self.ledger.finish_run(run_id)
            self.events.put(("finished", run_id))

    def run_fill(self, rows, run_id):
        try:
            collector = WebCollector(self.stop, self.log, self.ledger)
            for row in rows:
                collector.check_stop()
                self.publish(collector.enrich_row(row), run_id)
                self.log(f"字段补齐：{row.get('channel_name','')} · {row.get('title','')[:70]}")
                collector.pause()
        except Exception as exc:
            if not isinstance(exc, InterruptedError):
                self.events.put(("error", str(exc)))
        finally:
            self.ledger.finish_run(run_id)
            self.events.put(("finished", run_id))

    def process_events(self):
        dirty = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "changed":
                    dirty = True
                elif kind == "log":
                    self.logs.configure(state="normal")
                    self.logs.insert(END, time.strftime("%H:%M:%S") + "  " + payload + "\n")
                    self.logs.see(END)
                    self.logs.configure(state="disabled")
                    self.status_text.set(payload)
                elif kind == "error":
                    messagebox.showerror("采集提示", payload)
                elif kind == "finished":
                    for button in (self.start_button, self.channel_button, self.fill_button):
                        button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    count = len(self.ledger.rows(payload))
                    self.status_text.set(f"{'已停止' if self.stop.is_set() else '当次结束'}：{count} 条已入台账，可一键导出。")
                    dirty = True
        except queue.Empty:
            pass
        if dirty:
            self.refresh_tables()
        self.root.after(120, self.process_events)

    def on_close(self):
        self.stop.set()
        try:
            self.ledger.set_setting("brands", self.brand_settings())
        except ValueError:
            pass
        self.root.destroy()


def main():
    root = Tk()
    style = ttk.Style(root)
    style.theme_use("vista" if "vista" in style.theme_names() else "clam")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
