#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量网页元素下载器 v1.1（本地化离线版）
=====================================
纯 pip 依赖 · 不安装/不驱动/不修改任何浏览器 · 适配 Python 3.10 ~ 3.13

依赖:
    python -m pip install -U requests beautifulsoup4 tqdm lxml curl_cffi

用法:
    python download_elements.py                 # 自动读取同目录 list.txt
    python download_elements.py mylist.txt      # 指定列表文件
    python download_elements.py URL1 URL2 ...   # 直接给出多个网址
    python download_elements.py --pages 1       # 队列模式（逐页串行）
    python download_elements.py --no-localize   # 只下载，不改写 HTML
    python download_elements.py --no-skip       # 忽略已完成记录，强制重下

输出:
    downloads/<原网页名>.html          已本地化，可双击离线阅读
    downloads/elements/<同名>/         该页全部素材 + 原始备份 + 映射表
    downloads/summary.csv              总览表
"""

import argparse
import base64
import csv
import glob
import hashlib
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup
from tqdm import tqdm

# ---- 可选增强：缺失时自动降级 ----
try:
    import lxml  # noqa: F401
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

try:
    from curl_cffi import requests as http      # 自带 libcurl，模拟 Chrome 指纹
    HAS_CFFI = True
except ImportError:
    import requests as http
    HAS_CFFI = False

# --------------------------------------------------------------------------
# 可调参数
# --------------------------------------------------------------------------
LIST_FILE    = "list.txt"
OUT_ROOT     = "downloads"
ASSETS_DIR   = "elements"
PAGE_WORKERS = 3            # 同时处理的网页数；1 即队列模式
FILE_WORKERS = 6            # 单页内资源并发数
HTTP_LIMIT   = 12           # 全局并发请求上限（防封关键）
RETRIES      = 3
TIMEOUT      = 25
MAX_BYTES    = 64 * 1024 * 1024      # 单文件体积上限，0 表示不限制
IMPERSONATE  = "chrome"     # 可改为 chrome124 / chrome131 等具体档位
SKIP_DONE    = True
LOCALIZE     = True
RETRY_STATUS = {429, 500, 502, 503, 504}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept-Language": "zh-CN,zh-TW;q=0.9,zh;q=0.8,en;q=0.7",
}

IMG_EXT  = r"(?:jpe?g|png|webp|gif|avif|bmp)"
ABS_IMG  = re.compile(r"""https?://[^\s"'<>()\\]+?\.""" + IMG_EXT, re.I)
REL_IMG  = re.compile(r"""(?<![\w./-])(?:[\w.@~-]+/)+[\w.@~-]+\.""" + IMG_EXT, re.I)
BARE_IMG = re.compile(r"""['"]([\w.@~-]+\.""" + IMG_EXT + r""")['"]""", re.I)
BARE_ANY = re.compile(r"""['"]([\w.@~%-]+\.""" + IMG_EXT + r""")['"]""", re.I)
HOSTVAR  = re.compile(r"""[\w$]*(?:[Hh]ost|HOST|[Cc]dn|CDN|imgServer|imgUrl|img_url)[\w$]*\s*[:=]\s*['"]([^'"]{4,})['"]""")
B64_STR  = re.compile(r"""['"]([A-Za-z0-9+/]{64,}={0,2})['"]""")
CSS_URL  = re.compile(r"""url\(\s*['"]?([^'")]+?)['"]?\s*\)""", re.I)
DATA_URI = re.compile(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)")
KEEP_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp",
            ".css", ".js", ".json", ".woff", ".woff2", ".ttf", ".svg", ".mp3", ".mp4"}
MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif", "image/avif": ".avif", "image/svg+xml": ".svg"}
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}
LINK_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-url",
              "data-background", "poster", "href")

_lock     = threading.Lock()
_http_sem = threading.BoundedSemaphore(HTTP_LIMIT)
_tls      = threading.local()
_USE_IMPERSONATE = True


def log(msg):
    tqdm.write(msg)


# --------------------------------------------------------------------------
# 网络层：线程局部会话复用 + 指纹伪装 + 退避重试 + 全局限流
# --------------------------------------------------------------------------
def _session():
    """每个线程一个会话，复用 TCP/TLS 连接与 HTTP/2 多路复用（显著提速）。"""
    global _USE_IMPERSONATE
    s = getattr(_tls, "s", None)
    if s is None:
        if HAS_CFFI:
            try:
                s = http.Session(impersonate=IMPERSONATE) if _USE_IMPERSONATE else http.Session()
            except Exception:
                _USE_IMPERSONATE = False
                s = http.Session()
        else:
            from requests.adapters import HTTPAdapter
            s = http.Session()
            ad = HTTPAdapter(pool_connections=HTTP_LIMIT, pool_maxsize=HTTP_LIMIT)
            s.mount("http://", ad)
            s.mount("https://", ad)
        _tls.s = s
    return s


def _drop_session():
    old = getattr(_tls, "s", None)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    _tls.s = None


def http_get(url, referer=None, is_page=False):
    global _USE_IMPERSONATE
    h = dict(HEADERS)
    h["Accept"] = ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
                   if is_page else "image/avif,image/webp,image/apng,*/*;q=0.8")
    if referer:
        h["Referer"] = referer          # 应对图床防盗链

    last = None
    for attempt in range(RETRIES):
        try:
            with _http_sem:
                r = _session().get(url, headers=h, timeout=TIMEOUT)
            if r.status_code in RETRY_STATUS and attempt < RETRIES - 1:
                last = RuntimeError(f"HTTP {r.status_code}")
                time.sleep(min(8.0, 2 ** attempt) + random.random())
                continue
            return r
        except Exception as e:
            last = e
            if "impersonate" in str(e).lower():
                _USE_IMPERSONATE = False
                _drop_session()
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last if last is not None else RuntimeError("请求失败")


def decode_text(resp):
    """不依赖库的 encoding 属性，自行嗅探编码（utf-8 / gb18030 / big5）。"""
    raw = resp.content
    encs = []
    m = re.search(r"charset=([\w-]+)", resp.headers.get("Content-Type", ""), re.I)
    if m:
        encs.append(m.group(1))
    m = re.search(r"""charset\s*=\s*["']?([\w-]+)""", raw[:4096].decode("ascii", "ignore"), re.I)
    if m:
        encs.append(m.group(1))
    encs += ["utf-8", "gb18030", "big5"]
    for enc in dict.fromkeys(encs):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# 内联 JS 解析（纯字符串运算，不执行任何远程代码）
# --------------------------------------------------------------------------
def unpack_packer(js):
    """还原 eval(function(p,a,c,k,e,d){...}) 打包的 JS。"""
    m = re.search(r"\}\s*\(\s*'((?:\\.|[^'\\])*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,"
                  r"\s*'((?:\\.|[^'\\])*)'\.split\('\|'\)", js, re.S)
    if not m:
        return ""
    payload = m.group(1).replace("\\'", "'").replace("\\\\", "\\")
    words = m.group(4).split("|")

    def repl(mo):
        try:
            i = int(mo.group(0), 36)
        except ValueError:
            return mo.group(0)
        return words[i] if i < len(words) and words[i] else mo.group(0)

    return re.sub(r"\b[0-9a-zA-Z]+\b", repl, payload)


def norm(s):
    return s.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/").replace("&amp;", "&")


def mine_text(text, base_url, out, depth=0):
    """从 JS / JSON / base64 解码结果中挖掘图片地址，保持原顺序。"""
    t = norm(text)
    out.extend(ABS_IMG.findall(t))
    hosts = [h if h.startswith("http") else "https://" + h.lstrip("/") for h in HOSTVAR.findall(t)]
    hosts = [h if h.endswith("/") else h + "/" for h in hosts]
    for rel in REL_IMG.findall(t):
        if rel.startswith("http"):
            continue
        out += [urljoin(h, rel.lstrip("/")) for h in hosts] if hosts else [urljoin(base_url, rel)]
    if hosts:
        for fn in BARE_IMG.findall(t):
            out += [urljoin(h, fn.lstrip("/")) for h in hosts]
    if depth >= 2:
        return
    for b in B64_STR.findall(t):
        try:
            dec = base64.b64decode(b).decode("utf-8", "ignore")
        except Exception:
            continue
        if "http" in dec or re.search(r"\." + IMG_EXT, dec, re.I):
            mine_text(dec, base_url, out, depth + 1)
            try:
                arr = json.loads(norm(dec))
                if isinstance(arr, list):
                    out += [urljoin(base_url, x) for x in arr if isinstance(x, str)]
            except Exception:
                pass


# --------------------------------------------------------------------------
# 资源收集
# --------------------------------------------------------------------------
def collect(base_url, html, soup):
    urls, datas = [], []

    def add(u):
        u = (u or "").strip().strip("'\"")
        if not u or u.startswith(("#", "javascript:", "mailto:")):
            return
        if u.startswith("data:"):
            m = DATA_URI.match(u)
            if m:
                datas.append(m.groups())
            return
        urls.append(urljoin(base_url, u))

    for img in soup.find_all("img"):
        for a in ("src", "data-src", "data-original", "data-lazy-src", "data-url"):
            if img.get(a):
                add(img[a])
                break
        if img.get("srcset"):                       # P5：全部候选
            for cand in img["srcset"].split(","):
                bits = cand.strip().split()
                if bits:
                    add(bits[0])
    for t in soup.find_all(["source", "audio", "video", "script"]):
        add(t.get("src"))
    for t in soup.find_all(["video", "audio"]):     # P5：poster
        add(t.get("poster"))
    for l in soup.find_all("link"):                 # P5：图标
        href, rel = l.get("href"), l.get("rel") or []
        if href and ("stylesheet" in rel or "preload" in rel or "icon" in rel
                     or urlparse(href).path.lower().endswith(".css")):
            add(href)
    for t in soup.find_all(style=True):
        for m in CSS_URL.finditer(t["style"]):
            add(m.group(1))
    for st in soup.find_all("style"):
        for m in CSS_URL.finditer(st.get_text()):
            add(m.group(1))
    for m in DATA_URI.finditer(html):
        datas.append(m.groups())

    js_urls = []
    for sc in soup.find_all("script"):
        code = sc.get_text()
        if not code.strip():
            continue
        mine_text(code, base_url, js_urls)
        packed = unpack_packer(code)
        if packed:
            mine_text(packed, base_url, js_urls)
    mine_text(html, base_url, js_urls)              # 全文兜底
    urls += js_urls

    def dedup(seq, key=lambda x: x):
        seen, res = set(), []
        for x in seq:
            k = key(x)
            if k not in seen:
                seen.add(k)
                res.append(x)
        return res

    return dedup(urls), dedup(datas, key=lambda d: d[1][:64]), len(js_urls)


def expand_css(page_url, css_urls, datas):
    """解析外链 CSS 中的 url(...)，相对路径基于 CSS 自身地址。"""
    extra = []
    for cu in css_urls:
        try:
            text = decode_text(http_get(cu, referer=page_url))
            for m in CSS_URL.finditer(text):
                u = m.group(1).strip().strip("'\"")
                if u.startswith("data:"):
                    mm = DATA_URI.match(u)
                    if mm:
                        datas.append(mm.groups())
                elif u and not u.startswith("#"):
                    extra.append(urljoin(cu, u))
        except Exception as e:
            log(f"  [!] CSS 解析失败 {cu}: {type(e).__name__}")
    return extra


# --------------------------------------------------------------------------
# 命名（直接使用网页原名）
# --------------------------------------------------------------------------
def sanitize(name, limit=90):
    return re.sub(r"[^\w.\-]+", "_", unquote(name)).strip("._-")[:limit]


def page_stem(url):
    p = urlparse(url)
    segs = [s for s in p.path.split("/") if s]
    if segs and "." in segs[-1]:
        stem, ext = os.path.splitext(segs[-1])
    else:
        stem, ext = (segs[-1] if segs else "index"), ".html"
    if not ext or len(ext) > 6:
        ext = ".html"
    stem = sanitize(stem, 70) or "index"
    if p.query:
        stem += "_" + hashlib.md5(p.query.encode("utf-8")).hexdigest()[:6]
    if stem.upper() in RESERVED:
        stem = "_" + stem
    return stem, ext


def build_plan(urls):
    used, plan = set(), []
    for url in urls:
        stem, ext = page_stem(url)
        name, i = stem, 1
        while name in used:                 # 最低限度防覆盖
            i += 1
            name = f"{stem}_{i}"
        used.add(name)
        plan.append({"url": url, "name": name,
                     "html": os.path.join(OUT_ROOT, name + ext),
                     "assets": os.path.join(OUT_ROOT, ASSETS_DIR, name)})
    return plan


def safe_name(url, i, ctype=""):
    raw = unquote(os.path.basename(urlparse(url).path)) or "file"
    stem, ext = os.path.splitext(raw)
    stem = re.sub(r"[^\w.\-]+", "_", stem)[:50] or "file"
    ext = ext.lower()
    if ext not in KEEP_EXT:
        ext = MIME_EXT.get(ctype.split(";")[0].strip(), ".bin")
    return f"{i:04d}_{stem}{ext}"


# --------------------------------------------------------------------------
# 本地化改写
# --------------------------------------------------------------------------
def build_base_map(mapping):
    tmp = {}
    for u, fn in mapping.items():
        b = unquote(os.path.basename(urlparse(u).path))
        if b:
            tmp.setdefault(b, set()).add(fn)
    return {b: next(iter(s)) for b, s in tmp.items() if len(s) == 1}


def rewrite_css_text(text, base_url, mapping, data_map, prefix):
    def join(fn):
        return f"{prefix}/{fn}" if prefix else fn

    def sub(m):
        raw = m.group(1).strip().strip("'\"")
        if raw.startswith("data:"):
            fn = data_map.get(raw)
            return f"url({join(fn)})" if fn else m.group(0)
        fn = mapping.get(urljoin(base_url, raw))
        return f"url({join(fn)})" if fn else m.group(0)

    return CSS_URL.sub(sub, text)


def rewrite_js_text(code, mapping, base_map, dir_rel):
    # 1) 绝对地址的各种书写形式（长者优先，避免子串误伤）
    variants = []
    for u, fn in mapping.items():
        target = f"{dir_rel}/{fn}"
        p = urlparse(u)
        tail = p.path + (("?" + p.query) if p.query else "")
        forms = {u, u.replace("/", "\\/"), u.replace("&", "&amp;"),
                 u.replace("/", "\\u002F")}
        if p.scheme in ("http", "https") and p.netloc:
            forms.add("//" + p.netloc + tail)
            forms.add(tail)
        for f in forms:
            if len(f) > 8:
                variants.append((f, target))
    variants.sort(key=lambda x: -len(x[0]))
    for f, t in variants:
        if f in code:
            code = code.replace(f, t)

    # 2) P3：仅改写确实用于拼接已下载资源的图床前缀，避免误伤 API 域名
    used_hosts = set()

    def host_sub(m):
        val = m.group(1)
        nv = val if val.startswith("http") else "https://" + val.lstrip("/")
        nv = nv if nv.endswith("/") else nv + "/"
        if any(u.startswith(nv) for u in mapping):
            used_hosts.add(val)
            return m.group(0).replace(val, dir_rel + "/")
        return m.group(0)

    code = HOSTVAR.sub(host_sub, code)

    # 3) 裸文件名改写必须与前缀改写联动，否则会拼出错误地址
    if used_hosts:
        def bare_sub(m):
            fn = base_map.get(m.group(1))
            return m.group(0).replace(m.group(1), fn) if fn else m.group(0)
        code = BARE_ANY.sub(bare_sub, code)
    return code


SHIM_TMPL = r"""(function () {
  var DIR = __DIR__, MAP = __MAP__, BASE = __BASE__;
  function local(u) {
    if (typeof u !== 'string' || !u) return u;
    if (u.indexOf('data:') === 0 || u.indexOf('blob:') === 0 || u.indexOf(DIR) === 0) return u;
    var clean = u.replace(/\\\//g, '/').split('#')[0], noq = clean.split('?')[0];
    if (MAP[u]) return DIR + MAP[u];
    if (MAP[clean]) return DIR + MAP[clean];
    if (MAP[noq]) return DIR + MAP[noq];
    var b = noq.substring(noq.lastIndexOf('/') + 1);
    try { b = decodeURIComponent(b); } catch (e) {}
    if (BASE[b]) return DIR + BASE[b];
    return u;
  }
  window.__localAsset = local;
  var ATTRS = ['src','data-src','data-original','data-lazy-src','data-url','poster','href'];
  var URL_RE = /url\(\s*(['"]?)([^'")]+)\1\s*\)/g;
  function fixStyleText(el) {
    if (!el.tagName || el.tagName.toUpperCase() !== 'STYLE') return;
    var t = el.textContent || '';
    if (t.indexOf('url(') < 0) return;
    var nt = t.replace(URL_RE, function (m, q, x) { return 'url(' + q + local(x) + q + ')'; });
    if (nt !== t) el.textContent = nt;
  }
  function fixEl(el) {
    if (!el || el.nodeType !== 1 || !el.getAttribute) return;
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      if (el.hasAttribute(a)) {
        var v = el.getAttribute(a), n = local(v);
        if (n !== v) el.setAttribute(a, n);
      }
    }
    var s = el.getAttribute('style');
    if (s && s.indexOf('url(') >= 0) {
      var ns = s.replace(URL_RE, function (m, q, x) { return 'url(' + q + local(x) + q + ')'; });
      if (ns !== s) el.setAttribute('style', ns);
    }
    fixStyleText(el);
  }
  function sweep(root) {
    var l = (root || document).querySelectorAll('img,source,video,audio,script,link,style,[style]');
    for (var i = 0; i < l.length; i++) fixEl(l[i]);
  }
  var _sa = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (n, v) {
    if (typeof v === 'string' && ATTRS.indexOf(String(n).toLowerCase()) >= 0) v = local(v);
    return _sa.call(this, n, v);
  };
  try {
    var d = Object.getOwnPropertyDescriptor(HTMLImageElement.prototype, 'src');
    if (d && d.set) {
      Object.defineProperty(HTMLImageElement.prototype, 'src', {
        configurable: true,
        get: function () { return d.get.call(this); },
        set: function (v) { _sa.call(this, 'src', local(v)); }
      });
    }
  } catch (e) {}
  if (window.fetch) {
    var _f = window.fetch;
    window.fetch = function (i, o) { if (typeof i === 'string') i = local(i); return _f.call(this, i, o); };
  }
  if (window.XMLHttpRequest) {
    var _o = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function () {
      var a = Array.prototype.slice.call(arguments);
      if (typeof a[1] === 'string') a[1] = local(a[1]);
      return _o.apply(this, a);
    };
  }
  if (window.MutationObserver) {
    new MutationObserver(function (ms) {
      for (var i = 0; i < ms.length; i++) {
        var m = ms[i];
        if (m.type === 'attributes') fixEl(m.target);
        for (var j = 0; j < m.addedNodes.length; j++) {
          var n = m.addedNodes[j];
          if (n.nodeType === 1) { fixEl(n); sweep(n); }
        }
      }
    }).observe(document.documentElement, {
      subtree: true, childList: true, attributes: true,
      attributeFilter: ['src','data-src','data-original','data-lazy-src','data-url','poster','href','style']
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { sweep(document); });
  } else { sweep(document); }
})();"""


def build_shim(dir_rel, mapping, base_map):
    def js_json(obj):
        return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")
    return (SHIM_TMPL.replace("__DIR__", js_json(dir_rel + "/"))
                     .replace("__MAP__", js_json(mapping))
                     .replace("__BASE__", js_json(base_map)))


def localize_html(page_url, dir_rel, mapping, data_map, soup):
    # P2：清理会破坏离线渲染的标签与属性
    for b in soup.find_all("base"):
        b.decompose()
    for t in soup.find_all(True):
        for a in ("integrity", "crossorigin", "nonce"):
            if t.has_attr(a):
                del t[a]
    for m in soup.find_all("meta", attrs={"http-equiv": re.compile(r"content-security-policy", re.I)}):
        m.decompose()

    base_map = build_base_map(mapping)

    def abs_of(v):
        v = (v or "").strip().strip("'\"")
        if not v or v.startswith(("data:", "javascript:", "mailto:", "#")):
            return None
        return urljoin(page_url, v)

    for tag in soup.find_all(True):
        for a in LINK_ATTRS:
            if not tag.has_attr(a):
                continue
            v = tag[a]
            if isinstance(v, list):
                v = " ".join(v)
            if v.startswith("data:"):
                fn = data_map.get(v)
                if fn:
                    tag[a] = f"{dir_rel}/{fn}"
                continue
            u = abs_of(v)
            fn = mapping.get(u) if u else None
            if fn:
                tag[a] = f"{dir_rel}/{fn}"
        if tag.has_attr("srcset"):
            parts = []
            for cand in tag["srcset"].split(","):
                bits = cand.strip().split()
                if not bits:
                    continue
                u = abs_of(bits[0])
                fn = mapping.get(u) if u else None
                if fn:
                    bits[0] = f"{dir_rel}/{fn}"
                parts.append(" ".join(bits))
            if parts:
                tag["srcset"] = ", ".join(parts)
        if tag.has_attr("style"):
            tag["style"] = rewrite_css_text(tag["style"], page_url, mapping, data_map, dir_rel)

    for st in soup.find_all("style"):
        st.string = rewrite_css_text(st.get_text(), page_url, mapping, data_map, dir_rel)
    for sc in soup.find_all("script"):
        if sc.get("src"):
            continue
        code = sc.get_text()
        if code.strip():
            new = rewrite_js_text(code, mapping, base_map, dir_rel)
            if new != code:
                sc.string = new

    out = str(soup)
    for uri, fn in data_map.items():
        if uri in out:
            out = out.replace(uri, f"{dir_rel}/{fn}")

    shim = "<script>\n" + build_shim(dir_rel, mapping, base_map) + "\n</script>\n"
    m = re.search(r"<head[^>]*>", out, re.I)
    if m:
        out = out[:m.end()] + "\n" + shim + out[m.end():]
    else:
        m2 = re.search(r"<html[^>]*>", out, re.I)
        out = (out[:m2.end()] + "<head>" + shim + "</head>" + out[m2.end():]) if m2 else (shim + out)
    return out


def localize_css_files(adir, mapping, data_map, css_pairs):
    for css_url, fname in css_pairs:
        path = os.path.join(adir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read()
            try:                                  # P4：严格解码，避免损坏非 UTF-8 文件
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            new = rewrite_css_text(text, css_url, mapping, data_map, "")
            if new != text:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
        except Exception as e:
            log(f"  [!] CSS 本地化失败 {fname}: {type(e).__name__}")


# --------------------------------------------------------------------------
# 下载与单页处理
# --------------------------------------------------------------------------
def download_one(args):
    i, url, referer, outdir, resumable = args
    if resumable:                                   # P6：单文件续传
        pattern = os.path.join(glob.escape(outdir), f"{i:04d}_*")
        hit = [p for p in glob.glob(pattern) if os.path.getsize(p) > 0]
        if hit:
            return url, os.path.basename(hit[0])
    try:
        r = http_get(url, referer=referer)
        r.raise_for_status()
        data = r.content
        if MAX_BYTES and len(data) > MAX_BYTES:
            log(f"  [!] 超过体积上限已跳过 {url}（{len(data)} 字节）")
            return url, None
        fname = safe_name(url, i, r.headers.get("Content-Type", ""))
        with open(os.path.join(outdir, fname), "wb") as f:
            f.write(data)
        return url, fname
    except Exception as e:
        log(f"  [!] {url} -> {type(e).__name__}: {str(e)[:70]}")
        return url, None


def process_page(task, bar):
    url, name = task["url"], task["name"]
    html_path, adir = task["html"], task["assets"]
    manifest = os.path.join(adir, "_manifest.json")
    res_file = os.path.join(adir, "_resources.txt")
    dir_rel = f"{ASSETS_DIR}/{name}"
    row = {"url": url, "html": os.path.basename(html_path), "assets": dir_rel,
           "status": "", "ok": 0, "total": 0, "error": ""}

    if SKIP_DONE and os.path.isfile(html_path) and os.path.isfile(manifest):
        row["status"] = "skipped"
        log(f"[=] 已完成，跳过: {os.path.basename(html_path)}")
        return row

    os.makedirs(adir, exist_ok=True)
    log(f"[*] 开始: {url}")
    r = http_get(url, is_page=True)
    r.raise_for_status()
    html = decode_text(r)
    soup = BeautifulSoup(html, PARSER)              # S4：全页只解析一次

    targets, datas, n_js = collect(url, html, soup)
    with open(os.path.join(adir, "_page_raw.html"), "w", encoding="utf-8") as f:
        f.write(html)

    css_urls = [u for u in targets if urlparse(u).path.lower().endswith(".css")]
    for u in expand_css(url, css_urls, datas):
        if u not in targets:
            targets.append(u)

    resumable = False                               # 清单完全一致才允许按序号续传
    if os.path.isfile(res_file):
        try:
            with open(res_file, encoding="utf-8") as f:
                resumable = f.read().splitlines() == targets
        except Exception:
            resumable = False
    with open(res_file, "w", encoding="utf-8") as f:
        f.write("\n".join(targets))

    n_img = sum(1 for u in targets if re.search(r"\." + IMG_EXT + r"$", urlparse(u).path, re.I))
    log(f"[+] {os.path.basename(html_path)}: 资源 {len(targets)} 个（图片 {n_img}，JS 挖掘 {n_js}）"
        f"+ 内联 {len(datas)}" + ("  [续传]" if resumable else ""))
    if n_img == 0:
        log(f"    [!] 未发现图片地址，请检查 {adir}/_page_raw.html 中的 .jpg / chapterImages / imgHost")

    total = len(targets) + len(datas)
    with _lock:
        bar.total = (bar.total or 0) + total
        bar.refresh()

    ok, data_map = 0, {}
    for i, (mime, b64) in enumerate(datas, 1):
        try:
            ext = mime.split("/")[-1].split("+")[0]
            fname = f"{i:04d}_inline.{ext}"
            with open(os.path.join(adir, fname), "wb") as f:
                f.write(base64.b64decode(b64))
            data_map[f"data:{mime};base64,{b64}"] = fname
            ok += 1
        except Exception:
            pass
        bar.update(1)

    # B9：网络资源编号从内联图片之后开始，避免重号
    jobs = [(i + len(datas) + 1, u, url, adir, resumable) for i, u in enumerate(targets)]
    mapping = {}
    with ThreadPoolExecutor(FILE_WORKERS) as pool:
        for u, fn in pool.map(download_one, jobs):
            if fn:
                mapping[u] = fn
                ok += 1
            bar.update(1)

    if LOCALIZE:
        try:
            css_pairs = [(u, mapping[u]) for u in css_urls if u in mapping]
            localize_css_files(adir, mapping, data_map, css_pairs)
            out_html = localize_html(url, dir_rel, mapping, data_map, soup)
        except Exception as e:
            log(f"  [!] 本地化失败，保留原始 HTML: {type(e).__name__}: {e}")
            out_html = html
    else:
        out_html = html
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(out_html)

    with open(manifest, "w", encoding="utf-8") as f:
        json.dump({"url": url, "html": os.path.basename(html_path), "localized": bool(LOCALIZE),
                   "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": ok, "total": total,
                   "mapping": mapping, "resources": targets}, f, ensure_ascii=False, indent=2)

    row.update(status="done", ok=ok, total=total)
    log(f"[✓] {os.path.basename(html_path)}: 成功 {ok}/{total} → {dir_rel}/"
        + ("（已本地化）" if LOCALIZE else ""))
    return row


# --------------------------------------------------------------------------
# 输入与主流程
# --------------------------------------------------------------------------
def read_list(path):
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            with open(path, encoding=enc) as f:
                text = f.read()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    return [ln.strip().split()[0] for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def normalize_urls(items):
    out, seen = [], set()
    for u in items:
        u = u.strip().strip('"\'')
        if not u or u.startswith("#"):
            continue
        if not re.match(r"^https?://", u, re.I):
            u = "https://" + u
        if urlparse(u).netloc and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def gather_urls(args_inputs):
    if args_inputs:
        items = []
        for it in args_inputs:
            items += read_list(it) if os.path.isfile(it) else re.split(r"[\s,;]+", it)
        return normalize_urls(items)

    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), LIST_FILE)
    for cand in (LIST_FILE, here):
        if os.path.isfile(cand):
            log(f"[*] 读取列表: {os.path.abspath(cand)}")
            return normalize_urls(read_list(cand))

    print(f"未找到 {LIST_FILE}。请粘贴网址（每行一个，空行结束）：")
    items = []
    while True:
        try:
            line = input().strip()
        except EOFError:
            break
        if not line:
            break
        items += re.split(r"[\s,;]+", line)
    return normalize_urls(items)


def main():
    global OUT_ROOT, ASSETS_DIR, PAGE_WORKERS, FILE_WORKERS, HTTP_LIMIT
    global SKIP_DONE, LOCALIZE, IMPERSONATE, MAX_BYTES, _http_sem
    ap = argparse.ArgumentParser(description="批量网页元素下载器 v1.1（HTML 平铺 + 本地化离线，无浏览器）")
    ap.add_argument("inputs", nargs="*", help="list.txt 路径或若干网址；留空则自动查找 list.txt")
    ap.add_argument("--out", default=OUT_ROOT, help="输出根目录（HTML 存放于此）")
    ap.add_argument("--assets-dir", default=ASSETS_DIR, help="素材总目录名，默认 elements")
    ap.add_argument("--pages", type=int, default=PAGE_WORKERS, help="同时处理的网页数，1 为队列模式")
    ap.add_argument("--files", type=int, default=FILE_WORKERS, help="单页内资源并发数")
    ap.add_argument("--limit", type=int, default=HTTP_LIMIT, help="全局并发请求上限")
    ap.add_argument("--impersonate", default=IMPERSONATE, help="curl_cffi 指纹档位，如 chrome / chrome124")
    ap.add_argument("--max-mb", type=int, default=MAX_BYTES // (1024 * 1024), help="单文件体积上限(MB)，0 不限")
    ap.add_argument("--no-skip", action="store_true", help="忽略已完成记录，强制重下")
    ap.add_argument("--no-localize", action="store_true", help="只下载资源，不改写 HTML")
    a = ap.parse_args()

    OUT_ROOT, ASSETS_DIR = a.out, a.assets_dir
    PAGE_WORKERS, FILE_WORKERS, HTTP_LIMIT = max(1, a.pages), max(1, a.files), max(1, a.limit)
    IMPERSONATE = a.impersonate
    MAX_BYTES = max(0, a.max_mb) * 1024 * 1024
    SKIP_DONE, LOCALIZE = not a.no_skip, not a.no_localize
    _http_sem = threading.BoundedSemaphore(HTTP_LIMIT)

    urls = gather_urls(a.inputs)
    if not urls:
        print("没有有效网址。")
        return

    os.makedirs(OUT_ROOT, exist_ok=True)
    plan = build_plan(urls)
    log(f"[*] 共 {len(urls)} 个网址 | 解析器 {PARSER} | 通道 "
        f"{'curl_cffi (' + IMPERSONATE + ')' if HAS_CFFI else '标准 requests'} | "
        f"网页并发 {PAGE_WORKERS} × 资源并发 {FILE_WORKERS}（上限 {HTTP_LIMIT}）")
    log(f"[*] HTML → {os.path.abspath(OUT_ROOT)} | 素材 → {ASSETS_DIR}/<与 HTML 同名>/ | "
        f"本地化 {'开启' if LOCALIZE else '关闭'}")

    rows = []
    bar = tqdm(total=0, unit="个", desc="总进度", dynamic_ncols=True)
    pool = ThreadPoolExecutor(PAGE_WORKERS)
    futures = {pool.submit(process_page, t, bar): t for t in plan}
    try:
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                rows.append(fut.result())
            except Exception as e:
                rows.append({"url": t["url"], "html": os.path.basename(t["html"]),
                             "assets": f"{ASSETS_DIR}/{t['name']}", "status": "failed",
                             "ok": 0, "total": 0, "error": f"{type(e).__name__}: {e}"})
                log(f"[✗] 失败 {t['url']} -> {type(e).__name__}: {str(e)[:90]}")
    except KeyboardInterrupt:
        log("\n[!] 用户中断，正在取消尚未开始的任务 ...")
        for f in futures:
            f.cancel()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        bar.close()

    order = {t["url"]: i for i, t in enumerate(plan)}
    rows.sort(key=lambda r: order.get(r["url"], 0))
    csv_path = os.path.join(OUT_ROOT, "summary.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["url", "html", "assets", "status", "ok", "total", "error"])
        w.writeheader()
        w.writerows(rows)

    log(f"\n[✓] 全部结束：完成 {sum(1 for r in rows if r['status'] == 'done')} 页，"
        f"跳过 {sum(1 for r in rows if r['status'] == 'skipped')} 页，"
        f"失败 {sum(1 for r in rows if r['status'] == 'failed')} 页，"
        f"累计文件 {sum(r['ok'] for r in rows)} 个")
    log(f"    HTML 目录: {os.path.abspath(OUT_ROOT)}")
    log(f"    素材目录: {os.path.abspath(os.path.join(OUT_ROOT, ASSETS_DIR))}")
    log(f"    总览表格: {os.path.abspath(csv_path)}")
    if LOCALIZE:
        log("    离线阅读：直接双击 HTML；若某页异常，可在该目录执行 python -m http.server 后浏览器访问。")


if __name__ == "__main__":
    main()
