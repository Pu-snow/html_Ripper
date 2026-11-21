# html_Ripper
简单来说，这是为了更方便的批量保存网页以本地离线浏览，且我有意将此工具尽可能简单使用。由于一开始面向的是漫画网站，所以可能更偏向对图像的爬取

（请原谅我的技术力之低，不得不使用了AI工具）

================================================================================
批量网页元素下载器 (Batch Web Asset Downloader)
版本: 1.0  |  形态: 单文件 Python 脚本  |  特性: 纯 pip 依赖 / 零浏览器 / 离线本地化
================================================================================

零、文件树
工作目录/
├─ download_elements.py          主程序
├─ list.txt                      网址清单（一行一个，# 为注释）
├─ README.txt                    说明文档（见第五节）
└─ downloads/                    ← 运行后生成
   ├─ 0_0.html                   已本地化，可双击离线阅读
   ├─ 0_1.html
   ├─ 0_2.html
   ├─ summary.csv                总览：url / html / assets / status / ok / total / error
   └─ elements/
      ├─ 0_0/
      │  ├─ 0001_01.jpg          素材（四位序号保序）
      │  ├─ 0002_02.jpg
      │  ├─ 0003_main.css        已改写内部 url() 为同目录文件名
      │  ├─ 0004_reader.js
      │  ├─ _page_raw.html       原始网页备份（未改写）
      │  ├─ _resources.txt       提取到的全部资源地址（有序）
      │  └─ _manifest.json       下载记录 + URL→本地文件映射
      ├─ 0_1/
      └─ 0_2/

一、功能概述
--------------------------------------------------------------------------------
1. 批量抓取：从 list.txt 或命令行读取多个网址，逐页保存 HTML 并下载页面全部元素。
2. 元素覆盖：
   - img / source / audio / video，含懒加载 data-src、data-original、data-lazy-src、srcset
   - 外链 CSS 与 JS；CSS 内部 url(...) 引用的背景图、字体
   - 页面内联 <style> 与 style 属性中的 url(...)
   - data:image/...;base64 内联图片（落地为独立文件）
   - 内联 JavaScript 中的图片数组：支持 JSON 数组、转义斜杠、base64 字符串、
     图床前缀变量拼接，以及 eval(function(p,a,c,k,e,d){...}) 打包代码的纯 Python 还原
3. 离线本地化：将 HTML 与 CSS 中的资源引用改写为本地相对路径，并在 <head> 注入
   无依赖的运行时垫片，拦截 setAttribute / img.src / fetch / XHR / DOM 变动，
   把页面 JS 动态生成的远程地址映射到已下载的本地文件。
4. 调度能力：网页级并发 + 资源级并发 + 全局请求上限；支持队列串行模式。
5. 断点续跑：已完成的页面自动跳过；失败页重跑即可补齐。
6. 归档追溯：每页保留原始网页备份、资源清单与 URL→本地文件映射表。

明确不做的事：不安装、不驱动、不修改任何浏览器；不执行任何远程代码（JS 还原为
纯字符串运算）；不绕过需要完整浏览器挑战（如 Cloudflare JS Challenge）的防护。


二、环境依赖
--------------------------------------------------------------------------------
Python : 3.10 ~ 3.13（推荐 3.13，本机 CPython）
操作系统: Windows / macOS / Linux
必需包 : requests, beautifulsoup4, tqdm
建议包 : lxml（高速解析器）, curl_cffi（Chrome TLS/HTTP2 指纹与连接复用）
安装   :
    python -m pip install -U pip
    python -m pip install -U requests beautifulsoup4 tqdm lxml curl_cffi

说明:
- lxml 缺失时自动回退标准库 html.parser；curl_cffi 缺失时自动回退 requests。
- curl_cffi 的 wheel 为 cp310-abi3 稳定 ABI，可直接安装于 Python 3.13。
- Android/Termux 等平台若无预编译 wheel，仅安装三个必需包即可运行。
- 不支持在浏览器内的 Python（Pyodide/WebAssembly）中运行：其网络受 CORS 限制。


三、使用流程
--------------------------------------------------------------------------------
步骤 1  将脚本与 list.txt 放在同一目录。list.txt 一行一个网址：
            # 这是注释行
            https://example.com/comic/chapter/xxx/0_0.html
            https://example.com/comic/chapter/xxx/0_1.html   行尾备注可写

步骤 2  执行：
            python download_elements.py

步骤 3  等待进度条结束，查看 downloads/summary.csv 核对结果。

步骤 4  双击 downloads/0_0.html 离线阅读。若页面异常，见第六节。

命令行参数:
    python download_elements.py [列表文件或网址 ...]
      --out DIR          输出根目录，默认 downloads
      --assets-dir NAME  素材总目录名，默认 elements
      --pages N          同时处理的网页数，默认 3；设为 1 即队列串行
      --files N          单页内资源并发数，默认 6
      --limit N          全局并发请求上限，默认 12（防封关键）
      --no-skip          忽略已完成记录，强制重新下载
      --no-localize      只下载资源，不改写 HTML

常用示例:
    python download_elements.py                    读取同目录 list.txt
    python download_elements.py mylist.txt         指定列表
    python download_elements.py URL1 URL2          直接传网址
    python download_elements.py --pages 1 --limit 4   保守模式，站点严格时使用
    python download_elements.py --no-skip          全部重下


四、文件结构
--------------------------------------------------------------------------------
工作目录/
  download_elements.py      主程序
  list.txt                  网址清单
  README.txt                本文档
  downloads/                输出根目录
    0_0.html                主体 HTML（原名，已本地化）
    0_1.html
    summary.csv             总览表（UTF-8 BOM，可用 Excel 打开）
    elements/               素材总目录，按页隔离
      0_0/
        0001_01.jpg         素材文件，四位序号保持页面顺序
        0003_main.css       已改写内部 url() 指向同目录文件
        _page_raw.html      原始网页备份（未做任何改写）
        _resources.txt      本页提取到的全部资源地址（有序）
        _manifest.json      下载记录与 URL→本地文件映射

命名规则:
- HTML 直接使用网页原文件名，平铺于输出根目录。
- 素材目录名与 HTML 主名严格一致，一一对应，绝不合并。
- 同一批次内若出现同名（不同网址），后者追加 _2、_3 以防覆盖。
- 素材文件名形如「四位序号_原始名.扩展名」；扩展名缺失时按 Content-Type 补全。


五、工作原理（五阶段）
--------------------------------------------------------------------------------
阶段 1  抓取
    统一请求入口 http_get：优先以 curl_cffi 模拟 Chrome 的 TLS/HTTP2 指纹，
    失败自动降级为 requests；带 Referer 以应对图床防盗链；失败重试与退避；
    全局信号量限制在途请求数。编码由响应头与 <meta> 嗅探，回退 utf-8/gb18030/big5。

阶段 2  提取
    BeautifulSoup 解析常规标签与属性；正则扫描 <style> 与 style 属性中的 url(...)；
    对内联 JS 执行文本挖掘：绝对地址、转义斜杠、协议相对/根相对路径、
    图床前缀变量 + 裸文件名拼接、base64 字符串解码、p.a.c.k.e.r 打包还原。
    全部结果按出现顺序去重，保证漫画页码顺序。

阶段 3  下载
    网页级线程池 × 资源级线程池并发下载；外链 CSS 先行解析以补充其中的资源；
    base64 内联图片直接落地。每个成功项记入 URL→本地文件名映射。

阶段 4  本地化
    a) 改写已下载 CSS 内部的 url(...) 为同目录文件名；
    b) 改写 HTML 标签属性、srcset、style、<style>、内联 JS 中的地址为相对路径；
    c) 在 <head> 最前注入运行时垫片，按「完整地址 / 原始文件名」双表映射，
       拦截属性写入与网络 API，兜底处理 JS 运行时生成的地址。
    原始网页始终备份为 _page_raw.html，可随时回退。

阶段 5  归档
    写出 _resources.txt、_manifest.json 与全局 summary.csv；
    下次运行时，HTML 与 _manifest.json 同时存在的页面将被跳过。


六、离线阅读与常见问题
--------------------------------------------------------------------------------
Q1 双击 HTML 后图片不显示？
   依次检查：
   1) elements/<名>/_resources.txt 是否列出了图片地址；
   2) _manifest.json 的 mapping 中这些地址是否下载成功（失败项不会被改写）；
   3) 页面是否依赖 XHR/fetch 读取本地 JSON —— Chrome 在 file:// 下禁止此行为。
      解决：在 downloads/ 目录执行  python -m http.server 8000
            浏览器访问  http://localhost:8000/0_0.html
      （仅启动临时本地服务，不改动浏览器配置。）

Q2 提示「未发现图片地址」？
   打开 _page_raw.html，搜索 .jpg / .webp / chapterImages / imgHost 等关键字，
   确认地址存放形式。若由 JS 按页码动态计算生成，则静态提取无法覆盖。

Q3 大量 403 / 超时？
   降低并发：--pages 1 --limit 4；确认已安装 curl_cffi；
   部分图床需特定 Referer，脚本已自动携带页面地址。

Q4 中文乱码？
   脚本按响应头与 meta 嗅探编码。若仍异常，可强制指定编码后重跑该页。

Q5 Windows 提示路径过长？
   将输出目录设为短路径，例如 --out D:\dl。

Q6 想保留原始网页而不改写？
   加 --no-localize；任何情况下 _page_raw.html 都是未改写的原件。


七、已知限制
--------------------------------------------------------------------------------
1. 无法通过需要完整浏览器执行的 JS 挑战（Cloudflare 等）。
2. 图片地址若在运行时由算法计算而非出现在源码中，提取与映射均会落空。
3. 章节间的导航链接仍指向远程站点（未做跨页链接本地化）。
4. 运行时注入的 <style> 文本中的背景图，垫片暂不处理。
5. 素材文件名带序号前缀；若站点 JS 依赖原始文件名解析页码，可能需去掉前缀。
6. 单文件级续传需资源清单与上次完全一致方才生效。


八、合规与安全提示
--------------------------------------------------------------------------------
- 本工具仅用于个人离线阅读与技术研究，请遵守目标站点的服务条款与 robots 规则，
  并尊重著作权；请勿再分发下载内容。
- 默认并发上限为 12，请勿为提速而放大，以免对站点造成压力或触发封禁。
- Python 侧不执行任何远程代码；但打开本地 HTML 会执行站点原有 JavaScript。
  阅读不可信站点时，建议断网，或使用独立的浏览器配置文件。
- 文件名经白名单过滤，不存在路径穿越；TLS 证书校验保持开启。

================================================================================
