"""Export a one-click batch as brand-grouped video, description and summary files."""
import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from youtube_ledger import export_grouped


def export_bundle(rows, directory, template, brands, years):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    names = [b['name'] for b in brands]
    order = ('Mammotion', 'Yarbo', 'Lymow', 'Mowrator')
    names.sort(key=lambda name: (next((i for i, item in enumerate(order) if item.casefold() == name.casefold()), len(order)), name.casefold()))
    prepared = []
    for source in rows:
        row = dict(source)
        associated = row.get('brands') or [row.get('brand')]
        title = (row.get('title') or '').casefold()
        row['brands'] = [name for name in names if name in associated and name.casefold() in title]
        if row['brands']:
            prepared.append(row)
    if not prepared:
        (directory / '采集结果说明.txt').write_text('本次没有符合条件的视频。请查看窗口日志后继续采集。\n', encoding='utf-8')
        return []
    paths = [directory / '视频明细.xlsx', directory / '视频描述.csv', directory / '品牌年度汇总.xlsx']
    export_grouped(prepared, paths[0], template)
    def safe(value):
        text = str(value or '')
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
    with paths[1].open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['品牌', '视频链接', '标题', '发布时间', '完整描述'])
        for name in names:
            for row in prepared:
                if name in row['brands']:
                    writer.writerow([safe(x) for x in (name, row.get('video_url'), row.get('title'),
                                                       row.get('published_at'), row.get('description'))])
    book = Workbook()
    sheet = book.active
    sheet.title = '品牌年度汇总'
    sheet.append(['品牌', '年份', '去重红人数', '视频数', '总播放量', '单条平均播放量'])
    groups = defaultdict(list)
    for row in prepared:
        year = str(row.get('published_at', ''))[:4]
        for name in row['brands']:
            groups[(name, year)].append(row)
    for name in names:
        for year in years:
            group = groups[(name, str(year))]
            channels = {r['channel_id'] for r in group if r.get('channel_id')}
            views = [r['views'] for r in group if isinstance(r.get('views'), (int, float)) and not isinstance(r.get('views'), bool)]
            label = f'{year} 年至今' if int(year) == datetime.now(timezone.utc).year else str(year)
            sheet.append([name, label, len(channels), len(group), sum(views) / 10000 if views else None,
                          sum(views) / len(views) / 10000 if views else None])
            sheet.cell(sheet.max_row, 5).number_format = '0.00"万"'
            sheet.cell(sheet.max_row, 6).number_format = '0.00"万"'
    sheet.append(['说明：播放量为采集时累计值；平均值以取得播放量的视频为分母。'])
    sheet.append(['缺失频道 ID 的视频不计入去重红人数；跨品牌视频分别计数。'])
    sheet.freeze_panes = 'A2'
    for cell in sheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='27445C')
        cell.alignment = Alignment(horizontal='center')
    for column, width in zip('ABCDEF', (20, 16, 18, 16, 22, 24)):
        sheet.column_dimensions[column].width = width
    book.save(paths[2])
    book.close()
    return paths
