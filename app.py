# -*- coding: utf-8 -*-
"""
包子漫画（腕上漫画自定义漫画源）
================================
将包子漫画(baozimh 家族，主域名默认 dinnerku.com)包装成符合「腕上漫画 CUSTOM_SOURCE.md」
规范的 HTTP API 漫画源。

接口：
    /config                        -> 漫画源配置
    /album/<id>                    -> 漫画详情
    /search/<text>/<page>          -> 关键词搜索
    /photo/<id>/chapter/<chapter>  -> 章节图片列表
    /image/proxy                   -> 图片代理(缩放/质量/PNG/LVGL)

实现要点（均已真机验证）：
    1. 搜索、详情页在 cn.<domain> 上可直接抓取(纯 requests)；详情页的 302 跳转由
       allow_redirects 自动跟随，得到带哈希后缀的真实 comic_id。
    2. 章节阅读页在 appcn.baozimh.com 上，被 Cloudflare 的 TLS 指纹校验拦截，
       纯 requests/curl 会拿到 "Just a moment"；必须用 curl_cffi 的
       impersonate="chrome" 模拟浏览器指纹才能取到页面中的 data-src 图片地址。
    3. CDN 图片(s2.baozicdn.com 等)可直接用 requests 下载，代理只负责缩放与转码。
"""
import io
import os
import re
import time
import logging
import struct

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request
from PIL import Image
from curl_cffi import requests as cffi_requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("baozi-source")

# --------------------------------------------------------------------------
# 可配置项（环境变量），均可通过请求 /album、/search 时的同名参数临时覆盖
# --------------------------------------------------------------------------
DEFAULT_DOMAIN = os.environ.get("BAOZI_DOMAIN", "dinnerku.com")   # 主域名
DEFAULT_LANG   = os.environ.get("BAOZI_LANG", "cn")               # cn / tw 简繁
DEFAULT_CDN    = os.environ.get("BAOZI_CDN_DOMAIN", "")           # 图片站域名，空=跟随原图
DEFAULT_QUAL   = os.environ.get("BAOZI_IMAGE_QUALITY", "/w640")   # 图片质量路径，空=原图

# 章节阅读页(需 curl_cffi 模拟指纹)
APP_READ_URL = "https://appcn.baozimh.com/baozimhapp/comic/chapter/{comic}/0_{slot}.html"

# 浏览器 UA（抓详情/搜索/阅读页用；不要用 curl 默认 UA，会被拦）
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
# 抓 CDN 图片时的头
IMG_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Referer": "https://appcn.baozimh.com/",
}

# --------------------------------------------------------------------------
# 简单 TTL 缓存，避免反复抓取上游(阅读页较慢)
# --------------------------------------------------------------------------
_cache = {}
_CACHE_TTL = {"detail": 600, "chapter": 1800}


def _cache_get(key):
    ent = _cache.get(key)
    if not ent:
        return None
    val, ts, ttl = ent
    if time.time() - ts > ttl:
        _cache.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl):
    _cache[key] = (val, time.time(), ttl)


# --------------------------------------------------------------------------
# 上游抓取
# --------------------------------------------------------------------------
def _get_html(url, impersonate=False, timeout=25):
    """抓取上游 HTML。impersonate=True 时用 curl_cffi 模拟 Chrome 指纹(绕过 Cloudflare)。"""
    last_err = None
    for attempt in range(2):
        try:
            if impersonate:
                r = cffi_requests.get(
                    url, impersonate="chrome", headers=BROWSER_HEADERS, timeout=timeout
                )
            else:
                r = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue
            return r.text
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(0.6)
    raise RuntimeError(f"fetch failed {url} -> {last_err}")


def _get_html_robust(url, timeout=25):
    """先普通 requests，失败/被限流/超时后回退 curl_cffi 模拟指纹重试。
    用于搜索、详情页(站点偶发超时或反爬)。"""
    last_err = None
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.text, r.url
        last_err = f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        last_err = f"{type(e).__name__}: {str(e)[:120]}"
    # 回退 curl_cffi
    try:
        r = cffi_requests.get(
            url, impersonate="chrome", headers=BROWSER_HEADERS, timeout=timeout
        )
        if r.status_code == 200:
            return r.text, r.url
        last_err = f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        last_err = f"{type(e).__name__}: {str(e)[:120]}"
    raise RuntimeError(f"fetch failed {url} -> {last_err}")


def _truthy(v):
    return str(v).lower() in {"1", "true", "yes", "on", "y"}


# --------------------------------------------------------------------------
# 数据解析
# --------------------------------------------------------------------------
def _base_url(lang=None, domain=None):
    return f"https://{lang or DEFAULT_LANG}.{domain or DEFAULT_DOMAIN}"


def fetch_detail(comic_id, lang=None, domain=None):
    """抓详情页，跟随 302 得到真实 id，解析出漫画信息与章节列表。"""
    base = _base_url(lang, domain)
    key = ("detail", comic_id, base)
    cached = _cache_get(key)
    if cached:
        return cached

    # 详情页可能 302 到带哈希后缀的真实 id；用健壮抓取(失败回退 curl_cffi)
    html, final_url = _get_html_robust(f"{base}/comic/{comic_id}", timeout=25)
    soup = BeautifulSoup(html, "html.parser")

    # 真实 id：优先取跳转后的 URL 末段；若无跳转则保持原 id
    final_seg = final_url.rstrip("/").split("/")[-1]
    resolved_id = final_seg if re.match(r"^[^/]+$", final_seg) else comic_id
    if not resolved_id or "/" in resolved_id:
        resolved_id = comic_id

    def _txt(sel):
        node = soup.select_one(sel)
        return node.get_text(strip=True) if node else ""

    title = _txt("h1.comics-detail__title")
    author = _txt("h2.comics-detail__author")
    desc = _txt("p.comics-detail__desc")

    poster = soup.select_one(".comics-detail__poster")
    if not poster:
        poster = soup.select_one("div.l-content > div > div > amp-img")
    cover = poster.get("src") if poster else ""

    tags = [x.get_text(strip=True) for x in soup.select("div.tag-list > span")]
    tags = [t for t in tags if t]

    # 章节 slot 列表（阅读顺序：chapter-items 在前，chapters_other_list 续后）
    slots = []
    for container in soup.select("#chapter-items, #chapters_other_list"):
        for a in container.select("div.comics-chapters > a"):
            href = a.get("href") or ""
            m = re.search(r"chapter_slot=(\d+)", href)
            if m:
                slots.append(int(m.group(1)))
    total_chapters = len(slots) or 1

    # 若两类容器都没解析到，回退：所有 comics-chapters > a
    if not slots:
        for a in soup.select("div.comics-chapters > a"):
            m = re.search(r"chapter_slot=(\d+)", a.get("href") or "")
            if m:
                slots.append(int(m.group(1)))
        total_chapters = len(slots) or 1

    detail = {
        "resolved_id": resolved_id,
        "title": title,
        "author": author,
        "cover": cover,
        "desc": desc,
        "tags": tags,
        "slots": sorted(set(slots)),   # 阅读顺序按 slot 升序
        "total_chapters": total_chapters,
    }
    _cache_set(key, detail, _CACHE_TTL["detail"])
    return detail


def fetch_search(keyword, lang=None, domain=None):
    """搜索页返回漫画卡片(单页返回全部)。"""
    base = _base_url(lang, domain)
    url = f"{base}/search?q={keyword}"
    html = _get_html(url, impersonate=False)
    soup = BeautifulSoup(html, "html.parser")

    results = []
    for card in soup.select("div.comics-card"):
        a = card.find("a")
        if not a:
            continue
        href = a.get("href") or ""
        cid = href.split("/")[-1]
        if not cid:
            continue
        h3 = card.find("h3")
        title = h3.get_text(strip=True) if h3 else ""
        amp = card.find("amp-img")
        cover = (amp.get("src") if amp else "") or (amp.get("data-src") if amp else "")
        results.append({
            "comic_id": cid,
            "title": title,
            "cover_url": cover,
            "pages": 0,
        })
    return results


def fetch_chapter_images(comic_id, slot):
    """抓阅读页，解析 data-src 图片地址，并做域名替换与质量路径拼接。"""
    key = ("chapter", comic_id, slot)
    cached = _cache_get(key)
    if cached:
        return cached

    url = APP_READ_URL.format(comic=comic_id, slot=slot)
    html = _get_html(url, impersonate=True)   # 必须模拟指纹

    images = []
    soup = BeautifulSoup(html, "html.parser")
    cdn = _setting_cdn()
    quality = _setting_quality()
    for node in soup.select(".comic-contain .comic-contain__item"):
        src = node.get("data-src") or node.get("src")
        if not src:
            continue
        m = re.match(r"^(https?://)?([^/\s:]+)(:\d+)?(/.+)$", src)
        if not m:
            images.append(src)
            continue
        scheme, host, port, path = m.group(1) or "https://", m.group(2), m.group(3) or "", m.group(4)
        final_host = cdn if cdn else host
        images.append(f"{scheme}{final_host}{port}{quality}{path}")

    _cache_set(key, images, _CACHE_TTL["chapter"])
    return images


def _setting_cdn():
    v = os.environ.get("BAOZI_CDN_DOMAIN", DEFAULT_CDN)
    return v


def _setting_quality():
    v = os.environ.get("BAOZI_IMAGE_QUALITY", DEFAULT_QUAL)
    return v


# --------------------------------------------------------------------------
# LVGL 预解码二进制（RGB565，LVGL v8 lv_img_conv 风格头）
# --------------------------------------------------------------------------
def _lvgl_encode(img):
    img = img.convert("RGB")
    w, h = img.size
    px = img.load()
    # 12 字节头：cf=CF_TRUE_COLOR(0xFF) | w | h | stride(w*2) | reserved
    header = struct.pack("<IHHHH", 0x000000FF, w, h, w * 2, 0)
    out = bytearray(header)
    for y in range(h):
        row = bytearray()
        for x in range(w):
            pr, pg, pb = px[x, y]
            r5, g6, b5 = pr >> 3, pg >> 2, pb >> 3
            val = (r5 << 11) | (g6 << 5) | b5
            row += struct.pack("<H", val)
        out += row
    return bytes(out)


def _proxy_image():
    url = request.args.get("url")
    if not url:
        return jsonify({"code": 400, "message": "Missing parameter"}), 400

    width = request.args.get("width", type=int)
    quality = request.args.get("quality", type=int, default=80)
    if_png = _truthy(request.args.get("ifPNG"))
    if_lvgl = _truthy(request.args.get("ifLVGL"))

    try:
        r = requests.get(url, headers=IMG_HEADERS, timeout=20)
    except Exception as e:  # noqa: BLE001
        return jsonify({"code": 502, "message": f"upstream failed: {str(e)[:80]}"}), 502
    if r.status_code != 200 or not r.content:
        return jsonify({"code": 502, "message": "Upstream failed"}), 502

    try:
        img = Image.open(io.BytesIO(r.content))
        img.load()
    except Exception as e:  # noqa: BLE001
        return jsonify({"code": 502, "message": f"decode failed: {str(e)[:80]}"}), 502

    if img.mode in ("RGBA", "LA", "P"):
        # 去透明通道，铺白底转 RGB
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    if width and img.width > width:
        nh = max(1, int(img.height * width / img.width))
        img = img.resize((width, nh), Image.LANCZOS)

    # 优先级：ifLVGL=1 > ifPNG=1 > 默认 JPEG
    if if_lvgl:
        data = _lvgl_encode(img)
        return Response(
            data,
            content_type="application/octet-stream",
            headers={"Content-Length": str(len(data))},
        )
    if if_png:
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        data = buf.getvalue()
        return Response(
            data,
            content_type="image/png",
            headers={"Content-Length": str(len(data))},
        )
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=max(1, min(100, quality)))
    data = buf.getvalue()
    return Response(
        data,
        content_type="image/jpeg",
        headers={"Content-Length": str(len(data))},
    )


# --------------------------------------------------------------------------
# Flask 应用与路由
# --------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/config")
def config():
    api_url = request.host_url.rstrip("/")
    return jsonify({
        "baozi": {
            "name": "包子漫画",
            "apiUrl": api_url,
            "detailPath": "/album/<id>",
            "photoPath": "/photo/<id>/chapter/<chapter>",
            "searchPath": "/search/<text>/<page>",
            "type": "baozi",
        }
    })


@app.get("/album/<comic_id>")
def album(comic_id):
    lang = request.args.get("lang", DEFAULT_LANG)
    domain = request.args.get("domain", DEFAULT_DOMAIN)
    try:
        d = fetch_detail(comic_id, lang=lang, domain=domain)
    except Exception as e:  # noqa: BLE001
        log.warning("album %s failed: %s", comic_id, e)
        return jsonify({"code": 500, "message": f"Upstream failed: {str(e)[:80]}"}), 502

    api_url = request.host_url.rstrip("/")
    cover = d["cover"]
    if cover:
        # 走代理，让腕上漫画追加的 width/quality/ifPNG 参数生效
        cover = f"{api_url}/image/proxy?url={requests.utils.quote(cover, safe='')}"

    return jsonify({
        "item_id": d["resolved_id"],
        "name": d["title"] or comic_id,
        "page_count": d["total_chapters"],
        "cover": cover,
        "tags": d["tags"],
        "total_chapters": d["total_chapters"],
    })


@app.get("/search/<text>/<int:page>")
def search(text, page):
    lang = request.args.get("lang", DEFAULT_LANG)
    domain = request.args.get("domain", DEFAULT_DOMAIN)
    try:
        results = fetch_search(text, lang=lang, domain=domain)
    except Exception as e:  # noqa: BLE001
        log.warning("search %s failed: %s", text, e)
        return jsonify({"code": 500, "message": f"Upstream failed: {str(e)[:80]}"}), 502

    api_url = request.host_url.rstrip("/")
    for item in results:
        if item["cover_url"]:
            item["cover_url"] = (
                f"{api_url}/image/proxy?url="
                f"{requests.utils.quote(item['cover_url'], safe='')}"
            )
    return jsonify({
        "page": page,
        "has_more": False,
        "results": results,
    })


@app.get("/photo/<comic_id>/chapter/<chapter>")
def photo(comic_id, chapter):
    try:
        chapter_i = int(chapter)
    except (TypeError, ValueError):
        return jsonify({"code": 400, "message": "Missing parameter"}), 400

    # 章节编号约定：默认 1 基（chapter=1 即第1话，映射到阅读页 slot=0）。
    # 若你的腕上漫画端实际按 0 基调用，设置环境变量 BAOZI_CHAPTER_BASE=0。
    base = int(os.environ.get("BAOZI_CHAPTER_BASE", "1"))
    slots_to_try = [chapter_i - base, chapter_i - base + 1]  # 主映射 + 偏移兜底
    images = []
    used_slot = None
    for s in slots_to_try:
        if s < 0:
            continue
        try:
            imgs = fetch_chapter_images(comic_id, s)
        except Exception as e:  # noqa: BLE001
            log.warning("chapter %s slot %s failed: %s", comic_id, s, e)
            imgs = []
        if imgs:
            images, used_slot = imgs, s
            break

    if not images:
        return jsonify({"code": 404, "message": "Chapter not found"}), 404

    api_url = request.host_url.rstrip("/")
    proxied = [
        f"{api_url}/image/proxy?url={requests.utils.quote(u, safe='')}"
        for u in images
    ]
    return jsonify({
        "title": f"第{used_slot + 1}话" if used_slot is not None else "章节",
        "images": [{"url": u} for u in proxied],
    })


@app.get("/image/proxy")
def image_proxy_route():
    return _proxy_image()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
