"""Receive QQ images as private Linux files for Codex localImage inputs."""
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from qq_files import DOWNLOADS, save_attachment

MAX_IMAGE_BYTES = 20 * 1024 * 1024


def check_image_url(url):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
            or parsed.port not in (None, 80, 443)
            or not any(host == domain or host.endswith('.' + domain)
                       for domain in ('qpic.cn', 'qq.com'))):
        raise ValueError('图片下载地址不是受支持的 QQ 图片地址')


class QQImageRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def image_suffix(path):
    with path.open('rb') as src:
        header = src.read(16)
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if header.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if header[:6] in (b'GIF87a', b'GIF89a'):
        return '.gif'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return '.webp'
    raise ValueError('图片格式无法识别，请发送 PNG、JPEG、WebP 或 GIF 图片')


def save_image(local_file, url, directory=DOWNLOADS):
    """Prefer NapCat's local cache; otherwise download its QQ CDN URL."""
    saved = None
    try:
        if isinstance(local_file, str) and Path(local_file).is_absolute():
            try:
                if Path(local_file).stat().st_size > MAX_IMAGE_BYTES:
                    raise ValueError('单张图片超过 20 MiB 上限')
                saved = save_attachment(local_file, 'image', directory)
            except OSError:
                # Cache paths inside a container may be unavailable to the bridge.
                pass
        if saved is None:
            if not isinstance(url, str) or not url:
                raise ValueError('未取得可读取的图片，请重新发送原图')
            check_image_url(url)
            with tempfile.TemporaryDirectory(prefix='qq-image-') as temporary:
                source = Path(temporary) / 'image'
                opener = build_opener(QQImageRedirect())
                request = Request(url, headers={'User-Agent': 'QQ-Bridge/2.2'})
                with opener.open(request, timeout=30) as response, source.open('xb') as out:
                    length = response.headers.get('Content-Length')
                    if length and int(length) > MAX_IMAGE_BYTES:
                        raise ValueError('单张图片超过 20 MiB 上限')
                    total = 0
                    while chunk := response.read(64 * 1024):
                        total += len(chunk)
                        if total > MAX_IMAGE_BYTES:
                            raise ValueError('单张图片超过 20 MiB 上限')
                        out.write(chunk)
                saved = save_attachment(source, 'image', directory)
        if saved.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError('单张图片超过 20 MiB 上限')
        target = saved.with_suffix(image_suffix(saved))
        saved.rename(target)
        return target
    except BaseException:
        if saved is not None:
            saved.unlink(missing_ok=True)
            saved.parent.rmdir()
        raise
