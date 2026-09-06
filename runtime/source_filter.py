#!/usr/bin/env python3
"""Keep upstream highlighting; render LFS pointers as useful file cards."""

import html
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote, urlencode

from common import RequestError, config, object_path, pointer, repository


def main():
    data = sys.stdin.buffer.read()
    filename = sys.argv[1] if len(sys.argv) > 1 else "file"
    info = pointer(data)
    if not info:
        subprocess.run(["/usr/local/lib/cgit/filters/syntax-highlighting.py", filename],
                       input=data, check=True)
        return
    oid, size = info
    cfg = config()
    try:
        _, name = repository(cfg, os.environ["CGIT_REPO_URL"])
        path = object_path(cfg, name, oid)
        available = path.is_file() and path.stat().st_size == size
    except (RequestError, OSError):
        available = False
        name = os.environ["CGIT_REPO_URL"]
    url = "/" + quote(name, safe="/") + "/info/lfs/objects/" + oid
    download = url + "?" + urlencode({"filename": filename})
    preview = download + "&inline=1"
    # cgit v1.3.1 wraps source-filter output in <pre><code>. A file card is
    # block content; close that wrapper and restore it for cgit's closing tags.
    print('</code></pre><div class="lfs-file">')
    print(f'<strong>Git LFS · {html.escape(filename)}</strong>')
    print(f'<p>{size:,} bytes · SHA-256 <code>{oid}</code></p>')
    if available:
        print(f'<p><a href="{html.escape(download)}">Download original / 下载原文件</a></p>')
        if Path(filename).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp") and size <= 10 * 1024**2:
            print(f'<img loading="lazy" src="{html.escape(preview)}" alt="{html.escape(filename)}">')
        elif Path(filename).suffix.lower() == ".pdf":
            print(f'<p><a href="{html.escape(preview)}" target="_blank" rel="noopener">Open PDF / 打开 PDF</a></p>')
    else:
        print('<p role="status">LFS object missing or size mismatch / 对象缺失或大小不符，请先上传或迁入。</p>')
    print('<p class="lfs-note">ZIP / tar.gz snapshots contain the Git pointer, not this original file.<br>'
          '仓库归档包含 LFS 指针；需要完整文件请使用 git clone。</p></div><pre><code>')


if __name__ == "__main__":
    main()
