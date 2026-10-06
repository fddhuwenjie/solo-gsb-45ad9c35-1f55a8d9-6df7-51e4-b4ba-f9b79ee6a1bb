"""本地 PDF 遮盖工作台 —— 后端。

设计要点：
- 上传的原始 PDF 仅保存在内存中，且只有在通过全部校验后才会替换当前会话；
  无效 / 加密文件不会冲掉正在编辑的文档。
- 预览与导出都以“重新渲染整页位图”的方式生成：先把页面光栅化，
  再在像素层把遮盖框填成纯黑，因此框内内容无法从副本中恢复。
- 导出的新 PDF 每页只有一张位图，不复制原文档的文字层、批注、附件或元数据。
"""

from __future__ import annotations

import io
import math
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

import fitz  # PyMuPDF
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field, field_validator

MAX_PAGES = 8
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
PREVIEW_BASE_SCALE = 2.0  # zoom=1 时 144 DPI（72 * 2）
EXPORT_DPI = 200
EXPORT_SCALE = EXPORT_DPI / 72.0
MIN_BOX_SIZE = 0.5  # 点，小于该尺寸的退化矩形直接丢弃

app = FastAPI(title="本地 PDF 遮盖工作台", version="1.0.0")


class PDFError(ValueError):
    """向用户明确展示的 PDF 错误。"""


@dataclass
class PageState:
    width: float
    height: float
    boxes: list[list[float]] = field(default_factory=list)


@dataclass
class Session:
    session_id: str
    pdf_bytes: bytes
    pages: list[PageState]


_state_lock = threading.Lock()
_session: Session | None = None


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate_pdf(data: bytes) -> list[tuple[float, float]]:
    """校验 PDF，返回每页可见区域（page.rect，已含旋转 / 裁切）的宽高。

    任何异常都转换为 PDFError；调用方保证不会替换当前会话。
    """
    if not data.startswith(b"%PDF-"):
        raise PDFError("不是有效的 PDF 文件（缺少 %PDF- 文件头）。")

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF 对损坏文件会抛各种异常
        raise PDFError(f"无法打开 PDF，文件可能已损坏：{exc}") from exc

    try:
        if doc.needs_pass:
            raise PDFError("加密的 PDF 不受支持，请先在其他工具中移除密码后再上传。")
        if doc.page_count == 0:
            raise PDFError("该 PDF 不包含任何页面。")
        if doc.page_count > MAX_PAGES:
            raise PDFError(f"最多支持 {MAX_PAGES} 页，当前文件有 {doc.page_count} 页。")

        sizes: list[tuple[float, float]] = []
        for i in range(doc.page_count):
            try:
                page = doc.load_page(i)
                rect = page.rect
                if rect.width <= 0 or rect.height <= 0:
                    raise PDFError(f"第 {i + 1} 页尺寸异常，无法处理。")
                # 强制光栅化一次，尽早暴露损坏的页面内容流。
                pix = page.get_pixmap(matrix=fitz.Matrix(0.1, 0.1), alpha=False)
                if pix.width <= 0 or pix.height <= 0:
                    raise PDFError(f"第 {i + 1} 页无法渲染。")
            except PDFError:
                raise
            except Exception as exc:
                raise PDFError(f"第 {i + 1} 页内容损坏，无法渲染：{exc}") from exc
            sizes.append((rect.width, rect.height))
        return sizes
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_page_png(
    pdf_bytes: bytes,
    page_index: int,
    scale: float,
    boxes: list[list[float]],
) -> bytes:
    """把整页渲染为 PNG，并在像素层用纯黑覆盖遮盖框。"""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        draw = ImageDraw.Draw(img)
        for x0, y0, x1, y1 in boxes:
            draw.rectangle(
                [x0 * scale, y0 * scale, x1 * scale, y1 * scale],
                fill=(0, 0, 0),
            )
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()
    finally:
        doc.close()


def normalize_boxes(
    raw: list[list[float]], width: float, height: float
) -> list[list[float]]:
    """把前端传来的坐标规范化并裁切到页面可见区域内。"""
    result: list[list[float]] = []
    for box in raw:
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise HTTPException(status_code=400, detail="遮盖框坐标必须是 4 个数字。")
        try:
            x0, y0, x1, y1 = (float(v) for v in box)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="遮盖框坐标必须是数字。") from exc
        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
            raise HTTPException(status_code=400, detail="遮盖框坐标不能是无穷大或 NaN。")

        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0 = max(0.0, min(x0, width))
        x1 = max(0.0, min(x1, width))
        y0 = max(0.0, min(y0, height))
        y1 = max(0.0, min(y1, height))
        if x1 - x0 < MIN_BOX_SIZE or y1 - y0 < MIN_BOX_SIZE:
            continue  # 丢弃落在页面外或退化的框
        result.append([round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)])
    return result


def require_session() -> Session:
    if _session is None:
        raise HTTPException(status_code=404, detail="尚未上传 PDF。")
    return _session


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class BoxesPayload(BaseModel):
    boxes: list[list[float]] = Field(default_factory=list)

    @field_validator("boxes")
    @classmethod
    def _shape(cls, v: Any) -> Any:
        for box in v:
            if not isinstance(box, list) or len(box) != 4:
                raise ValueError("每个遮盖框必须包含 4 个数字。")
        return v


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/upload")
def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传内容为空。")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大，上限为 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB。",
        )
    try:
        sizes = validate_pdf(data)
    except PDFError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    pages = [PageState(width=w, height=h) for w, h in sizes]
    new_session = Session(session_id=uuid.uuid4().hex, pdf_bytes=data, pages=pages)
    # 只有校验全部通过后才替换会话，正在编辑的文档不受影响。
    with _state_lock:
        global _session
        _session = new_session
    return document_info(new_session)


@app.get("/api/document")
def get_document() -> dict[str, Any]:
    return document_info(require_session())


def document_info(session: Session) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "page_count": len(session.pages),
        "pages": [
            {"index": i, "width": p.width, "height": p.height, "boxes": p.boxes}
            for i, p in enumerate(session.pages)
        ],
    }


@app.get("/api/page/{page_index}")
def get_page(page_index: int, zoom: float = 1.0) -> StreamingResponse:
    if not 0.25 <= zoom <= 4.0:
        raise HTTPException(status_code=400, detail="zoom 必须在 0.25 到 4 之间。")
    with _state_lock:
        session = require_session()
        if not 0 <= page_index < len(session.pages):
            raise HTTPException(status_code=404, detail="页码不存在。")
        pdf_bytes = session.pdf_bytes
        boxes = [b[:] for b in session.pages[page_index].boxes]

    scale = PREVIEW_BASE_SCALE * zoom
    png = render_page_png(pdf_bytes, page_index, scale, boxes)
    return StreamingResponse(
        io.BytesIO(png),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@app.put("/api/page/{page_index}/boxes")
def update_boxes(page_index: int, payload: BoxesPayload) -> dict[str, Any]:
    with _state_lock:
        session = require_session()
        if not 0 <= page_index < len(session.pages):
            raise HTTPException(status_code=404, detail="页码不存在。")
        page = session.pages[page_index]
        page.boxes = normalize_boxes(payload.boxes, page.width, page.height)
        return {"index": page_index, "boxes": page.boxes}


@app.post("/api/export")
def export_pdf() -> StreamingResponse:
    with _state_lock:
        session = require_session()
        pdf_bytes = session.pdf_bytes
        page_specs = [
            (p.width, p.height, [b[:] for b in p.boxes]) for p in session.pages
        ]

    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    try:
        for i, (width, height, boxes) in enumerate(page_specs):
            src_page = src.load_page(i)
            pix = src_page.get_pixmap(
                matrix=fitz.Matrix(EXPORT_SCALE, EXPORT_SCALE), alpha=False
            )
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            draw = ImageDraw.Draw(img)
            for x0, y0, x1, y1 in boxes:
                draw.rectangle(
                    [
                        x0 * EXPORT_SCALE,
                        y0 * EXPORT_SCALE,
                        x1 * EXPORT_SCALE,
                        y1 * EXPORT_SCALE,
                    ],
                    fill=(0, 0, 0),
                )
            png_buf = io.BytesIO()
            img.save(png_buf, format="PNG", optimize=False)

            # 新页面使用原页可见区域（含旋转后的方向、含裁切后的尺寸）。
            page_rect = fitz.Rect(0, 0, width, height)
            dst_page = out.new_page(width=width, height=height)
            dst_page.insert_image(page_rect, stream=png_buf.getvalue(), keep_proportion=False)

        # 不复制任何元数据：写入空字段并让 PyMuPDF 回收全部残余对象。
        out.set_metadata(
            {
                "format": "",
                "title": "",
                "author": "",
                "subject": "",
                "keywords": "",
                "creator": "",
                "producer": "",
                "creationDate": "",
                "modDate": "",
                "trapped": "",
                "encryption": "",
            }
        )
        pdf_bytes_out = out.tobytes(garbage=4, deflate=True, clean=True)
    finally:
        out.close()
        src.close()

    headers = {"Content-Disposition": 'attachment; filename="redacted.pdf"'}
    return StreamingResponse(
        io.BytesIO(pdf_bytes_out),
        media_type="application/pdf",
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# 静态前端（必须挂在 API 路由之后）
# --------------------------------------------------------------------------- #
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
