"""端到端冒烟测试：构造各种 PDF，直接用 FastAPI TestClient 验证全部关键需求。"""
import io
import sys

sys.path.insert(0, "/tmp/pylibs")
sys.path.insert(0, "/workspace")

import fitz  # noqa: E402
from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main as backend  # noqa: E402

TMP = "/tmp/pdfredact_test"
import os  # noqa: E402
os.makedirs(TMP, exist_ok=True)

SECRET = "TOP-SECRET-WORD-12345"
ANNOT_TEXT = "annotation-marker-xyz"


def make_pdf(path, rotated=False, cropped=False, with_annot=False):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 竖
    page.insert_text((72, 100), SECRET, fontsize=24)
    page.insert_text((72, 300), "VISIBLE-NORMAL-TEXT", fontsize=18)
    if with_annot:
        page.add_text_annot((72, 400), ANNOT_TEXT)
    if rotated:
        page.set_rotation(90)
    if cropped:
        page.set_cropbox(fitz.Rect(50, 60, 500, 700))
    # 附件
    if with_annot:
        doc.embfile_add("secret.txt", b"EMBEDDED-FILE-BYTES", filename="secret.txt")
    doc.set_metadata({"title": "Secret Title", "author": "Secret Author", "producer": "X"})
    page2 = doc.new_page(width=400, height=600)
    page2.insert_text((50, 100), "PAGE-TWO-CONTENT", fontsize=18)
    doc.save(path)
    doc.close()


def main():
    good = f"{TMP}/good.pdf"
    rot = f"{TMP}/rot.pdf"
    make_pdf(good, with_annot=True)
    make_pdf(rot, rotated=True, cropped=True)

    # 加密 PDF
    enc = fitz.open(good)
    enc.save(f"{TMP}/enc.pdf", encryption=fitz.PDF_ENCRYPT_AES_256,
             owner_pw="o", user_pw="p")
    enc.close()

    # 9 页超限
    big = fitz.open()
    for _ in range(9):
        big.new_page()
    big.save(f"{TMP}/big.pdf")
    big.close()

    with open(f"{TMP}/notpdf.txt", "wb") as f:
        f.write(b"hello world, not a pdf")

    client = TestClient(backend.app)

    # 1) 无效上传：非 PDF
    r = client.post("/api/upload", files={"file": ("a.pdf", b"not pdf", "application/pdf")})
    assert r.status_code == 400 and "PDF" in r.json()["detail"], r.text

    # 2) 加密
    with open(f"{TMP}/enc.pdf", "rb") as f:
        r = client.post("/api/upload", files={"file": ("enc.pdf", f.read(), "application/pdf")})
    assert r.status_code == 400 and "加密" in r.json()["detail"], r.text

    # 3) 超页数
    with open(f"{TMP}/big.pdf", "rb") as f:
        r = client.post("/api/upload", files={"file": ("big.pdf", f.read(), "application/pdf")})
    assert r.status_code == 400 and "8 页" in r.json()["detail"], r.text

    # 4) 假文件头
    r = client.post("/api/upload", files={"file": ("a.pdf", b"%PDF-1.4 broken", "application/pdf")})
    assert r.status_code == 400, r.text

    # 5) 正常上传
    with open(good, "rb") as f:
        good_bytes = f.read()
    r = client.post("/api/upload", files={"file": ("good.pdf", good_bytes, "application/pdf")})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["page_count"] == 2
    pw, ph = info["pages"][0]["width"], info["pages"][0]["height"]
    assert (pw, ph) == (595.0, 842.0), (pw, ph)

    # 6) 无效上传不能冲掉现有会话
    r = client.post("/api/upload", files={"file": ("bad.pdf", b"%PDF-bad", "application/pdf")})
    assert r.status_code == 400
    r = client.get("/api/document")
    assert r.status_code == 200 and r.json()["page_count"] == 2

    # 7) 画框：盖住 SECRET 所在区域（坐标约 72,80 - 420,115）
    box = [70, 78, 430, 118]
    r = client.put("/api/page/0/boxes", json={"boxes": [box]})
    assert r.status_code == 200 and r.json()["boxes"] == [[70.0, 78.0, 430.0, 118.0]], r.text

    # 8) 预览 PNG：框内像素纯黑，框外有白色/其它像素
    r = client.get("/api/page/0?zoom=1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    SCALE = 2.0
    px = img.load()
    cx = int((box[0] + box[2]) / 2 * SCALE)
    cy = int((box[1] + box[3]) / 2 * SCALE)
    assert px[cx, cy] == (0, 0, 0), px[cx, cy]
    # 框外区域应非全黑（白纸）
    assert px[int(72 * SCALE), int(300 * SCALE)] != (0, 0, 0)

    # 9) 导出
    r = client.post("/api/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    out_bytes = r.content
    with open(f"{TMP}/out.pdf", "wb") as f:
        f.write(out_bytes)

    out = fitz.open("pdf", out_bytes)
    assert out.page_count == 2, out.page_count
    p0 = out[0]
    assert abs(p0.rect.width - 595) < 0.5 and abs(p0.rect.height - 842) < 0.5, p0.rect
    # 无文字、无批注、无附件、无链接
    assert p0.get_text().strip() == "", repr(p0.get_text())
    assert not list(p0.annots())
    assert p0.first_link is None
    assert out.embfile_count() == 0
    # 元数据为空（'format' 是输出 PDF 的格式版本号，非文档元数据）
    for k, v in out.metadata.items():
        if k == "format":
            continue
        assert v in ("", None), (k, v)
    # 每一页只有图片 XObject，没有矢量图形/文字指令残留（图片型页面）
    imgs = p0.get_images(full=True)
    assert len(imgs) == 1, imgs
    # 整页渲染后，框内仍然黑（确认副本自身的像素不可恢复）
    pix = p0.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    oimg = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    assert oimg.load()[cx, cy] == (0, 0, 0)
    # 输出流里不能出现原文敏感串（文字层/元数据/附件均不应残留）
    assert SECRET.encode() not in out_bytes
    assert ANNOT_TEXT.encode() not in out_bytes
    assert b"EMBEDDED-FILE-BYTES" not in out_bytes
    assert b"Secret Title" not in out_bytes
    # 第二页没画框，但必须在副本里
    assert out[1].rect.width == 400 and out[1].rect.height == 600
    assert len(out[1].get_images(full=True)) == 1
    out.close()

    # 10) 旋转 + 裁切页
    with open(rot, "rb") as f:
        r = client.post("/api/upload", files={"file": ("rot.pdf", f.read(), "application/pdf")})
    assert r.status_code == 200, r.text
    info = r.json()
    rw, rh = info["pages"][0]["width"], info["pages"][0]["height"]
    # 90 度旋转 + cropbox 后的可见矩形
    assert abs(rw - 640) < 1 and abs(rh - 450) < 1, (rw, rh)
    # 在可见页面中央画框，预览必须是黑色
    r = client.put("/api/page/0/boxes", json={"boxes": [[rw/2 - 40, rh/2 - 20, rw/2 + 40, rh/2 + 20]]})
    r = client.get("/api/page/0?zoom=1")
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    px = img.load()
    assert px[img.width // 2, img.height // 2] == (0, 0, 0)
    r = client.post("/api/export")
    out = fitz.open("pdf", r.content)
    assert out.page_count == 2
    w0, h0 = out[0].rect.width, out[0].rect.height
    assert (abs(w0 - rw) < 1 and abs(h0 - rh) < 1), (w0, h0, rw, rh)
    assert out[0].rotation in (90, 0)  # 方向体现在宽高上即可
    pix = out[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
    oimg = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    assert oimg.load()[pix.width // 2, pix.height // 2] == (0, 0, 0)
    assert out[0].get_text().strip() == ""
    out.close()

    # 11) 坐标规范化：越界、反向、退化框
    r = client.put("/api/page/0/boxes", json={"boxes": [[-50, -50, 99999, 99999], [10, 10, 5, 5], [100, 100, 101, 101]]})
    boxes = r.json()["boxes"]
    assert boxes[0] == [0.0, 0.0, round(rw, 3), round(rh, 3)], boxes
    # 反向框经排序后规范化为 [5,5,10,10]；另一个 1pt 小框也保留（阈值 0.5pt）
    assert boxes[1] == [5.0, 5.0, 10.0, 10.0], boxes
    assert len(boxes) == 3, boxes

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
