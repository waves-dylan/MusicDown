# -*- coding: utf-8 -*-
"""
bilingual_lyric.py: 双语歌词解析、时序对齐与增强模块
Author: Antigravity
Description:
    1. 提供精准的 LRC 时间戳解析与格式化；
    2. 基于双指针与毫秒级容差窗口，将外语歌曲的原文歌词与中文译文合并；
    3. 支持三种主流排版：双行同时间戳 (dual_line)、单行行内拼接 (inline)、纯中文翻译 (translation_only)；
    4. 智能判断歌曲是否为外语歌、是否已包含双语，杜绝误处理；
    5. 支持从网易云（及 QQ 音乐）跨源穿透补全双语歌词。
"""

from __future__ import annotations
import re
import json
import requests
from typing import Optional, Tuple, List, Dict, Any

try:
    import zh_converter
    _ZH_CONVERTER_AVAILABLE = True
except ImportError:
    zh_converter = None
    _ZH_CONVERTER_AVAILABLE = False

# 正则：匹配 LRC 时间戳 [mm:ss.xx] 或 [mm:ss.xxx] 或 [mm:ss]
TIME_RE = re.compile(r'\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]')

# 正则：元数据标签 [ti:...], [ar:...], [al:...], [by:...], [offset:...]
HEADER_TAG_RE = re.compile(r'^\[(ti|ar|al|by|offset|kana|re|ve):.*\]$', re.IGNORECASE)

# 正则：歌曲制作信息行（如 作词、作曲、编曲、制作人等）
META_LINE_RE = re.compile(
    r'^(作词|作曲|编曲|制作|监制|和声|混音|录音|吉他|贝斯|鼓|键盘|企划|统筹|出品|发行|OP|SP|'
    r'Lyricist|Composer|Arranger|Producer|Guitar|Bass|Drums|Keyboards|Mixing|Mastering)\s*[:：]',
    re.IGNORECASE
)

# 中文字符正则（覆盖 CJK 统一表意文字基本区，同时包含简体与繁体字）
CHINESE_CHAR_RE = re.compile(r'[\u4e00-\u9fa5]')

# 日文假名正则（平假名 \u3040-\u309f，片假名 \u30a0-\u30ff）
JAPANESE_KANA_RE = re.compile(r'[\u3040-\u309f\u30a0-\u30ff]')

# 韩文谚文正则（\uac00-\ud7af, \u1100-\u11ff, \u3130-\u318f）
KOREAN_HANGUL_RE = re.compile(r'[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]')


def clean_text(text: str) -> str:
    """清理不可见字符与多余首尾空白。"""
    if not text:
        return ''
    return text.strip('\ufeff\u200b\u200c\u200d\u2060\u00a0 \t\r\n')


def parse_lrc(lrc_text: str) -> List[Dict[str, Any]]:
    """
    将 LRC 歌词解析为结构化列表：
    [
        {'ms': 5930, 'tag': '[00:05.93]', 'text': 'Fever dream high...', 'raw': '...', 'is_header': False},
        ...
    ]
    支持单行多时间戳展开并按时间排序。
    """
    if not lrc_text:
        return []
    
    entries = []
    lines = lrc_text.splitlines()
    for raw in lines:
        line = raw.strip('\ufeff\u200b\u200c\u200d\u2060\u00a0 \t\r')
        if not line:
            continue
        
        # 1. 头部元数据标签
        if HEADER_TAG_RE.match(line):
            entries.append({'ms': -1, 'tag': line, 'text': '', 'raw': line, 'is_header': True})
            continue

        # 2. 匹配时间戳
        matches = list(TIME_RE.finditer(line))
        if not matches:
            # 无时间戳行（纯文本说明或注释）
            entries.append({'ms': -1, 'tag': '', 'text': line, 'raw': line, 'is_header': False})
            continue
        
        # 文本内容为最后一个时间戳之后的部分
        text = clean_text(line[matches[-1].end():])
        for m in matches:
            mm = int(m.group(1))
            ss = int(m.group(2))
            frac_str = m.group(3) or '0'
            # 补齐至 3 位毫秒数
            ms = mm * 60000 + ss * 1000 + int(frac_str.ljust(3, '0')[:3])
            entries.append({
                'ms': ms,
                'tag': m.group(0),
                'text': text,
                'raw': line,
                'is_header': False
            })

    # 按时间升序排序（保证多时间戳行展开后时序正确）
    entries.sort(key=lambda x: (x['ms'] if x['ms'] >= 0 else -1))
    return entries


def format_timestamp(ms: int) -> str:
    """将毫秒数格式化为标准 LRC 时间戳 [mm:ss.xx]。"""
    if ms < 0:
        return '[00:00.00]'
    mm = ms // 60000
    ss = (ms % 60000) // 1000
    cs = (ms % 1000) // 10  # 百分秒
    return f'[{mm:02d}:{ss:02d}.{cs:02d}]'


def has_chinese(text: str) -> bool:
    """判断文本中是否包含中文字符。"""
    return bool(CHINESE_CHAR_RE.search(text or ''))


def is_foreign_lyrics(lrc_text: str) -> bool:
    """
    判断歌词是否属于外语歌曲：
    1. 包含日文假名或韩文谚文行，判定为外语歌（日韩歌曲中常包含汉字，优先通过假名/谚文精准判定）；
    2. 排除开头的作词、作曲、编曲等署名信息后，如果正文歌词中包含汉字（含简体/繁体）的行数占比低于 20%，判定为外语歌（如英语、法语、西班牙语等）；
    3. 纯繁体中文歌曲（如港台华语、粤语歌）由于只含汉字而不含假名/韩文，且汉字占比极高，会被精准判定为中文歌（不误加翻译）。
    """
    if not lrc_text:
        return False
    
    entries = parse_lrc(lrc_text)
    lyrics_lines = []
    for item in entries:
        if item['is_header'] or not item['text']:
            continue
        # 排除制作人员署名行
        if META_LINE_RE.search(item['text']):
            continue
        lyrics_lines.append(item['text'])

    if not lyrics_lines:
        return False

    total = len(lyrics_lines)

    # 1. 检测日文假名（超过 12% 行含有假名即确认为日文歌）
    kana_count = sum(1 for line in lyrics_lines if JAPANESE_KANA_RE.search(line))
    if (kana_count / total) >= 0.12:
        return True

    # 2. 检测韩文谚文（超过 12% 行含有谚文即确认为韩文歌）
    hangul_count = sum(1 for line in lyrics_lines if KOREAN_HANGUL_RE.search(line))
    if (hangul_count / total) >= 0.12:
        return True

    # 3. 检测汉字（简体 + 繁体均在 \u4e00-\u9fa5 范围中）
    chinese_count = sum(1 for line in lyrics_lines if has_chinese(line))
    return (chinese_count / total) < 0.20


def already_bilingual(lrc_text: str) -> bool:
    """
    检测歌词是否已经是双语歌词（避免重复合并）：
    如果存在多处相同时间戳且一行是中文、一行是非中文，或者包含 ' ｜ ' / ' / ' 分隔符并带有中文，则判定已为双语。
    """
    if not lrc_text:
        return False
    
    # 检查内联分隔符特征
    if ' ｜ ' in lrc_text or ' / ' in lrc_text:
        for line in lrc_text.splitlines():
            if (' ｜ ' in line or ' / ' in line) and has_chinese(line):
                return True

    entries = parse_lrc(lrc_text)
    seen = {}  # 粗略时间秒数 -> 是否有中文
    bilingual_pairs = 0
    for item in entries:
        if item['is_header'] or not item['text'] or item['ms'] < 0:
            continue
        if META_LINE_RE.search(item['text']):
            continue
        sec_bucket = item['ms'] // 1000
        ch = has_chinese(item['text'])
        if sec_bucket in seen:
            prev_ch = seen[sec_bucket]
            if prev_ch != ch:
                bilingual_pairs += 1
                if bilingual_pairs >= 3:
                    return True
        else:
            seen[sec_bucket] = ch

    return False


def merge_bilingual_lrc(
    orig_lrc: str,
    trans_lrc: str,
    layout: str = 'dual_line',
    tolerance_ms: int = 500
) -> str:
    """
    智能将原文歌词与翻译歌词按时间戳对齐合并。
    
    参数:
        orig_lrc: 原文歌词文本
        trans_lrc: 中文译文歌词文本
        layout: 格式排版方式
            - 'dual_line': 同时间戳双行（主流播放器推荐）
            - 'inline': 单行斜杠/分隔符拼接（车机系统推荐）
            - 'translation_only': 仅保留中文翻译
        tolerance_ms: 时间对齐容差（毫秒），默认 500ms
        
    返回:
        合并后的 LRC 字符串
    """
    if not trans_lrc or not trans_lrc.strip():
        return orig_lrc
    if not orig_lrc or not orig_lrc.strip():
        return trans_lrc

    orig_items = parse_lrc(orig_lrc)
    trans_items = parse_lrc(trans_lrc)

    if not orig_items:
        return trans_lrc
    if not trans_items:
        return orig_lrc

    # 提取有效的翻译正文行
    trans_lyrics = [
        item for item in trans_items
        if not item['is_header'] and item['text'] and item['ms'] >= 0
    ]
    p_trans = 0
    n_trans = len(trans_lyrics)

    output = []
    for item in orig_items:
        if item['is_header']:
            output.append(item['raw'])
            continue
        
        orig_ms = item['ms']
        orig_text = item['text']
        
        if orig_ms < 0:
            output.append(orig_text)
            continue

        ts = format_timestamp(orig_ms)
        
        # 排除制作人员署名行匹配翻译
        if META_LINE_RE.search(orig_text):
            output.append(f'{ts}{orig_text}')
            continue

        # 在滑动窗口内寻找最佳翻译行
        matched_trans = None
        best_diff = float('inf')
        best_idx = -1
        
        for j in range(p_trans, min(p_trans + 6, n_trans)):
            t_item = trans_lyrics[j]
            diff = abs(t_item['ms'] - orig_ms)
            if diff <= tolerance_ms and diff < best_diff:
                best_diff = diff
                best_idx = j

        if best_idx != -1:
            matched_trans = trans_lyrics[best_idx]['text']
            p_trans = best_idx + 1

        if matched_trans:
            if layout == 'dual_line':
                output.append(f'{ts}{orig_text}')
                output.append(f'{ts}{matched_trans}')
            elif layout == 'inline':
                output.append(f'{ts}{orig_text} ｜ {matched_trans}')
            elif layout == 'translation_only':
                output.append(f'{ts}{matched_trans}')
            else:
                output.append(f'{ts}{orig_text}')
                output.append(f'{ts}{matched_trans}')
        else:
            output.append(f'{ts}{orig_text}')

    return '\n'.join(output)


def fetch_netease_lyrics_raw(
    title: str,
    singer: str = '',
    duration: float = 0,
    session: Optional[requests.Session] = None,
    timeout: Tuple[float, float] = (3.0, 5.0)
) -> Dict[str, Any]:
    """
    通过网易云音乐 API 检索指定歌曲，获取原文与翻译歌词。
    返回: {'lrc': str, 'tlyric': str, 'romalrc': str, 'song_id': int, 'name': str}
    """
    empty_res = {'lrc': '', 'tlyric': '', 'romalrc': '', 'song_id': 0, 'name': ''}
    if not title:
        return empty_res

    sess = session or requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152.0 Safari/537.36',
        'Referer': 'https://music.163.com/'
    }

    try:
        norm = lambda s: re.sub(r'\s+', '', (s or '')).lower()
        search_kw = f'{title} {singer}'.strip()
        resp = sess.get(
            'https://music.163.com/api/search/get',
            params={'s': search_kw, 'type': 1, 'limit': 8, 'offset': 0},
            headers=headers,
            timeout=timeout
        )
        songs = ((resp.json().get('result') or {}).get('songs') or [])
        target = norm(title)
        best = None

        for song in songs:
            song_name = norm(song.get('name'))
            # 歌名包含或完全相等
            if song_name != target and target not in song_name and song_name not in target:
                continue
            song_dur = float(song.get('duration') or 0) / 1000.0
            if duration and song_dur and abs(song_dur - duration) > 5.0:
                continue
            best = song
            break

        if not best and songs:
            # 若没精确命中但有同名候选，宽松对比时长
            for song in songs:
                song_dur = float(song.get('duration') or 0) / 1000.0
                if duration and song_dur and abs(song_dur - duration) <= 3.0:
                    best = song
                    break

        if not best:
            return empty_res

        song_id = best['id']
        lr = sess.get(
            'https://music.163.com/api/song/lyric',
            params={'id': song_id, 'lv': 1, 'kv': 1, 'tv': -1},
            headers=headers,
            timeout=timeout
        )
        data = lr.json()
        return {
            'lrc': clean_text((data.get('lrc') or {}).get('lyric') or ''),
            'tlyric': clean_text((data.get('tlyric') or {}).get('lyric') or ''),
            'romalrc': clean_text((data.get('romalrc') or {}).get('lyric') or ''),
            'song_id': song_id,
            'name': best.get('name', '')
        }
    except Exception:
        return empty_res


def get_enhanced_lyrics(
    current_lrc: Optional[str],
    title: str,
    singer: str = '',
    duration: float = 0,
    layout: str = 'dual_line',
    enable_bilingual: bool = True,
    zh_convert: str = 'keep',
    session: Optional[requests.Session] = None
) -> Optional[str]:
    """
    智能歌词总调度入口：
    1. 按需进行双语抓取与对齐合并；
    2. 全局应用简繁转换策略（keep / t2s / s2t），无论单语还是双语歌词均统一转换；
    3. 优雅回退，确保即使无翻译也能安全返回原文。
    """
    clean_current = clean_text(current_lrc or '')
    final_lrc = None
    
    # 1. 未开启双语，直接采用当前已有歌词（若无则尝试拉取网易云单语歌词）
    if not enable_bilingual:
        if clean_current:
            final_lrc = clean_current
        else:
            ndata = fetch_netease_lyrics_raw(title=title, singer=singer, duration=duration, session=session)
            final_lrc = ndata.get('lrc', '') or None
    # 2. 如果已经包含双语，直接使用已有双语
    elif clean_current and already_bilingual(clean_current):
        final_lrc = clean_current
    # 3. 如果已有歌词且明确不是外语歌（是中文歌），直接使用已有歌词
    elif clean_current and not is_foreign_lyrics(clean_current):
        final_lrc = clean_current
    # 4. 需要寻找中文翻译（外语歌或尚无有效歌词）
    else:
        netease_data = fetch_netease_lyrics_raw(
            title=title,
            singer=singer,
            duration=duration,
            session=session
        )
        tlyric = netease_data.get('tlyric', '')
        netease_lrc = netease_data.get('lrc', '')

        if tlyric:
            orig_base = clean_current if (clean_current and '[' in clean_current) else netease_lrc
            if orig_base:
                final_lrc = merge_bilingual_lrc(orig_base, tlyric, layout=layout)
            else:
                final_lrc = tlyric
        elif clean_current:
            final_lrc = clean_current
        elif netease_lrc:
            final_lrc = netease_lrc

    # 5. 全局简繁转换处理管道（支持简体化、繁体化或保持原样）
    if final_lrc and zh_convert in ('t2s', 's2t') and _ZH_CONVERTER_AVAILABLE and zh_converter:
        final_lrc = zh_converter.convert_lrc(final_lrc, mode=zh_convert)

    return final_lrc
