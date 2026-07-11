# 用固定 Chrome 窗口把 Krill 生图封装成本地 API

这篇教程记录一个轻量做法：不申请 Krill API Key，而是复用已经登录的 Chrome 窗口，通过 Playwright 自动操作 Krill 在线绘图页，再用 FastAPI 暴露一个本地接口。

适合场景：

- 想把 Krill 生图接入自己的脚本、工作流或本地工具。
- 希望保留可见浏览器窗口，方便登录、排错和人工接管。
- 只需要单账号串行生图，不追求云端多账号调度。

## 实现思路

核心链路很短：

1. Playwright 启动一个固定位置、固定尺寸的 Chrome 持久化窗口。
2. 用户第一次手动登录 Krill，登录态保存到本机 profile。
3. FastAPI 提供 `/generate` 接口接收提示词和可选参考图。
4. Playwright 在 Krill 页面选择“文生图”或“图生图”，填写提示词并点击生成。
5. 拿到任务 ID 后轮询 Krill 页面接口，完成后下载图片到本地。

这个方案的关键不是破解接口，而是把“你已经能在浏览器里完成的操作”稳定地自动化。

## 项目结构

```text
krill-playwright-api/
  app.py                 # FastAPI + Playwright 主程序
  requirements.txt       # Python 依赖
  .env.example           # 常用环境变量示例
  README.md              # GitHub 仓库说明
  docs/
    cauhub-tutorial.md   # 本教程
```

生成结果默认写到 `outputs/`，该目录已被 `.gitignore` 排除。

## 安装依赖

Windows PowerShell 示例：

```powershell
cd D:\app\cauhub\krill-playwright-api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chrome
```

如果机器上不想使用系统 Chrome，可以改用 Playwright 内置 Chromium：

```powershell
$env:KRILL_BROWSER_CHANNEL=""
playwright install chromium
```

## 第一次登录 Krill

运行登录命令：

```powershell
python app.py login
```

程序会打开一个 Chrome 窗口，默认窗口位置为 `80,80`，大小为 `1440x1000`。在窗口里完成 Krill 登录，进入绘图页后终端会提示登录成功。

登录态保存在：

```text
%LOCALAPPDATA%\KrillPlaywright\chrome-profile
```

这个目录不在项目里，所以不会被误提交到 GitHub。

## 启动本地 API

```powershell
python app.py serve --host 127.0.0.1 --port 8791
```

检查服务是否可用：

```powershell
curl.exe http://127.0.0.1:8791/health
```

正常会返回类似：

```json
{
  "ok": true,
  "page": "https://www.krill-ai.com/app/draw",
  "busy": false
}
```

## 调用文生图

```powershell
curl.exe -X POST http://127.0.0.1:8791/generate `
  -F "prompt=一只戴宇航员头盔的橘猫，电影感灯光"
```

## 调用图生图

```powershell
curl.exe -X POST http://127.0.0.1:8791/generate `
  -F "prompt=改成水彩插画风格，保留主体轮廓" `
  -F "image=@D:\images\reference.png"
```

接口会自动把上传的图片临时保存，设置到 Krill 的文件上传控件里，任务完成后删除临时文件。

## 返回结果

成功后会返回任务 ID、结果 URL 和本地保存路径：

```json
{
  "task_id": "123456",
  "status": "completed",
  "image_urls": ["https://..."],
  "files": ["D:\\app\\cauhub\\krill-playwright-api\\outputs\\...\\image-1.png"]
}
```

如果 Krill 返回的是 `blob:` 或 `data:image/`，程序也会转成真实图片文件写入本地。

## 固定窗口配置

默认配置已经适合本地桌面使用，也可以用环境变量调整：

```powershell
$env:KRILL_WINDOW_X="120"
$env:KRILL_WINDOW_Y="80"
$env:KRILL_WINDOW_WIDTH="1500"
$env:KRILL_WINDOW_HEIGHT="900"
$env:KRILL_OUTPUT_DIR="D:\krill-outputs"
python app.py serve --host 127.0.0.1 --port 8791
```

常用变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `KRILL_BROWSER_CHANNEL` | `chrome` | 使用系统 Chrome；设为空可用 Playwright Chromium |
| `KRILL_WINDOW_WIDTH` | `1440` | 窗口宽度 |
| `KRILL_WINDOW_HEIGHT` | `1000` | 窗口高度 |
| `KRILL_WINDOW_X` | `80` | 窗口横向位置 |
| `KRILL_WINDOW_Y` | `80` | 窗口纵向位置 |
| `KRILL_OUTPUT_DIR` | `outputs` | 图片输出目录 |

## 关键代码

固定 Chrome 窗口和持久化登录态在 `KrillBrowser.start()` 里完成：

```python
self.context = await self.playwright.chromium.launch_persistent_context(
    str(PROFILE_DIR),
    headless=False,
    channel="chrome",
    viewport={"width": WINDOW_WIDTH, "height": WINDOW_HEIGHT},
    args=[
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-position={WINDOW_X},{WINDOW_Y}",
        f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
    ],
)
```

生成任务用一个 `asyncio.Lock()` 串行化，避免同一个页面被多个请求同时点击：

```python
async with self.lock:
    page = await self.open_draw()
    await self._select_mode(page, image_path is not None, image_path)
    await self._fill_prompt(page, prompt)
```

提交后监听 Krill 的创建任务请求，再轮询任务状态：

```python
async with page.expect_response(
    lambda r: "/api/draw/v1/image/" in r.url and r.request.method == "POST",
    timeout=30_000,
) as response_info:
    await self._button(page, submit_label).last.click()
```

## 常见问题

`playwright install chrome` 失败：

可以先确认本机已经安装 Chrome，然后尝试把 `KRILL_BROWSER_CHANNEL` 设为空，改用内置 Chromium。

`401 Krill 未登录`：

重新运行 `python app.py login`，确认固定窗口里已经进入 Krill 绘图页。

找不到按钮或输入框：

Krill 页面可能改版了。优先检查 `app.py` 里“文生图”“图生图”“生成”“编辑”这些按钮文案是否还一致。

并发请求排队：

这是单窗口方案的正常行为。如果要做多账号并发，可以把同样思路扩展成多个 Chrome profile + 多个远程调试端口。

## 发布到 GitHub

只提交这个目录里的源码和文档：

```powershell
git init
git add app.py README.md requirements.txt .gitignore .env.example docs/cauhub-tutorial.md
git commit -m "Add Krill Playwright image API tutorial"
```

不要提交这些内容：

- `outputs/`
- `.venv/`
- Chrome profile 或 cookie
- 账号配置、日志、生成图、临时上传图

如果没有 GitHub CLI，可以在 GitHub 网页新建空仓库，再绑定远端：

```powershell
git remote add origin https://github.com/<your-name>/krill-playwright-image-api.git
git branch -M main
git push -u origin main
```

## 小结

这个项目把“打开 Krill 页面手动生图”变成了一个可脚本化的本地接口。它的优点是简单、透明、容易排错；限制是依赖前端页面结构，适合个人和小规模自动化，不适合当作高并发后端服务直接暴露到公网。
