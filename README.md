# Krill Playwright Image API

用本机固定 Chrome 窗口调用 Krill 在线绘图页，把页面里的文生图/图生图封装成一个本地 HTTP API。它不需要 Krill API Key，复用浏览器登录态，适合个人工作流、脚本批量出图或接入自己的工具链。

> 注意：这是浏览器自动化方案，Krill 页面结构改版时可能需要更新按钮/输入框选择器。请遵守 Krill 的账号规则和使用条款。

## 功能

- 固定打开本机 Chrome 窗口，默认位置 `80,80`，尺寸 `1440x1000`。
- 登录态保存在 `%LOCALAPPDATA%\KrillPlaywright\chrome-profile`，不写入仓库。
- 提供 `POST /generate`，支持文生图和上传参考图的图生图。
- 串行执行任务，避免同一个网页登录态被并发操作打乱。
- 自动轮询 Krill 任务，下载结果到 `outputs/`。

## 环境准备

```powershell
cd D:\app\cauhub\krill-playwright-api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chrome
```

如果你想用 Playwright 自带 Chromium，而不是系统 Chrome：

```powershell
$env:KRILL_BROWSER_CHANNEL=""
playwright install chromium
```

## 首次登录

```powershell
python app.py login
```

脚本会打开一个固定窗口。请在窗口里完成 Krill 登录，看到绘图页可用后终端会提示登录成功。后续启动会复用同一个浏览器 profile。

## 启动服务

```powershell
python app.py serve --host 127.0.0.1 --port 8791
```

健康检查：

```powershell
curl.exe http://127.0.0.1:8791/health
```

文生图：

```powershell
curl.exe -X POST http://127.0.0.1:8791/generate `
  -F "prompt=一只戴宇航员头盔的橘猫，电影感灯光"
```

图生图：

```powershell
curl.exe -X POST http://127.0.0.1:8791/generate `
  -F "prompt=改成水彩插画风格" `
  -F "image=@D:\images\reference.png"
```

返回值包含 Krill 任务 ID、结果 URL 与下载后的本地文件路径：

```json
{
  "task_id": "123456",
  "status": "completed",
  "image_urls": ["https://..."],
  "files": ["D:\\app\\cauhub\\krill-playwright-api\\outputs\\...\\image-1.png"]
}
```

## 可配置项

可以通过环境变量覆盖默认值，也可以复制 `.env.example` 自己记录常用配置。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KRILL_BROWSER_CHANNEL` | `chrome` | Playwright 浏览器通道；设为空可用内置 Chromium |
| `KRILL_WINDOW_WIDTH` | `1440` | 浏览器窗口宽度 |
| `KRILL_WINDOW_HEIGHT` | `1000` | 浏览器窗口高度 |
| `KRILL_WINDOW_X` | `80` | 浏览器窗口左上角 X 坐标 |
| `KRILL_WINDOW_Y` | `80` | 浏览器窗口左上角 Y 坐标 |
| `KRILL_OUTPUT_DIR` | `outputs` | 生成图片保存目录 |

## CauHub 教程

可直接发布的中文教程在 [docs/cauhub-tutorial.md](docs/cauhub-tutorial.md)。仓库根 README 面向 GitHub，教程面向读者一步步复现。

## GitHub 发布建议

这个目录已经按单仓库整理，建议只发布 `krill-playwright-api`，不要把浏览器 profile、`outputs/`、账号文件或云端多账号迁移目录一起提交。

```powershell
git init
git add app.py README.md requirements.txt .gitignore .env.example docs/cauhub-tutorial.md
git commit -m "Add Krill Playwright image API tutorial"
```

本机没有安装 GitHub CLI 时，可以在 GitHub 网页新建空仓库后执行：

```powershell
git remote add origin https://github.com/<your-name>/krill-playwright-image-api.git
git branch -M main
git push -u origin main
```
