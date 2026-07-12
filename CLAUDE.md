# CS2 饰品行情监控系统

## 项目概述
24小时监控 CS2 饰品市场行情，数据来自 csqaq.com API。
大盘指数 + 具体饰品价格 + K线图表 + QQ邮箱异动通知。

## 文件结构
- `C:\Users\张泽旭\cs_monitor_backend.py` — Flask 后端 (本地运行)
- `C:\Users\张泽旭\cs_monitor_frontend.html` — 可视化前端
- `C:\Users\张泽旭\echarts.min.js` — 本地 ECharts (不用CDN)
- `C:\Users\张泽旭\cs_monitor.db` — SQLite 数据库
- `C:\Users\张泽旭\cloudflared.exe` — Cloudflare 隧道 (手机外网访问)
- `C:\Users\张泽旭\cs_monitor\` — 云端版 (Railway 部署)

## 关键配置
- API Token: `XGCWH1F7Y8U3P8X7L3F7G6X3` (csqaq.com)
- QQ邮箱: `1870462637@qq.com` (授权码已配置)
- 代理: FlClash `127.0.0.1:7890`
- 本地地址: `http://127.0.0.1:5000`
- Railway: `cs-monitor-production-9118.up.railway.app`
- 隧道地址: 每次重启变动, 运行 `D:\python\python.exe C:\Users\张泽旭\get_tunnel_url.py`

## 启动方式
- 开机自启: `C:\Users\张泽旭\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\CS2监控自启.bat`
- 桌面快捷: `C:\Users\张泽旭\Desktop\CS2行情监控.bat`
- 手动: `D:\python\python.exe C:\Users\张泽旭\cs_monitor_backend.py`

## 数据库表
- `market_snapshots` — 指数历史 (name_key, market_index, captured_at)
- `alerts` — 异动提醒
- `watchlist` — 自选指数
- `email_config` — QQ邮箱配置
- `skin_monitor` — 皮肤价格监控

## 待完成
- [ ] 自动拉取热门/涨跌榜饰品加入监控
- [ ] 完善涨幅榜/跌幅榜展示
- [ ] 皮肤监控列表前端展示优化
- [ ] 手机推送 (微信/其他)
- [ ] 云端版同步更新
