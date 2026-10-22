# YouTube 竞品视频挖掘

基于 Python / Tkinter 的桌面工具，按品牌搜索 YouTube 公开视频，维护本地历史台账，并导出 Excel。默认通过 yt-dlp 读取公开元数据，也可选择 YouTube Data API。

## 功能

- 配置多个品牌、官方频道及补充搜索词，排除官方频道内容。
- 按 2024、2025、2026 年筛选，补扫历史红人频道的视频、Shorts 和直播。
- 按视频 ID 去重、复用详情缓存，保留批次指标快照。
- 查询历史台账，删除和恢复记录，手动修改地区及类型。
- 导出固定 11 列：视频内容、视频链接、发布时间、观看量、点赞量、评论量、频道名称、网红频道链接、粉丝数、地区、类型。

搜索结果和合作提示词只提供研究线索，不代表已经确认商业合作。公开搜索也不能保证发现全部相关视频。

## 安装与启动

需要 Python 3.10+（含 Tcl/Tk）以及可访问 YouTube 的网络。Windows 是主要使用环境；macOS / Linux 需要可用的图形桌面和 Tkinter。

Windows 首次使用：

1. 安装 Python，勾选 Add Python to PATH，保留 Tcl/Tk 组件。
2. 下载并解压本仓库，双击 `安装或更新采集依赖.bat`。
3. 安装完成后双击 `启动竞品红人挖掘.bat`。

也可以使用独立虚拟环境：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python "竞品红人视频挖掘.py"
```

macOS / Linux 激活虚拟环境后，执行 `python -m pip install -r requirements.txt` 和 `python 竞品红人视频挖掘.py`。部分 Linux 发行版需另行安装 Tkinter。

## 基本用法

首次启动品牌框为空，请填写自己的品牌名。官方账号可选，支持 `@账号`、`/@账号`、`/channel/UC…` 和 `/user/…`；补充搜索词使用逗号分隔。

选择年份，点击“开始采集”。默认每词搜索 100 条线索，当次最多新读取 500 条详情，可在界面调整。只有勾选“刷新历史指标”才重新读取已有指标。遇到限流或验证时停止并保留已经保存的记录，稍后再试。

“一键导出当次 Excel”可在采集中途使用；“历史台账”支持按品牌、年份、关键词、批次筛选和导出。更多操作见 [使用说明](使用说明.txt)。

## 数据与隐私

开源版不附带真实品牌配置、采集数据、历史台账、工作簿或第三方依赖目录。首次运行在项目目录创建 `竞品视频台账.sqlite3`；导出保存在 `导出文件/`。运行目录需要可写，请自行备份台账。

这些本地文件已加入 `.gitignore`，请勿强制提交。旧版 `竞品红人挖掘_工作数据.json` 如存在，会被自动导入本地台账。

官方 API 模式需要你自己的 API Key，界面输入使用遮罩且不写入台账。请求会发送至 Google / YouTube；网页采集请求会发送至 YouTube。日志、截图和导出文件分享前仍应人工检查。

Excel 导出使用代码内置的空白样式，无需业务模板。可在本地放置 `竞品youtube视频数据.xlsx` 自定义样式，首行必须与上述 11 列一致；该工作簿默认被忽略。

## 已知限制

- 年份选择目前固定为 2024–2026。
- 网页结构、访问验证和接口配额可能影响采集，缺失指标与明确为 0 的指标分别处理。
- 地区来自频道公开信息，未公开或读取失败会明确标注。
- 观看量、粉丝数等是采集时的公开值，不是视频发布当年的值；网页日期按 UTC 处理。
- 类型由规则推断，可以人工修改。请按平台规则和适用的数据使用要求开展研究。

## 开发与检查

```powershell
python -m unittest discover -s tests -v
python scripts/check_publication.py
```

测试使用临时目录中的虚构记录，覆盖去重、批次快照、删除恢复、人工修改、Excel 导出和错误消息脱敏，不进行真实网络采集。自动检查覆盖常见凭据格式和禁止发布的数据文件；新增代码及附件仍需人工审查。

## 依赖与许可证

本项目代码采用 [MIT License](LICENSE)。依赖独立安装并遵循各自许可证：

- [yt-dlp](https://github.com/yt-dlp/yt-dlp)：公开元数据提取。
- [openpyxl](https://openpyxl.readthedocs.io/)：Excel 读写。
- [pycountry](https://github.com/pycountry/pycountry)：国家代码转换。

不包含视频媒体文件或第三方采集数据。贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；敏感信息处理见 [SECURITY.md](SECURITY.md)。
