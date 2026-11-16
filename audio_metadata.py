"""下载后音频元数据兜底校验与补全。

为什么要自己再做一遍：
1. musicdl 的 ``SongInfoUtils.safeeditaudio`` 在写完标签后会做一次“读回校验”，
   只要这次读回失败（文件被杀毒软件 / 资源管理器 / 播放器短暂占用），它就会用
   备份把文件覆盖回去。于是整首歌的标签被静默回退，下载却仍算成功——这就是
   “偶尔有歌曲没有封面 / 未知艺术家 / 标题是文件名”的直接原因。
2. 它按扩展名猜容器：CDN 返回的 FLAC 被存成 ``.mp3`` 时，mutagen 会按 MP3 解析
   并抛 ``HeaderNotFoundError``，所有标签一个都写不进去。

本模块的做法：
1. 读文件头识别真实容器（不信任扩展名），必要时把扩展名改正；
2. 只补缺失的核心字段（标题 / 艺术家 / 专辑 / 歌词 / 封面），已有内容不动；
3. 改标签在临时副本上完成，校验通过后才原子替换，绝不回退已经成功的结果；
4. 遇到占用（PermissionError）时退避重试；重试后仍缺失的字段返回给界面显示，
   而不是像 musicdl 那样只打一行看不见的 warning。
"""

from __future__ import annotations

import base64
import errno
import os
import re
import shutil
import struct
import tempfile
import time

import requests
from mutagen import File as MutagenFile
from mutagen.aiff import AIFF
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, TPOS, TRCK, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE


# 音频容器 -> 规范扩展名
CONTAINER_EXT = {
    'flac': 'flac', 'mp3': 'mp3', 'mp4': 'm4a', 'ogg': 'ogg', 'opus': 'opus',
    'wav': 'wav', 'aiff': 'aiff', 'asf': 'wma', 'ape': 'ape', 'wv': 'wv',
    'dsf': 'dsf', 'dff': 'dff', 'amr': 'amr',
}

# 扩展名 -> 容器族（同一族内的扩展名互换不认为是错配，例如 .m4a/.mp4、.ogg/.opus）
EXT_FAMILY = {
    'flac': 'flac',
    'mp3': 'mp3', 'mp2': 'mp3', 'mpa': 'mp3',
    'm4a': 'mp4', 'mp4': 'mp4', 'm4b': 'mp4', 'alac': 'mp4',
    'ogg': 'ogg', 'oga': 'ogg', 'opus': 'ogg',
    'wav': 'wav', 'wave': 'wav',
    'aiff': 'aiff', 'aif': 'aiff', 'aifc': 'aiff',
    'wma': 'asf', 'asf': 'asf',
    'ape': 'ape', 'wv': 'wv',
    'dsf': 'dsf', 'dff': 'dff',
    'amr': 'amr',
}

# 容器 -> 负责解析的 mutagen 类
LOADERS = {
    'flac': FLAC,
    'mp3': MP3,
    'mp4': MP4,
    'ogg': OggVorbis,
    'opus': OggOpus,
    'wav': WAVE,
    'aiff': AIFF,
    'asf': ASF,
}

AUDIO_FIELDS = ('title', 'artist', 'album', 'tracknumber', 'lyrics', 'cover')
FIELD_LABELS = {
    'title': '标题', 'artist': '艺术家', 'album': '专辑', 'tracknumber': '音轨号',
    'lyrics': '歌词', 'cover': '封面',
}
COVER_MAX_BYTES = 20 * 1024 * 1024


# ==================== 容器识别 ====================
def sniff_container(path):
    """只读文件头判断真实音频容器，识别不了返回 None。"""
    try:
        with open(path, 'rb') as fp:
            head = fp.read(16)
            size = os.fstat(fp.fileno()).st_size
            if size <= 0:
                return None
            if head[:4] == b'OggS':
                fp.seek(0)
                chunk = fp.read(128)
                return 'opus' if b'OpusHead' in chunk else 'ogg'
    except OSError:
        return None
    if len(head) < 4:
        return None
    if head[:4] == b'fLaC':
        return 'flac'
    if head[:3] == b'ID3':
        return 'mp3'
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return 'mp3'
    if head[4:8] == b'ftyp':
        return 'mp4'
    if head[:4] == b'RIFF' and head[8:12] == b'WAVE':
        return 'wav'
    if head[:4] == b'FORM' and head[8:12] in (b'AIFF', b'AIFC'):
        return 'aiff'
    if head[:4] == b'MAC ':
        return 'ape'
    if head[:4] == b'wvpk':
        return 'wv'
    if head[:4] == b'DSD ':
        return 'dsf'
    if head[:4] == b'FRM8':
        return 'dff'
    if head[:4] == b'0&\xb2u':
        return 'asf'
    if head[:5] == b'#!AMR':
        return 'amr'
    return None


def extension_mismatch(path, container=None):
    """扩展名与真实容器是否不符。"""
    container = container or sniff_container(path)
    if not container:
        return False
    ext = os.path.splitext(path)[1].lower().lstrip('.')
    current = EXT_FAMILY.get(ext)
    return bool(current) and current != EXT_FAMILY.get(CONTAINER_EXT.get(container, ''))


def _load(path, container):
    loader = LOADERS.get(container)
    if loader is None:
        return None
    return loader(path)


# ==================== 标签读取 ====================
def _as_text(value):
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return ', '.join(str(item) for item in value if item not in (None, ''))
    return str(value)


def _vorbis_value(tags, *keys):
    if tags is None:
        return ''
    for key in keys:
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if value:
            return _as_text(value)
    return ''


def _read_fields(audio, container):
    """读出当前已有的核心字段（缺失返回空字符串 / False）。"""
    state = {'title': '', 'artist': '', 'album': '', 'tracknumber': '', 'lyrics': '', 'cover': False}
    if container == 'flac':
        tags = audio.tags
        state['title'] = _vorbis_value(tags, 'TITLE')
        state['artist'] = _vorbis_value(tags, 'ARTIST')
        state['album'] = _vorbis_value(tags, 'ALBUM')
        state['tracknumber'] = _vorbis_value(tags, 'TRACKNUMBER')
        state['lyrics'] = _vorbis_value(tags, 'LYRICS', 'UNSYNCEDLYRICS')
        state['cover'] = bool(getattr(audio, 'pictures', None))
    elif container in ('ogg', 'opus'):
        tags = audio.tags
        state['title'] = _vorbis_value(tags, 'TITLE')
        state['artist'] = _vorbis_value(tags, 'ARTIST')
        state['album'] = _vorbis_value(tags, 'ALBUM')
        state['tracknumber'] = _vorbis_value(tags, 'TRACKNUMBER')
        state['lyrics'] = _vorbis_value(tags, 'LYRICS', 'UNSYNCEDLYRICS')
        state['cover'] = bool(_vorbis_value(tags, 'METADATA_BLOCK_PICTURE', 'COVERART'))
    elif container == 'mp3':
        tags = audio.tags
        state['title'] = _as_text(tags.getall('TIT2')) if tags else ''
        state['artist'] = _as_text(tags.getall('TPE1')) if tags else ''
        state['album'] = _as_text(tags.getall('TALB')) if tags else ''
        state['tracknumber'] = _as_text(tags.getall('TRCK')) if tags else ''
        state['lyrics'] = _as_text(tags.getall('USLT')) if tags else ''
        state['cover'] = bool(tags and tags.getall('APIC'))
    elif container == 'mp4':
        tags = audio.tags
        if tags:
            state['title'] = _as_text(tags.get('\xa9nam'))
            state['artist'] = _as_text(tags.get('\xa9ART'))
            state['album'] = _as_text(tags.get('\xa9alb'))
            trkn = tags.get('trkn')
            state['tracknumber'] = str(trkn[0][0]) if trkn else ''
            state['lyrics'] = _as_text(tags.get('\xa9lyr'))
            state['cover'] = bool(tags.get('covr'))
    elif container in ('wav', 'aiff'):
        tags = audio.tags if isinstance(audio.tags, ID3) else None
        state['title'] = _as_text(tags.getall('TIT2')) if tags else ''
        state['artist'] = _as_text(tags.getall('TPE1')) if tags else ''
        state['album'] = _as_text(tags.getall('TALB')) if tags else ''
        state['tracknumber'] = _as_text(tags.getall('TRCK')) if tags else ''
        state['lyrics'] = _as_text(tags.getall('USLT')) if tags else ''
        state['cover'] = bool(tags and tags.getall('APIC'))
    elif container == 'asf':
        tags = audio.tags
        if tags:
            state['title'] = _as_text(tags.get('Title'))
            state['artist'] = _as_text(tags.get('Author'))
            state['album'] = _as_text(tags.get('WM/AlbumTitle'))
            state['tracknumber'] = _as_text(tags.get('WM/TrackNumber'))
            state['lyrics'] = _as_text(tags.get('WM/Lyrics'))
            state['cover'] = bool(tags.get('WM/Picture'))
    return state


def inspect_audio(path):
    """检查一个音频文件，返回 {path, kind, title, artist, album, lyrics, cover, error}。"""
    container = sniff_container(path)
    info = {
        'path': path, 'kind': container, 'title': '', 'artist': '', 'album': '',
        'lyrics': '', 'cover': False, 'error': None,
    }
    if not container:
        info['error'] = '无法识别的音频容器'
        return info
    try:
        audio = _load(path, container)
    except Exception as err:  # HeaderNotFoundError / EOFError / ...
        info['error'] = f'{type(err).__name__}: {err}'
        return info
    if audio is None:
        info['error'] = 'mutagen 无法解析该文件'
        return info
    try:
        info.update(_read_fields(audio, container))
    except Exception as err:
        info['error'] = f'{type(err).__name__}: {err}'
    return info


# ==================== 标签写入 ====================
def _write_flac(audio, desired, fields, cover):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if 'title' in fields:
        tags['TITLE'] = [desired['title']]
    if 'artist' in fields:
        tags['ARTIST'] = [desired['artist']]
    if 'album' in fields:
        tags['ALBUM'] = [desired['album']]
    if 'tracknumber' in fields:
        tags['TRACKNUMBER'] = [str(int(desired['tracknumber']))]
        if desired.get('discnumber'):
            tags['DISCNUMBER'] = [str(int(desired['discnumber']))]
    if 'lyrics' in fields:
        tags['LYRICS'] = [desired['lyrics']]
    if 'cover' in fields and cover:
        data, mime = cover
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = 'Cover'
        picture.data = data
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def _write_vorbis(audio, desired, fields, cover):
    """OGG Vorbis / Opus 用 Vorbis comment。"""
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if 'title' in fields:
        tags['TITLE'] = [desired['title']]
    if 'artist' in fields:
        tags['ARTIST'] = [desired['artist']]
    if 'album' in fields:
        tags['ALBUM'] = [desired['album']]
    if 'tracknumber' in fields:
        tags['TRACKNUMBER'] = [str(int(desired['tracknumber']))]
        if desired.get('discnumber'):
            tags['DISCNUMBER'] = [str(int(desired['discnumber']))]
    if 'lyrics' in fields:
        tags['LYRICS'] = [desired['lyrics']]
    if 'cover' in fields and cover:
        data, mime = cover
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = 'Cover'
        picture.data = data
        tags['METADATA_BLOCK_PICTURE'] = [base64.b64encode(picture.write()).decode('ascii')]
    audio.save()


def _write_mp3(audio, desired, fields, cover):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if 'title' in fields:
        tags.setall('TIT2', [TIT2(encoding=3, text=[desired['title']])])
    if 'artist' in fields:
        tags.setall('TPE1', [TPE1(encoding=3, text=[desired['artist']])])
    if 'album' in fields:
        tags.setall('TALB', [TALB(encoding=3, text=[desired['album']])])
    if 'tracknumber' in fields:
        tags.setall('TRCK', [TRCK(encoding=3, text=[str(int(desired['tracknumber']))])])
        if desired.get('discnumber'):
            tags.setall('TPOS', [TPOS(encoding=3, text=[str(int(desired['discnumber']))])])
    if 'lyrics' in fields:
        tags.setall('USLT', [USLT(encoding=3, lang='eng', desc='Lyrics', text=desired['lyrics'])])
    if 'cover' in fields and cover:
        data, mime = cover
        tags.setall('APIC', [APIC(encoding=3, mime=mime, type=3, desc='Cover', data=data)])
    audio.save(v2_version=3)


def _write_mp4(audio, desired, fields, cover):
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if 'title' in fields:
        tags['\xa9nam'] = [desired['title']]
    if 'artist' in fields:
        tags['\xa9ART'] = [desired['artist']]
    if 'album' in fields:
        tags['\xa9alb'] = [desired['album']]
    if 'tracknumber' in fields:
        tags['trkn'] = [(int(desired['tracknumber']), 0)]
        if desired.get('discnumber'):
            tags['disk'] = [(int(desired['discnumber']), 0)]
    if 'lyrics' in fields:
        tags['\xa9lyr'] = [desired['lyrics']]
    if 'cover' in fields and cover:
        data, mime = cover
        if mime not in ('image/jpeg', 'image/png'):
            raise RuntimeError('M4A 封面只支持 JPEG/PNG')
        image_format = MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG
        tags['covr'] = [MP4Cover(data, imageformat=image_format)]
    audio.save()


def _write_id3_container(audio, desired, fields, cover):
    """WAV / AIFF：标签是容器里的 ID3 chunk。"""
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if 'title' in fields:
        tags.setall('TIT2', [TIT2(encoding=3, text=[desired['title']])])
    if 'artist' in fields:
        tags.setall('TPE1', [TPE1(encoding=3, text=[desired['artist']])])
    if 'album' in fields:
        tags.setall('TALB', [TALB(encoding=3, text=[desired['album']])])
    if 'tracknumber' in fields:
        tags.setall('TRCK', [TRCK(encoding=3, text=[str(int(desired['tracknumber']))])])
        if desired.get('discnumber'):
            tags.setall('TPOS', [TPOS(encoding=3, text=[str(int(desired['discnumber']))])])
    if 'lyrics' in fields:
        tags.setall('USLT', [USLT(encoding=3, lang='eng', desc='Lyrics', text=desired['lyrics'])])
    if 'cover' in fields and cover:
        data, mime = cover
        tags.setall('APIC', [APIC(encoding=3, mime=mime, type=3, desc='Cover', data=data)])
    audio.save()


def _write_asf(audio, desired, fields, cover):
    tags = audio.tags
    if 'title' in fields:
        tags['Title'] = [desired['title']]
    if 'artist' in fields:
        tags['Author'] = [desired['artist']]
    if 'album' in fields:
        tags['WM/AlbumTitle'] = [desired['album']]
    if 'tracknumber' in fields:
        tags['WM/TrackNumber'] = [str(int(desired['tracknumber']))]
    if 'lyrics' in fields:
        tags['WM/Lyrics'] = [desired['lyrics']]
    if 'cover' in fields and cover:
        data, mime = cover
        packed = struct.pack('<BI', 3, len(data)) + mime.encode('utf-16le') + b'\x00\x00' \
            + 'Cover'.encode('utf-16le') + b'\x00\x00' + data
        tags['WM/Picture'] = [ASFByteArrayAttribute(packed)]
    audio.save()


def _write_fields(path, container, desired, fields, cover):
    audio = _load(path, container)
    if audio is None:
        raise RuntimeError(f'暂不支持写入 {container} 格式')
    if container == 'flac':
        _write_flac(audio, desired, fields, cover)
    elif container in ('ogg', 'opus'):
        _write_vorbis(audio, desired, fields, cover)
    elif container == 'mp3':
        _write_mp3(audio, desired, fields, cover)
    elif container == 'mp4':
        _write_mp4(audio, desired, fields, cover)
    elif container in ('wav', 'aiff'):
        _write_id3_container(audio, desired, fields, cover)
    elif container == 'asf':
        _write_asf(audio, desired, fields, cover)
    else:
        raise RuntimeError(f'暂不支持写入 {container} 格式')


# ==================== 封面下载 ====================
def _sniff_image_mime(data, content_type=''):
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    mime = (content_type or '').split(';')[0].strip().lower()
    if mime.startswith('image/'):
        return 'image/jpeg' if mime in ('image/jpg', 'image/pjpeg') else mime
    return None


def fetch_cover(url, timeout=20, retries=3):
    """下载封面，返回 (bytes, mime)；失败返回 None。"""
    url = (url or '').strip()
    if not url.startswith('http'):
        return None
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://music.163.com/'}
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers, allow_redirects=True)
            resp.raise_for_status()
            data = resp.content or b''
            if not data or len(data) > COVER_MAX_BYTES:
                raise ValueError('封面内容为空或过大')
            mime = _sniff_image_mime(data, resp.headers.get('Content-Type', ''))
            if not mime:
                raise ValueError('封面不是图片')
            return data, mime
        except Exception:
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))
    return None


# ==================== 文件操作 ====================
def temp_dir_for(path):
    """尽量把临时文件放到“已下载音乐”之外的同盘暂存目录，避免被 foobar2000 扫进媒体库。"""
    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path) or '.'
    cur = parent
    for _ in range(6):
        if os.path.basename(cur) == '已下载音乐':
            root_parent = os.path.dirname(cur)
            if root_parent and root_parent != cur:
                staging = os.path.join(root_parent, '.musicdl_staging')
                try:
                    os.makedirs(staging, exist_ok=True)
                    return staging
                except OSError:
                    pass
            break
        nxt = os.path.dirname(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt
    return parent


def bump_mtime(path, prev_mtime=None):
    """更新文件修改时间并避开 exFAT/FAT 2 秒精度碰撞，确保外部媒体库能察觉变更。"""
    try:
        os.utime(path, None)
        if prev_mtime is not None:
            cur_mtime = os.stat(path).st_mtime
            if abs(cur_mtime - prev_mtime) < 1.5:
                target = max(time.time(), float(prev_mtime)) + 2.5
                os.utime(path, (target, target))
    except OSError:
        pass


def _copy_to_temp(path):
    stem, ext = os.path.splitext(os.path.basename(path))
    # 临时文件放在非媒体库目录且使用非音频扩展名：foobar 等媒体库如果配置了 * 通配，
    # 同目录下的 .musictmp 同样会被当作音频扫入库并留下死条目。
    fd, temp_name = tempfile.mkstemp(prefix=stem + '.', suffix='.musictmp', dir=temp_dir_for(path))
    os.close(fd)
    shutil.copy2(path, temp_name)
    return temp_name


def find_lockers(path):
    """用 Windows Restart Manager 查出正在占用文件的进程，返回 [(pid, 程序名)]。"""
    if os.name != 'nt':
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [('dwLowDateTime', wintypes.DWORD), ('dwHighDateTime', wintypes.DWORD)]

        class RM_UNIQUE_PROCESS(ctypes.Structure):
            _fields_ = [('dwProcessId', wintypes.DWORD), ('ProcessStartTime', FILETIME)]

        class RM_PROCESS_INFO(ctypes.Structure):
            _fields_ = [
                ('Process', RM_UNIQUE_PROCESS),
                ('strAppName', wintypes.WCHAR * 256),
                ('strServiceShortName', wintypes.WCHAR * 64),
                ('ApplicationType', wintypes.DWORD),
                ('AppStatus', wintypes.ULONG),
                ('TSSessionId', wintypes.DWORD),
                ('bRestartable', wintypes.BOOL),
            ]

        rm = ctypes.WinDLL('Rstrtmgr')
        session = wintypes.DWORD()
        key = ctypes.create_unicode_buffer(33)
        if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
            return []
        try:
            files = (wintypes.LPCWSTR * 1)(os.path.abspath(path))
            if rm.RmRegisterResources(session, 1, files, 0, None, 0, None) != 0:
                return []
            needed = wintypes.UINT(0)
            count = wintypes.UINT(0)
            reasons = wintypes.DWORD()
            rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None,
                              ctypes.byref(reasons))
            if rc != 234 or needed.value == 0:  # ERROR_MORE_DATA 才说明有进程占用
                return []
            infos = (RM_PROCESS_INFO * needed.value)()
            count = wintypes.UINT(needed.value)
            rc = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), infos,
                              ctypes.byref(reasons))
            if rc != 0:
                return []
            own = os.getpid()
            return [(int(infos[i].Process.dwProcessId), str(infos[i].strAppName))
                    for i in range(count.value) if int(infos[i].Process.dwProcessId) != own]
        finally:
            rm.RmEndSession(session)
    except Exception:
        return []


def describe_lockers(path):
    """把占用进程拼成一句话，例如 "foobar2000.exe (PID 15028)"；无占用返回空串。"""
    labels = []
    for pid, name in find_lockers(path):
        label = f'{name} (PID {pid})' if name else f'PID {pid}'
        if label not in labels:
            labels.append(label)
    return '、'.join(labels)


def _replace_with_retry(src, dst, retries=4, delay=0.4):
    last_error = None
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as err:
            last_error = err
        except OSError as err:
            if getattr(err, 'errno', None) != errno.EXDEV:
                raise
            try:
                shutil.copy2(src, dst)
                try:
                    os.remove(src)
                except OSError:
                    pass
                return
            except PermissionError as perr:
                last_error = perr
        if attempt + 1 < retries:
            time.sleep(delay * (attempt + 1))
    raise last_error


def _fix_extension(path, container):
    """扩展名与真实容器不符时改名（同目录原子替换）。"""
    if not extension_mismatch(path, container):
        return path
    base, ext = os.path.splitext(path)
    target_ext = '.' + CONTAINER_EXT[container]
    target = base + target_ext
    index = 1
    while os.path.exists(target):
        target = f'{base} ({index}){target_ext}'
        index += 1
    _replace_with_retry(path, target)
    return target


# ==================== 主流程 ====================
def _dedupe(items):
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _missing_fields(state, desired, skip=()):
    missing = []
    for field in AUDIO_FIELDS:
        if field in skip:
            continue
        if field == 'cover':
            if not state['cover']:
                missing.append('cover')
        elif desired.get(field):
            if not state.get(field):
                missing.append(field)
    return missing


def repair_audio(path, desired, attempts=4, retry_delay=0.6, cover_timeout=20):
    """校验并补全一个音频文件的元数据。

    desired 支持 title / artist / album / lyrics / cover_url（空值表示不要求）。
    返回 (最终路径, 修复说明列表, 仍未解决说明列表)。
    """
    notes = []
    warnings = []
    desired = {key: (desired.get(key) or '') for key in AUDIO_FIELDS + ('cover_url', 'discnumber')}

    if not os.path.exists(path):
        return path, notes, ['文件不存在']
    container = sniff_container(path)
    if not container:
        return path, notes, ['无法识别的音频容器']
    try:
        renamed = _fix_extension(path, container)
    except PermissionError:
        return path, notes, ['文件被占用，无法修正扩展名']
    except Exception as err:
        return path, notes, [f'修正扩展名失败：{err}']
    if renamed != path:
        notes.append(f'扩展名 {os.path.splitext(path)[1] or "?"} → {os.path.splitext(renamed)[1]}')
        path, container = renamed, sniff_container(renamed) or container

    cover = None
    cover_fetched = False
    unfixable = set()
    last_issue = None
    for attempt in range(attempts):
        state = inspect_audio(path)
        if state['error']:
            last_issue = state['error']
            time.sleep(retry_delay * (2 ** attempt))
            continue
        container = state['kind'] or container
        missing = _missing_fields(state, desired, skip=unfixable)
        if not missing:
            return path, notes, _dedupe(warnings)

        write_fields = list(missing)
        if 'cover' in write_fields:
            if not desired.get('cover_url'):
                unfixable.add('cover')
                warnings.append('缺少封面且没有封面地址')
                write_fields.remove('cover')
            elif not cover_fetched:
                cover = fetch_cover(desired['cover_url'], timeout=cover_timeout)
                cover_fetched = True
                if cover is None:
                    last_issue = '封面下载失败'
                    write_fields.remove('cover')
                    cover_fetched = False  # 下一轮再试一次

        if not write_fields:
            if not _missing_fields(state, desired, skip=unfixable):
                return path, notes, _dedupe(warnings)
            time.sleep(retry_delay * (2 ** attempt))
            continue

        temp_path = None
        try:
            try:
                prev_mtime = os.stat(path).st_mtime
            except OSError:
                prev_mtime = None
            temp_path = _copy_to_temp(path)
            _write_fields(temp_path, container, desired, write_fields, cover)
            check = inspect_audio(temp_path)
            still_missing = _missing_fields(check, desired, skip=unfixable)
            if still_missing:
                last_issue = '写入后仍缺少 ' + '、'.join(FIELD_LABELS[f] for f in still_missing)
            else:
                _replace_with_retry(temp_path, path)
                temp_path = None
                bump_mtime(path, prev_mtime)
                verified = inspect_audio(path)
                if not _missing_fields(verified, desired, skip=unfixable):
                    labels = '、'.join(FIELD_LABELS[f] for f in write_fields)
                    notes.append(f'补全 {labels}')
                    return path, notes, _dedupe(warnings)
                last_issue = '替换后校验仍缺少字段'
        except PermissionError:
            holders = describe_lockers(path)
            last_issue = '文件被占用' + (f'：{holders}' if holders else '（可能正在被杀软/播放器读取）')
        except Exception as err:
            last_issue = f'{type(err).__name__}: {err}'
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        time.sleep(retry_delay * (2 ** attempt))

    issues = list(warnings)
    holders = describe_lockers(path)
    if holders and not any('占用' in item for item in issues):
        issues.append(f'文件仍被占用：{holders}')
    state = inspect_audio(path)
    if not state['error']:
        still = _missing_fields(state, desired, skip=unfixable)
        if still:
            issues.append('最终仍缺少 ' + '、'.join(FIELD_LABELS[f] for f in still))
    if last_issue and last_issue not in issues:
        issues.append(last_issue)
    return path, notes, _dedupe(issues)
