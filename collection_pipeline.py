"""Durable discovery and fair, restartable metadata processing (no network at import)."""
import json
from collections import deque
from itertools import zip_longest


LABELS = {"running": "采集中", "complete": "采集完成", "partial": "部分失败，可继续",
          "budget": "达到本次额度，可继续", "stopped": "已停止，可继续",
          "blocked": "访问受限，稍后继续", "failed": "采集失败，可继续"}


def blocked(exc):
    return any(word in str(exc).lower() for word in
               ("429", "not a bot", "too many requests", "验证访问", "unusual traffic"))


class TaskStore:
    def __init__(self, ledger):
        self.ledger = ledger
        with ledger.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS collection_status
                (run_id TEXT PRIMARY KEY, status TEXT, summary TEXT);
                CREATE TABLE IF NOT EXISTS discovery_jobs
                (run_id TEXT, brand TEXT, query TEXT, state TEXT DEFAULT 'pending',
                 count INTEGER DEFAULT 0, error TEXT DEFAULT '',
                 PRIMARY KEY(run_id,brand,query));
                CREATE TABLE IF NOT EXISTS discovery_items
                (run_id TEXT, brand TEXT, video_id TEXT, state TEXT DEFAULT 'pending',
                 attempts INTEGER DEFAULT 0, error TEXT DEFAULT '',
                 PRIMARY KEY(run_id,brand,video_id));
            """)

    def status(self, run, state, summary=None):
        with self.ledger.connect() as db:
            db.execute("INSERT OR REPLACE INTO collection_status VALUES (?,?,?)",
                       (run, state, json.dumps(summary or {}, ensure_ascii=False)))

    def result(self, run):
        with self.ledger.connect() as db:
            row = db.execute("SELECT status,summary FROM collection_status WHERE run_id=?", (run,)).fetchone()
        return (row[0], json.loads(row[1])) if row else ("partial", {})

    def resume(self, config, label):
        with self.ledger.connect() as db:
            rows = db.execute("""SELECT r.run_id,r.config FROM runs r JOIN collection_status s USING(run_id)
                WHERE r.label=? AND s.status != 'complete' ORDER BY r.started_at DESC""", (label,)).fetchall()
        # A changed per-invocation budget must not discard the pending queue.
        comparable = lambda c: {k: v for k, v in c.items() if k != "limit"}
        return next((r for r, c in rows if comparable(json.loads(c)) == comparable(config)), None)

    def seed(self, run, brand, query):
        with self.ledger.connect() as db:
            db.execute("INSERT OR IGNORE INTO discovery_jobs(run_id,brand,query) VALUES (?,?,?)", (run, brand, query))
            return db.execute("SELECT state FROM discovery_jobs WHERE run_id=? AND brand=? AND query=?",
                              (run, brand, query)).fetchone()[0]

    def job(self, run, brand, query, state, count=0, error=""):
        with self.ledger.connect() as db:
            db.execute("UPDATE discovery_jobs SET state=?,count=?,error=? WHERE run_id=? AND brand=? AND query=?",
                       (state, count, error[:500], run, brand, query))

    def discover(self, run, brand, video_id):
        with self.ledger.connect() as db:
            db.execute("INSERT OR IGNORE INTO discovery_items(run_id,brand,video_id) VALUES (?,?,?)",
                       (run, brand, video_id))

    def discover_entries(self, run, brand, entries, check_stop):
        # Keep one connection alive during pagination. Each new ID is still committed
        # immediately; duplicates do not repeatedly open/close SQLite's WAL files.
        with self.ledger.connect() as db:
            known = {r[0] for r in db.execute(
                "SELECT video_id FROM discovery_items WHERE run_id=? AND brand=?", (run, brand))}
            for video_id in entries:
                check_stop()
                if video_id not in known:
                    db.execute("INSERT OR IGNORE INTO discovery_items(run_id,brand,video_id) VALUES (?,?,?)",
                               (run, brand, video_id))
                    db.commit()
                    known.add(video_id)
                yield video_id

    def item(self, run, brand, video_id, state, error="", attempted=False):
        with self.ledger.connect() as db:
            db.execute("UPDATE discovery_items SET state=?,error=?,attempts=attempts+? WHERE run_id=? AND brand=? AND video_id=?",
                       (state, error[:500], int(attempted), run, brand, video_id))

    def pending(self, run, brands):
        queues = []
        with self.ledger.connect() as db:
            for brand in brands:
                rows = db.execute("""SELECT video_id FROM discovery_items WHERE run_id=? AND brand=?
                    AND state IN ('pending','error','missing_date') ORDER BY attempts,rowid""", (run, brand["name"])).fetchall()
                queues.append(deque((brand, row[0]) for row in rows))
        while any(queues):
            # Prefer brands with the least attempts across resumptions as well.
            with self.ledger.connect() as db:
                attempts = dict(db.execute("SELECT brand,SUM(attempts) FROM discovery_items WHERE run_id=? GROUP BY brand", (run,)))
            for q in sorted(queues, key=lambda q: attempts.get(q[0][0]["name"], 0) if q else float('inf')):
                if q:
                    yield q.popleft()

    def counts(self, run):
        with self.ledger.connect() as db:
            items = dict(db.execute("SELECT state,count(*) FROM discovery_items WHERE run_id=? GROUP BY state", (run,)))
            jobs = dict(db.execute("SELECT state,count(*) FROM discovery_jobs WHERE run_id=? GROUP BY state", (run,)))
            unique = db.execute("SELECT count(DISTINCT video_id) FROM discovery_items WHERE run_id=?", (run,)).fetchone()[0]
        return {"unique": unique, "items": items, "jobs": jobs}


def collect_web(ledger, collector, brands, years, depth, limit, run, publish, log, refresh=False, channel_scan=False):
    store = TaskStore(ledger)
    store.status(run, "running")
    state = "complete"
    official, unavailable = {}, set()
    attempts = 0
    fetched_rows = {}
    try:
        # Isolate invalid/unavailable official channels instead of aborting all brands.
        for brand in brands:
            collector.check_stop()
            try:
                official[brand["name"]] = collector.resolve_official(brand["official"])
            except Exception as exc:
                if isinstance(exc, InterruptedError) or blocked(exc):
                    raise
                unavailable.add(brand["name"])
                log(f"{brand['name']} 官方频道解析失败，保留任务：{exc}")
        plans = []
        history = ledger.rows()
        for brand in brands:
            if channel_scan:
                queries = list(dict.fromkeys(row['channel_id'] for row in history
                    if row.get('channel_id') and brand['name'] in row.get('brands', [row.get('brand')])))
            else:
                words = list(dict.fromkeys(brand['words']))
                queries = list(dict.fromkeys(words + [f"{word} {year}" for word in words for year in years]))
            plans.append([(brand, query) for query in queries])
        # Discovery is persisted before any expensive metadata work.
        for group in zip_longest(*plans):
            for task in group:
                if not task:
                    continue
                brand, query = task
                name = brand['name']
                collector.check_stop()
                if store.seed(run, name, query) == 'complete':
                    continue
                count = 0
                try:
                    log(f"发现线索：{name} · {query}")
                    entries = collector.channel_videos(query, depth) if channel_scan else collector.search(query, depth)
                    for video_id in store.discover_entries(run, name, entries, collector.check_stop):
                        count += 1
                    store.job(run, name, query, 'complete', count)
                    log(f"已保存线索：{name} · {query} · {count} 条（每种排序/频道分页上限 {depth}）")
                except Exception as exc:
                    store.job(run, name, query, 'error', count, str(exc))
                    if isinstance(exc, InterruptedError) or blocked(exc):
                        raise
                    log(f"查询失败，继续其他任务：{name} · {exc}")
        log(f"发现阶段结束：{store.counts(run)['unique']} 个不同视频；开始轮流补齐各品牌详情。")
        for brand, video_id in store.pending(run, brands):
            collector.check_stop()
            name = brand['name']
            if name in unavailable:
                continue
            attempted = False
            try:
                saved = ledger.get_video(video_id)
                if saved and saved.get('_deleted'):
                    store.item(run, name, video_id, 'deleted')
                    continue
                row = fetched_rows.get(video_id) or (saved if not refresh else None)
                if not row or not row.get('published_at'):
                    if attempts >= limit:
                        state = 'budget'
                        break
                    attempts += 1
                    attempted = True
                    row = collector.video(brand, video_id)
                    if not row:
                        raise ValueError('视频详情为空')
                    fetched_rows[video_id] = row
                    ledger.store_cache(row)
                if official.get(name) and row.get('channel_id') == official[name]:
                    outcome = 'official'
                elif not row.get('published_at'):
                    outcome = 'missing_date'
                elif row['published_at'][:4] not in {str(y) for y in years}:
                    outcome = 'year'
                elif channel_scan and not any(w.casefold() in (row.get('title','') + ' ' + row.get('description','')).casefold() for w in brand['words']):
                    outcome = 'unrelated'
                else:
                    publish(dict(row, brand=name), run)
                    outcome = 'accepted'
                store.item(run, name, video_id, outcome, attempted=attempted)
            except Exception as exc:
                store.item(run, name, video_id, 'error', str(exc), attempted)
                if isinstance(exc, InterruptedError) or blocked(exc):
                    raise
                log(f"详情未取得，已留待继续：{video_id} · {str(exc)[:180]}")
            finally:
                if attempted:
                    collector.pause()
    except InterruptedError:
        state = 'stopped'
    except Exception as exc:
        state = 'blocked' if blocked(exc) else 'failed'
        log(str(exc))
    finally:
        summary = store.counts(run)
        summary['attempted_this_time'] = attempts
        summary['unresolved_official'] = sorted(unavailable)
        if state == 'complete' and (unavailable or summary['jobs'].get('error') or any(
                summary['items'].get(k) for k in ('pending','error','missing_date'))):
            state = 'partial'
        store.status(run, state, summary)
        ledger.finish_run(run)
        log(f"{LABELS[state]}：发现 {summary['unique']} 个视频，入库 {len(ledger.rows(run))} 条；其余进度已保存。")
    return state, summary
