"""Optional packaged-app integration check; writes only to a temporary directory."""
import json
import tempfile
import time
from pathlib import Path
from tkinter import messagebox


def check_one_click(app, report_path, config_path):
    result = {'ok': False}
    root = None
    original_dir, original_template = app.APP_DIR, app.TEMPLATE_PATH
    original_error, original_info = messagebox.showerror, messagebox.showinfo
    try:
        brands = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))['brands']
        errors, opened = [], []
        messagebox.showerror = lambda title, text: errors.append([title, text])
        messagebox.showinfo = lambda *args: None
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            app.APP_DIR, app.TEMPLATE_PATH = directory, directory / 'missing.xlsx'
            ledger = app.Ledger(directory / 'test.sqlite3')
            ledger.set_setting('brands', brands)
            root = app.Tk()
            root.withdraw()
            instance = app.App(root, ledger)
            instance.open_results = lambda: opened.append(True)
            test_year = min(max(app.YEARS), 2025)
            for year, variable in instance.year_vars.items():
                variable.set(year == test_year)
            # Small bounded depth verifies the same workflow without a long crawl.
            instance.search_limit.set('1')
            instance.batch_limit.set('2')
            start = time.monotonic()
            instance.start_search()
            if instance.worker is None:
                raise ValueError('一键采集没有启动')
            while instance.worker.is_alive():
                root.update()
                if time.monotonic() - start > 600:
                    instance.stop.set()
                    raise TimeoutError('实时验证超过 10 分钟')
                time.sleep(.1)
            instance.worker.join()
            instance.process_events()
            root.update()
            state, summary = app.TaskStore(ledger).result(instance.current_run)
            rows = ledger.rows(instance.current_run)
            files = Path(summary.get('export_directory', ''))
            expected = ('视频明细.xlsx', '视频描述.csv', '品牌年度汇总.xlsx')
            if state != 'complete' or not rows or errors or not opened or not all((files / name).is_file() for name in expected):
                raise ValueError(f'完整采集未通过：状态 {state}，记录 {len(rows)}，错误 {errors[:1]}')
            result = {'ok': True, 'state': state, 'videos': len(rows), 'export_files': 3,
                      'brand_count': len(brands), 'auto_opened': bool(opened),
                      'seconds': round(time.monotonic()-start, 1)}
            instance.on_close()
            root = None
    except Exception as exc:
        result['error'] = str(exc)[:500]
    finally:
        if root is not None:
            root.destroy()
        app.APP_DIR, app.TEMPLATE_PATH = original_dir, original_template
        messagebox.showerror, messagebox.showinfo = original_error, original_info
    Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result['ok']
