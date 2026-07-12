# CS2 饰品行情监控系统 v2

## 系统概述
24小时全自动 CS2 饰品市场监控。大盘指数 + 涨跌榜 + 热门系列 + 皮肤K线 + 成交量异动 + QQ邮件通知。

## 技术栈
- 后端: Python Flask (`cs_monitor_backend.py`)
- 前端: 纯 HTML/JS + ECharts (本地加载, `echarts.min.js`)
- 数据源: csqaq.com API (75 endpoints, Token已配置)
- 数据库: SQLite (`cs_monitor.db`)
- 云端: Railway (`cs-monitor-production-9118.up.railway.app`)
- 隧道: Cloudflare Tunnel (手机外网, 无需翻墙)

## 文件清单
| 文件 | 用途 |
|------|------|
| `C:\Users\张泽旭\cs_monitor_backend.py` | Flask 后端 (本地) |
| `C:\Users\张泽旭\cs_monitor_frontend.html` | 前端页面 |
| `C:\Users\张泽旭\echarts.min.js` | ECharts 本地 |
| `C:\Users\张泽旭\cs_monitor.db` | SQLite 数据 |
| `C:\Users\张泽旭\cloudflared.exe` | 隧道工具 |
| `C:\Users\张泽旭\get_tunnel_url.py` | 提取隧道地址 |
| `C:\Users\张泽旭\cs_monitor\` | 云端项目 (GitHub) |

## 功能模块
| Tab | 功能 |
|-----|------|
| 大盘指数 | 23个指数实时卡片 + K线(分时/日线/周线) + MA均线 |
| 涨跌榜 | 涨幅/跌幅/热门成交排行 |
| 热门系列 | 收藏品系列概览 |
| 皮肤监控 | 100+主流饰品成交量异动自动检测 |
| 饰品搜索 | 全平台价格对比 + 历史涨跌 + K线 |
| 自选管理 | 指数自定义阈值监控 |

## 配置
- csqaq API Token: `XGCWH1F7Y8U3P8X7L3F7G6X3`
- QQ邮箱: `1870462637@qq.com`
- 代理: FlClash `127.0.0.1:7890`
- 成交量异动阈值: ¥100↓≥50件, ¥100~1k≥20, ¥1k~10k≥10, ¥10k↑≥5

## 启动命令
- 手动: `D:\python\python.exe cs_monitor_backend.py`
- 访问: `http://127.0.0.1:5000`
- 云端: `https://cs-monitor-production-9118.up.railway.app` (需翻墙)
- 手机: 运行 `get_tunnel_url.py` 获取隧道地址 (无需翻墙, 电脑需开机)
- 开机自启: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\CS2监控自启.bat`

## 部署更新到云端
```bash
# 同步文件
python -c "import shutil,os;h=os.path.expanduser('~');shutil.copy(f'{h}/cs_monitor_backend.py',f'{h}/cs_monitor/app.py');shutil.copy(f'{h}/cs_monitor_frontend.html',f'{h}/cs_monitor/static/index.html')"
# 推送
cd cs_monitor && git add -A && git commit -m "update" && git push
# 部署
railway up --service cs-monitor
```
