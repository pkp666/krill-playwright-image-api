from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from playwright.async_api import BrowserContext, Page, Playwright, async_playwright


DRAW_URL = "https://www.krill-ai.com/app/draw"
PROFILE_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "KrillPlaywright" / "chrome-profile"
OUTPUT_DIR = Path(os.getenv("KRILL_OUTPUT_DIR", Path(__file__).parent / "outputs"))
BROWSER_CHANNEL = os.getenv("KRILL_BROWSER_CHANNEL", "chrome").strip() or None
WINDOW_WIDTH = int(os.getenv("KRILL_WINDOW_WIDTH", "1440"))
WINDOW_HEIGHT = int(os.getenv("KRILL_WINDOW_HEIGHT", "1000"))
WINDOW_X = int(os.getenv("KRILL_WINDOW_X", "80"))
WINDOW_Y = int(os.getenv("KRILL_WINDOW_Y", "80"))


def walk(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def task_id(value: Any) -> str | None:
    for key, item in walk(value):
        if key in {"task_id", "taskId"} and isinstance(item, (str, int)):
            return str(item)
    for key, item in walk(value):
        if key == "id" and isinstance(item, (str, int)):
            return str(item)
    return None


def status(value: Any) -> str:
    for key, item in walk(value):
        if key == "status" and isinstance(item, str):
            return item.lower()
    return ""


def image_urls(value: Any) -> list[str]:
    found: list[str] = []
    for key, item in walk(value):
        if not isinstance(item, str):
            continue
        if key.lower() in {"url", "image", "image_url", "output_url", "result_url"}:
            if item.startswith(("http://", "https://", "data:image/", "blob:")):
                found.append(item)
    return list(dict.fromkeys(found))


class KrillBrowser:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        if self.context:
            return
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self.playwright = await async_playwright().start()
        launch_options = {
            "headless": False,
            "viewport": {"width": WINDOW_WIDTH, "height": WINDOW_HEIGHT},
            "args": [
                "--no-first-run",
                "--no-default-browser-check",
                f"--window-position={WINDOW_X},{WINDOW_Y}",
                f"--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}",
            ],
        }
        if BROWSER_CHANNEL:
            launch_options["channel"] = BROWSER_CHANNEL
        self.context = await self.playwright.chromium.launch_persistent_context(str(PROFILE_DIR), **launch_options)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

    async def stop(self) -> None:
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.context = self.page = self.playwright = None

    async def open_draw(self) -> Page:
        await self.start()
        assert self.page
        if "/app/draw" not in self.page.url:
            await self.page.goto(DRAW_URL, wait_until="domcontentloaded")
        if "/login" in self.page.url:
            raise HTTPException(401, "Krill 未登录，请先运行: python app.py login")
        await self.page.locator("textarea:visible").last.wait_for(timeout=30_000)
        return self.page

    @staticmethod
    def _button(page: Page, text: str):
        return page.locator("button:visible").filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))

    async def _fill_prompt(self, page: Page, prompt: str) -> None:
        textarea = page.locator("textarea:visible").last
        if await textarea.count():
            await textarea.fill(prompt)
            return
        editable = page.locator('[contenteditable="true"]:visible').last
        if await editable.count():
            await editable.fill(prompt)
            return
        raise RuntimeError("未找到提示词输入框，页面结构可能已更新")

    async def _select_mode(self, page: Page, edit: bool, image_path: Path | None) -> None:
        label = "图生图" if edit else "文生图"
        await self._button(page, label).first.click()
        if not edit:
            return
        upload = page.locator('input[type="file"][accept*="image"]')
        if not await upload.count():
            upload = page.locator('input[type="file"]')
        if not await upload.count() or image_path is None:
            raise RuntimeError("未找到参考图上传控件")
        await upload.last.set_input_files(str(image_path))

    async def _poll(self, page: Page, tid: str, timeout: int) -> Any:
        deadline = time.monotonic() + timeout
        latest: Any = {}
        while time.monotonic() < deadline:
            latest = await page.evaluate(
                """async id => {
                    const token = localStorage.getItem('krill_jwt');
                    const r = await fetch(`/api/draw/v1/tasks/${encodeURIComponent(id)}`, {
                        headers: token ? {Authorization: `Bearer ${token}`} : {}
                    });
                    const text = await r.text();
                    let body; try { body = JSON.parse(text) } catch { body = {message: text} }
                    if (!r.ok) throw new Error(`task status ${r.status}: ${text.slice(0, 200)}`);
                    return body;
                }""",
                tid,
            )
            state = status(latest)
            if state in {"completed", "success", "succeeded"}:
                return latest
            if state in {"failed", "error", "cancelled", "canceled"}:
                raise RuntimeError(f"Krill 生图失败: {json.dumps(latest, ensure_ascii=False)[:500]}")
            await asyncio.sleep(2)
        raise TimeoutError(f"等待 Krill 任务 {tid} 超时")

    async def _download(self, page: Page, urls: list[str], job_dir: Path) -> list[str]:
        assert self.context
        files: list[str] = []
        for index, url in enumerate(urls, 1):
            suffix = ".png"
            if url.startswith("data:image/"):
                header, encoded = url.split(",", 1)
                kind = re.search(r"data:image/([a-zA-Z0-9.+-]+)", header)
                suffix = ".jpg" if kind and kind.group(1) == "jpeg" else f".{kind.group(1) if kind else 'png'}"
                data = base64.b64decode(encoded)
            elif url.startswith("blob:"):
                encoded = await page.evaluate(
                    """async url => {
                        const b = await (await fetch(url)).blob();
                        return await new Promise((ok, bad) => {
                            const r = new FileReader(); r.onload = () => ok(r.result.split(',')[1]);
                            r.onerror = bad; r.readAsDataURL(b);
                        });
                    }""",
                    url,
                )
                data = base64.b64decode(encoded)
            else:
                response = await self.context.request.get(url, timeout=120_000)
                if not response.ok:
                    continue
                content_type = response.headers.get("content-type", "")
                if "jpeg" in content_type:
                    suffix = ".jpg"
                elif "webp" in content_type:
                    suffix = ".webp"
                data = await response.body()
            target = job_dir / f"image-{index}{suffix}"
            target.write_bytes(data)
            files.append(str(target.resolve()))
        return files

    async def generate(self, prompt: str, image_path: Path | None, timeout: int) -> dict[str, Any]:
        async with self.lock:
            page = await self.open_draw()
            before = set(await page.locator("img").evaluate_all("els => els.map(x => x.currentSrc || x.src)"))
            await self._select_mode(page, image_path is not None, image_path)
            await self._fill_prompt(page, prompt)
            submit_label = "编辑" if image_path else "生成"

            async with page.expect_response(
                lambda r: "/api/draw/v1/image/" in r.url and r.request.method == "POST",
                timeout=30_000,
            ) as response_info:
                await self._button(page, submit_label).last.click()
            response = await response_info.value
            try:
                submitted = await response.json()
            except Exception:
                submitted = {"message": (await response.text())[:500]}
            if not response.ok:
                raise RuntimeError(f"Krill 提交失败 ({response.status}): {submitted}")

            tid = task_id(submitted)
            completed = await self._poll(page, tid, timeout) if tid else submitted
            urls = image_urls(completed) or image_urls(submitted)
            if not urls:
                await page.wait_for_timeout(1500)
                after = await page.locator("img").evaluate_all("els => els.map(x => x.currentSrc || x.src)")
                urls = [url for url in after if url and url not in before and not url.startswith("data:image/svg")]

            job_dir = OUTPUT_DIR / uuid.uuid4().hex
            job_dir.mkdir(parents=True)
            files = await self._download(page, urls, job_dir)
            return {"task_id": tid, "status": status(completed) or "submitted", "image_urls": urls, "files": files}


browser = KrillBrowser()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await browser.start()
    yield
    await browser.stop()


app = FastAPI(title="Krill Playwright Image API", lifespan=lifespan)


@app.get("/health")
async def health():
    await browser.start()
    return {"ok": True, "page": browser.page.url if browser.page else None, "busy": browser.lock.locked()}


@app.post("/generate")
async def generate(
    prompt: str = Form(..., min_length=1),
    image: UploadFile | None = File(None),
    timeout: int = Form(600, ge=30, le=1800),
):
    temp: Path | None = None
    try:
        if image:
            suffix = Path(image.filename or "reference.png").suffix or ".png"
            temp_dir = OUTPUT_DIR / ".uploads"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp = temp_dir / f"{uuid.uuid4().hex}{suffix}"
            temp.write_bytes(await image.read())
        return await browser.generate(prompt.strip(), temp, timeout)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        if temp:
            temp.unlink(missing_ok=True)


async def login() -> None:
    await browser.start()
    assert browser.page
    await browser.page.goto(DRAW_URL, wait_until="domcontentloaded")
    browser_name = BROWSER_CHANNEL or "chromium"
    print(f"请在打开的 {browser_name} 窗口完成 Krill 登录；检测到在线绘图页后会自动保存登录态。")
    try:
        await browser._button(browser.page, "生成").wait_for(timeout=600_000)
        print("登录成功。")
    finally:
        await browser.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "login"], nargs="?", default="serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    if args.command == "login":
        asyncio.run(login())
    else:
        uvicorn.run(app, host=args.host, port=args.port)
