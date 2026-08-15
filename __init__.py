"""
Anki 发音补丁插件
选中卡片 → 快捷键/菜单 → 弹窗输入单词 → 下载美式发音 → 嵌入 HTML 播放按钮
"""
import os
import re
import json
import urllib.request
import urllib.error
import urllib.parse
import concurrent.futures
import hashlib

from aqt import mw
from aqt.qt import *
from aqt import utils
from aqt import gui_hooks
try:
    from aqt import dialogs
except ImportError:
    dialogs = None



# ============================================================
# 本地缓存
# ============================================================

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".anki-pron-cache")


def _ensure_cache_dir():
    if not os.path.exists(_CACHE_DIR):
        os.makedirs(_CACHE_DIR, exist_ok=True)


def cache_get(word):
    """从缓存读取音频，返回 (filename, audio_bytes) 或 None"""
    _ensure_cache_dir()
    filepath = os.path.join(_CACHE_DIR, f"{word}.mp3")
    if os.path.exists(filepath):
        with open(filepath, "rb") as f:
            return f"{word}.mp3", f.read()
    return None


def cache_put(word, audio_bytes):
    """将音频写入缓存"""
    _ensure_cache_dir()
    filepath = os.path.join(_CACHE_DIR, f"{word}.mp3")
    with open(filepath, "wb") as f:
        f.write(audio_bytes)


# ============================================================
# 单词提取（从 word_extractor.py 合并）
# ============================================================

_PATTERN_PHONETIC = re.compile(
    r'([a-zA-Z]+)'
    r'(?:\s+|\s*\|\s*)'
    r'/[^/]{3,}/?'
)
_PATTERN_PURE_WORD = re.compile(r'^[a-zA-Z]{2,}$')
_PATTERN_SOUND = re.compile(r'\[sound:([^\]]+)\]')
# 匹配 HTML 按钮中的文件名：id="anki-play-xxx" 或 new Audio('xxx.mp3')
_PATTERN_HTML_BTN = re.compile(r"anki-play-([a-zA-Z0-9_-]+)")


def extract_word(text):
    if not text:
        return None
    cleaned = re.sub(r'<[^>]+>', '', text)
    cleaned = cleaned.replace('**', '').replace('*', '').strip()
    if not cleaned:
        return None
    m = _PATTERN_PHONETIC.search(cleaned)
    if m:
        return m.group(1)
    lines = [l.strip() for l in cleaned.split('\n') if l.strip()]
    if lines:
        first_line = lines[0]
        first_line = re.sub(r'^[-*]\s+', '', first_line)
        first_line = re.sub(r'^\d+\.\s*', '', first_line)
        first_line = first_line.strip()
        if _PATTERN_PURE_WORD.match(first_line):
            return first_line
    return None


def find_existing_pronunciations(text):
    """查找字段中已有的发音标记（支持 [sound:] 和 HTML 按钮两种格式）"""
    if not text:
        return []
    results = []
    # [sound:xxx.mp3] 格式
    for m in _PATTERN_SOUND.findall(text):
        if m not in results:
            results.append(m)
    # HTML 按钮格式 anki-play-xxx
    for m in _PATTERN_HTML_BTN.findall(text):
        filename = m + ".mp3"
        if filename not in results:
            results.append(filename)
    return results


def remove_pronunciation(text, filename):
    """移除发音标记（支持 [sound:] 和 HTML 按钮两种格式）"""
    word = filename.replace(".mp3", "")
    # 移除 [sound:xxx.mp3]
    pattern_sound = re.compile(r'\s?\[sound:' + re.escape(filename) + r'\]\s?')
    text = pattern_sound.sub(' ', text)
    # 移除特定按钮（只删除这个单词的按钮，保留其他）
    pattern_btn = re.compile(
        r'<button[^>]*id="anki-play-' + re.escape(word) + r'"[^>]*>.*?</button>',
        re.DOTALL
    )
    text = pattern_btn.sub('', text)
    # 如果没有剩余按钮了，移除整个容器和脚本
    if 'anki-play-' not in text:
        pattern_container = re.compile(
            r'<div\s+style="margin-top:\s*8px;">.*?</div>',
            re.DOTALL
        )
        text = pattern_container.sub('', text)
        pattern_script = re.compile(r'<script>[^<]*anki-play-[^<]*</script>', re.DOTALL)
        text = pattern_script.sub('', text)
    # 合并多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ============================================================
# MW API 发音获取（从 pron_fetcher.py 合并）
# ============================================================

def fetch_pronunciation(word, api_key):
    if not api_key or not word:
        return None, None
    # 先检查缓存
    cached = cache_get(word)
    if cached:
        return cached
    # 缓存未命中，调用 API
    url = (
        f"https://www.dictionaryapi.com/api/v3/references/collegiate/json/"
        f"{urllib.parse.quote(word)}?key={api_key}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None, None
    if not isinstance(data, list) or not data:
        return None, None
    if isinstance(data[0], str):
        return None, None
    try:
        sound = data[0]["hwi"]["prs"][0]["sound"]
    except (KeyError, IndexError):
        return None, None
    audio_name = sound.get("audio")
    if not audio_name:
        return None, None
    first_letter = audio_name[0]
    audio_url = (
        f"https://media.merriam-webster.com/audio/prons/en/us/wav/"
        f"{first_letter}/{audio_name}.wav"
    )
    try:
        req = urllib.request.Request(audio_url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            audio_bytes = resp.read()
    except (urllib.error.URLError, OSError):
        return None, None
    filename = f"{word}.mp3"
    # 写入缓存
    cache_put(word, audio_bytes)
    return filename, audio_bytes


# ============================================================
# TTS 整句发音（小米 MiMo API）
# ============================================================

def fetch_tts(text, api_key):
    """调用小米 MiMo TTS API 生成语音，返回 (filename, audio_bytes, error_msg)"""
    if not api_key or not text or not text.strip():
        return None, None, "未提供 API Key 或文本"
    # 用文本哈希作为文件名，避免重复生成
    text_hash = hashlib.md5(text.strip().encode("utf-8")).hexdigest()[:12]
    filename = f"tts_{text_hash}.mp3"
    # 先检查缓存
    cached = cache_get(f"tts_{text_hash}")
    if cached:
        return cached[0], cached[1], None
    # 根据 key 格式选择正确的 BASE_URL
    if api_key.startswith("tp-"):
        base_url = "https://token-plan-cn.xiaomimimo.com/v1"
    else:
        base_url = "https://api.xiaomimimo.com/v1"
    url = f"{base_url}/chat/completions"
    payload = json.dumps({
        "model": "mimo-v2.5-tts",
        "messages": [
            {"role": "user", "content": "Generate speech for the following text."},
            {"role": "assistant", "content": text.strip()}
        ],
        "audio": {
            "format": "mp3",
            "voice": "mimo_default"
        }
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")[:200]
        return None, None, f"HTTP {e.code}: {error_body}"
    except urllib.error.URLError as e:
        return None, None, f"网络错误: {e.reason}"
    except OSError as e:
        return None, None, f"连接错误: {e}"
    except json.JSONDecodeError:
        return None, None, "响应格式错误（非 JSON）"
    # 从响应中提取 base64 音频数据
    try:
        audio_b64 = resp_data["choices"][0]["message"]["audio"]["data"]
        import base64
        audio_bytes = base64.b64decode(audio_b64)
    except (KeyError, IndexError, TypeError):
        return None, None, f"响应中无音频数据: {str(resp_data)[:150]}"
    if not audio_bytes or len(audio_bytes) < 100:
        return None, None, "音频数据异常"
    cache_put(f"tts_{text_hash}", audio_bytes)
    return filename, audio_bytes, None


# ============================================================
# 配置管理
# ============================================================

def get_config():
    try:
        return mw.addonManager.getConfig(__name__) or {}
    except Exception:
        return {}


def write_config(config):
    try:
        mw.addonManager.writeConfig(__name__, config)
    except Exception:
        pass


# ============================================================
# 音频播放
# ============================================================

def play_audio_file(filename):
    media_dir = mw.col.media.dir()
    filepath = os.path.join(media_dir, filename)
    if not os.path.exists(filepath):
        utils.tooltip("文件不存在: " + filename)
        return
    try:
        from aqt.sound import av_player
        av_player.play_file(filepath)
    except ImportError:
        from anki.sound import play
        play(filepath)


def check_audio_exists(filename):
    """检查音频文件是否存在于 Anki 媒体库"""
    media_dir = mw.col.media.dir()
    filepath = os.path.join(media_dir, filename)
    return os.path.exists(filepath)


def build_pronunciation_html(entries):
    """
    构建发音按钮 HTML，与 anki-sender 风格完全一致。
    entries: [(word, filename), ...] 支持多个单词
    所有按钮共享一个音量滑块。
    使用 inline onclick，编辑卡片后不会失效。
    """
    btn_style = (
        "background:#f0f0f0;border:1px solid #ccc;border-radius:4px;"
        "padding:4px 12px;cursor:pointer;font-size:14px;margin-right:6px;"
    )
    buttons = ""
    for word, filename in entries:
        onclick = (
            f"var v=+(localStorage.getItem('anki-sender-vol')||'0.8');"
            f"var a=new Audio('{word}.mp3');a.volume=v;a.play();"
        )
        buttons += (
            f'<button id="anki-play-{word}" style="{btn_style}" '
            f'onclick="{onclick}">🔊 {word}</button>'
        )
    # 共享音量滑块（oninput 直接写在属性上，不依赖 script）
    volume_bar = (
        '<span style="margin-left:8px;font-size:13px;color:#888;">🔈</span>'
        '<input id="anki-vol" type="range" min="0" max="100" value="80" '
        'style="width:80px;vertical-align:middle;" '
        'oninput="var v=this.value/100;'
        "document.getElementById('anki-vol-val').textContent=v.toFixed(1);"
        "localStorage.setItem('anki-sender-vol',v);\">"
        '<span id="anki-vol-val" style="font-size:12px;color:#888;">0.8</span>'
    )
    return f'<div style="margin-top:8px;">{buttons}{volume_bar}</div>'


# ============================================================
# 发音添加弹窗
# ============================================================

class PronDialog(QDialog):
    def __init__(self, parent, detected_word, existing_prons, note, field_name):
        super().__init__(parent)
        self.setWindowTitle("添加发音")
        self.setMinimumWidth(520)
        self.note = note
        self.field_name = field_name
        self.original_content = note[field_name]  # 备份原始内容
        self.saved = False  # 是否已保存到数据库
        self.existing_prons = list(existing_prons)
        # 多单词支持：results = [(word, filename, audio_bytes), ...]
        self.results = []
        self.result_action = None
        # 修改模式：记录正在被替换的旧发音文件名
        self.replacing_pron = None
        # TTS 修改模式：记录正在被替换的旧 TTS ID
        self.modifying_tts_id = None

        layout = QVBoxLayout()

        # 单词输入（支持空格分隔多个单词）
        word_group = QGroupBox("单词")
        word_layout = QHBoxLayout()
        self.word_input = QLineEdit(detected_word or "")
        self.word_input.setPlaceholderText("输入单词，多个用空格分隔，如 proceed process")
        self.word_input.returnPressed.connect(self.on_fetch)
        word_layout.addWidget(QLabel("单词："))
        word_layout.addWidget(self.word_input)
        word_group.setLayout(word_layout)
        layout.addWidget(word_group)

        # 已有发音
        self.exist_group = QGroupBox("已有发音")
        self.exist_layout = QVBoxLayout()
        self._render_existing_prons()
        self.exist_group.setLayout(self.exist_layout)
        layout.addWidget(self.exist_group)

        # 目标字段
        field_group = QGroupBox("目标字段")
        field_layout = QHBoxLayout()
        self.field_combo = QComboBox()
        self.field_combo.addItems(note.keys())
        self.field_combo.setCurrentText(field_name)
        field_layout.addWidget(QLabel("写入字段："))
        field_layout.addWidget(self.field_combo)
        field_group.setLayout(field_layout)
        layout.addWidget(field_group)

        # 状态提示
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        # 脚本状态检测
        self.script_status_label = QLabel("")
        self.script_status_label.setWordWrap(True)
        layout.addWidget(self.script_status_label)

        # TTS 整句发音
        tts_group = QGroupBox("整句发音（小米 TTS）")
        tts_layout = QVBoxLayout()
        tts_input_layout = QHBoxLayout()
        self.tts_input = QLineEdit()
        self.tts_input.setPlaceholderText("输入整句英文，如 The quick brown fox jumps over the lazy dog")
        self.tts_input.returnPressed.connect(self.on_generate_tts)
        tts_input_layout.addWidget(QLabel("句子："))
        tts_input_layout.addWidget(self.tts_input)
        tts_layout.addLayout(tts_input_layout)
        tts_btn_layout = QHBoxLayout()
        gen_tts_btn = QPushButton("生成整句发音")
        gen_tts_btn.setAutoDefault(False)
        gen_tts_btn.clicked.connect(self.on_generate_tts)
        tts_btn_layout.addWidget(gen_tts_btn)
        rm_tts_btn = QPushButton("移除全部")
        rm_tts_btn.setAutoDefault(False)
        rm_tts_btn.clicked.connect(self.on_remove_all_tts)
        tts_btn_layout.addWidget(rm_tts_btn)
        tts_layout.addLayout(tts_btn_layout)
        # 已有整句发音列表
        self.tts_exist_group = QGroupBox("已有整句发音")
        self.tts_exist_layout = QVBoxLayout()
        self.tts_exist_group.setLayout(self.tts_exist_layout)
        tts_layout.addWidget(self.tts_exist_group)
        self.tts_status_label = QLabel("")
        self.tts_status_label.setWordWrap(True)
        tts_layout.addWidget(self.tts_status_label)
        tts_group.setLayout(tts_layout)
        layout.addWidget(tts_group)
        self._update_tts_status()

        # 试听按钮
        self.preview_btn = QPushButton("试听")
        self.preview_btn.setEnabled(False)
        self.preview_btn.clicked.connect(self.on_preview)
        layout.addWidget(self.preview_btn)

        # 按钮行
        btn_layout = QHBoxLayout()
        fetch_btn = QPushButton("查询发音")
        fetch_btn.clicked.connect(self.on_fetch)
        btn_layout.addWidget(fetch_btn)
        self.fix_script_btn = QPushButton("修复脚本")
        self.fix_script_btn.clicked.connect(self.on_fix_script)
        btn_layout.addWidget(self.fix_script_btn)
        btn_layout.addStretch()
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.on_refresh)
        btn_layout.addWidget(refresh_btn)
        save_btn = QPushButton("保存")
        save_btn.clicked.connect(self.on_save_only)
        btn_layout.addWidget(save_btn)
        add_btn = QPushButton("添加发音")
        add_btn.clicked.connect(self.on_add)
        btn_layout.addWidget(add_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        # 检测脚本状态（在 fix_script_btn 创建后）
        self._check_script_status()

        # 延迟设置焦点，等对话框完全渲染后再设置
        QTimer.singleShot(100, self.word_input.setFocus)

    def closeEvent(self, event):
        """关闭窗口：如果未保存，恢复原始内容"""
        if not self.saved:
            self.note[self.field_name] = self.original_content
        event.accept()

    def _render_existing_prons(self):
        while self.exist_layout.count():
            item = self.exist_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()
        if not self.existing_prons:
            self.exist_group.setVisible(False)
            return
        self.exist_group.setVisible(True)
        for pr in self.existing_prons:
            row = QHBoxLayout()
            # 检查音频文件是否存在
            exists = check_audio_exists(pr)
            if exists:
                status_label = QLabel("  ✅")
                status_label.setToolTip("音频文件正常")
            else:
                status_label = QLabel("  ❌")
                status_label.setToolTip("音频文件缺失")
            row.addWidget(status_label)
            row.addWidget(QLabel(f"  {pr}"))
            # 播放按钮
            play_btn = QPushButton("🔊")
            play_btn.setMinimumWidth(30)
            play_btn.setMaximumWidth(36)
            play_btn.setAutoDefault(False)
            play_btn.setToolTip("试听")
            play_btn.setEnabled(exists)
            play_btn.clicked.connect(lambda _, p=pr: play_audio_file(p))
            row.addWidget(play_btn)
            if not exists:
                repair_btn = QPushButton("修复")
                repair_btn.setMinimumWidth(50)
                repair_btn.setMaximumWidth(80)
                repair_btn.setAutoDefault(False)
                repair_btn.clicked.connect(lambda _, p=pr: self.on_repair_existing(p))
                row.addWidget(repair_btn)
            mod_btn = QPushButton("修改")
            mod_btn.setMinimumWidth(50)
            mod_btn.setMaximumWidth(80)
            mod_btn.setAutoDefault(False)
            mod_btn.clicked.connect(lambda _, p=pr: self.on_modify_existing(p))
            row.addWidget(mod_btn)
            del_btn = QPushButton("删除")
            del_btn.setMinimumWidth(50)
            del_btn.setMaximumWidth(80)
            del_btn.setAutoDefault(False)
            del_btn.clicked.connect(lambda _, p=pr: self.on_delete_existing(p))
            row.addWidget(del_btn)
            self.exist_layout.addLayout(row)

    def on_fetch(self):
        text = self.word_input.text().strip()
        if not text:
            self.status_label.setText("请输入单词")
            return
        config = get_config()
        api_key = config.get("api_key", "")
        if not api_key:
            self.status_label.setText("未配置 API Key，请在插件设置中配置")
            return
        # 按空格分割，支持多个单词
        words = [w.strip() for w in text.split() if w.strip()]
        if not words:
            self.status_label.setText("请输入单词")
            return
        self.results = []
        failed_words = []
        total = len(words)
        self.status_label.setText(f"正在查询 0/{total}...")
        QApplication.processEvents()
        # 并行查询
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_word = {
                executor.submit(fetch_pronunciation, w, api_key): w
                for w in words
            }
            done_count = 0
            for future in concurrent.futures.as_completed(future_to_word):
                word = future_to_word[future]
                done_count += 1
                self.status_label.setText(f"正在查询 {done_count}/{total}...")
                QApplication.processEvents()
                try:
                    filename, audio_bytes = future.result()
                    if filename:
                        self.results.append((word, filename, audio_bytes))
                    else:
                        failed_words.append(word)
                except Exception:
                    failed_words.append(word)
        if not self.results:
            self.status_label.setText(f"未找到发音：{', '.join(failed_words)}")
            self.preview_btn.setEnabled(False)
        else:
            msg = f"找到 {len(self.results)} 个发音"
            if failed_words:
                msg += f"，未找到：{', '.join(failed_words)}"
            self.status_label.setText(msg)
            self.preview_btn.setEnabled(True)

    def on_preview(self):
        if not self.results:
            return
        media_dir = mw.col.media.dir()
        for word, filename, audio_bytes in self.results:
            filepath = os.path.join(media_dir, filename)
            if not os.path.exists(filepath):
                with open(filepath, "wb") as f:
                    f.write(audio_bytes)
            play_audio_file(filename)

    def on_add(self):
        if not self.results:
            self.status_label.setText("请先查询发音")
            return
        field = self.field_combo.currentText()
        content = self.note[field]
        # 写入所有音频文件到媒体库
        media_dir = mw.col.media.dir()
        for word, filename, audio_bytes in self.results:
            filepath = os.path.join(media_dir, filename)
            if not os.path.exists(filepath):
                with open(filepath, "wb") as f:
                    f.write(audio_bytes)
        # 收集所有保留的发音（原有 + 新增）
        # 先从现有内容中提取所有原有的单词（排除新增的和正在替换的）
        new_filenames = [filename for _, filename, _ in self.results]
        existing_entries = []
        for pr in self.existing_prons:
            if pr not in new_filenames and pr != self.replacing_pron:
                word = pr.replace(".mp3", "")
                existing_entries.append((word, pr))
        # 移除整个现有的发音容器
        pattern_container = re.compile(
            r'<div\s+style="margin-top:\s*8px;">.*?</div>',
            re.DOTALL
        )
        content = pattern_container.sub('', content)
        # 也移除 [sound:] 格式的标记
        for pr in self.existing_prons:
            pattern_sound = re.compile(r'\s?\[sound:' + re.escape(pr) + r'\]\s?')
            content = pattern_sound.sub(' ', content)
        # 移除正在被替换的旧发音
        if self.replacing_pron:
            content = remove_pronunciation(content, self.replacing_pron)
            self.replacing_pron = None
        # 合并所有发音：保留的 + 新增的
        new_entries = [(word, filename) for word, filename, _ in self.results]
        all_entries = existing_entries + new_entries
        # 构建合并后的 HTML
        audio_html = build_pronunciation_html(all_entries)
        if content and not content.endswith("\n"):
            content += "\n"
        content += audio_html
        # 清理多余空行
        content = re.sub(r'\n{3,}', '\n\n', content)
        self.note[field] = content.strip()
        # 更新已有发音列表（不关闭对话框）
        self.existing_prons = find_existing_pronunciations(self.note[field])
        self._render_existing_prons()
        words = ", ".join(w for w, _, _ in self.results)
        self.status_label.setText(f"已添加：{words}")
        self.results = []  # 清空查询结果

    def on_delete_existing(self, filename):
        field = self.field_combo.currentText()
        content = self.note[field]
        self.note[field] = remove_pronunciation(content, filename)
        if filename in self.existing_prons:
            self.existing_prons.remove(filename)
        self._render_existing_prons()
        self.status_label.setText(f"已删除 {filename}")

    def on_repair_existing(self, filename):
        """修复缺失的音频文件"""
        word = filename.replace(".mp3", "")
        config = get_config()
        api_key = config.get("api_key", "")
        if not api_key:
            self.status_label.setText("未配置 API Key，无法修复")
            return
        self.status_label.setText(f"正在修复「{word}」...")
        QApplication.processEvents()
        new_filename, audio_bytes = fetch_pronunciation(word, api_key)
        if new_filename and audio_bytes:
            media_dir = mw.col.media.dir()
            filepath = os.path.join(media_dir, filename)
            with open(filepath, "wb") as f:
                f.write(audio_bytes)
            self._render_existing_prons()
            self.status_label.setText(f"已修复 {filename}")
        else:
            self.status_label.setText(f"修复失败：未找到「{word}」的发音")

    def on_save_only(self):
        """保存到数据库，不关闭对话框"""
        field = self.field_combo.currentText()
        mw.col.update_note(self.note)
        # 重新从数据库加载 note，确保后续操作使用最新数据
        self.note = mw.col.get_note(self.note.id)
        self.original_content = self.note[self.field_name]
        self.saved = True
        self._update_tts_status()
        self.status_label.setText("已保存")

    def on_modify_existing(self, filename):
        """修改已有发音：将单词填入输入框，标记为替换模式"""
        word = filename.replace(".mp3", "")
        self.word_input.setText(word)
        self.replacing_pron = filename
        self.status_label.setText(f"修改「{word}」：编辑单词后点击「查询发音」→「添加发音」")

    def on_refresh(self):
        """刷新已有发音列表"""
        field = self.field_combo.currentText()
        content = self.note[field]
        self.existing_prons = find_existing_pronunciations(content)
        self._render_existing_prons()
        self._check_script_status()
        self._update_tts_status()
        self.status_label.setText(f"已刷新，共 {len(self.existing_prons)} 个发音")

    def _check_script_status(self):
        """检测按钮播放状态（inline onclick 模式，不依赖 script）"""
        try:
            field = self.field_combo.currentText()
            if not field or field not in self.note:
                self.script_status_label.setText("")
                self.fix_script_btn.setEnabled(False)
                return
            content = self.note[field]
            has_word_btns = 'anki-play-' in content
            has_tts_btns = 'anki-tts-' in content
            has_buttons = has_word_btns or has_tts_btns
            if not has_buttons:
                self.script_status_label.setText("")
                self.fix_script_btn.setEnabled(False)
                return
            # 检测单词按钮是否使用 inline onclick（新版）还是依赖 script（旧版）
            word_btn_has_onclick = bool(re.search(
                r'id="anki-play-[^"]*"[^>]*onclick="', content))
            has_old_script = '<script>' in content and has_word_btns
            if has_word_btns and not word_btn_has_onclick:
                # 旧版按钮，没有 inline onclick，需要修复
                self.script_status_label.setText(
                    "⚠️ 单词按钮缺少 inline onclick，编辑后会失效")
                self.fix_script_btn.setEnabled(True)
            elif has_old_script:
                # 有 inline onclick 但还有残留的旧 script，可以清理
                self.script_status_label.setText(
                    "⚠️ 存在旧版 script 残留，建议清理")
                self.fix_script_btn.setEnabled(True)
            else:
                self.script_status_label.setText("✅ 按钮正常（inline 播放）")
                self.fix_script_btn.setEnabled(False)
        except Exception:
            self.script_status_label.setText("")
            self.fix_script_btn.setEnabled(False)

    def on_fix_script(self):
        """修复按钮：移除旧 script，为旧版单词按钮添加 inline onclick"""
        field = self.field_combo.currentText()
        content = self.note[field]
        # 1. 移除所有现有的 <script> 块
        pattern_script = re.compile(r'<script>.*?</script>', re.DOTALL)
        content = pattern_script.sub('', content)
        # 2. 为没有 onclick 的旧版单词按钮添加 inline onclick
        def _add_onclick(m):
            btn = m.group(0)
            if 'onclick=' in btn:
                return btn  # 已有 onclick，跳过
            id_match = re.search(r'id="anki-play-([^"]+)"', btn)
            if not id_match:
                return btn
            word = id_match.group(1)
            onclick = (
                f"var v=+(localStorage.getItem('anki-sender-vol')||'0.8');"
                f"var a=new Audio('{word}.mp3');a.volume=v;a.play();"
            )
            return btn.replace('>', f' onclick="{onclick}">', 1)
        content = re.sub(
            r'<button\s+id="anki-play-[^"]*"[^>]*>.*?</button>',
            _add_onclick, content, flags=re.DOTALL)
        # 3. 为旧版音量滑块添加 oninput（如果没有的话）
        if 'anki-vol' in content and 'oninput=' not in content:
            content = re.sub(
                r'(<input\s+id="anki-vol"\s+type="range"[^>]*?)(>)',
                r'\1 oninput="var v=this.value/100;'
                r"document.getElementById('anki-vol-val').textContent=v.toFixed(1);"
                r"localStorage.setItem('anki-sender-vol',v);\"\2",
                content)
        self.note[field] = content
        self._check_script_status()
        self.status_label.setText("已修复：添加 inline onclick，移除旧 script")

    def _find_existing_tts(self):
        """查找已有的 TTS 整句发音，返回 [(tts_id, filename, display_text), ...]"""
        field = self.field_combo.currentText()
        try:
            content = self.note[field]
        except (KeyError, IndexError):
            return []
        results = []
        # 匹配 TTS 按钮：<button id="anki-tts-xxx" ...>🔊 text...</button>
        pattern = re.compile(
            r'<button[^>]*id="(anki-tts-[a-f0-9]+)"[^>]*>(.*?)</button>',
            re.DOTALL
        )
        for m in pattern.finditer(content):
            tts_id = m.group(1)
            btn_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            # 去掉开头的 🔊 符号
            btn_text = re.sub(r'^🔊\s*', '', btn_text)
            # tts_id 格式: anki-tts-{hash}, 实际文件: tts_{hash}.mp3
            hash_part = tts_id.replace("anki-tts-", "")
            filename = f"tts_{hash_part}.mp3"
            results.append((tts_id, filename, btn_text))
        return results

    def _render_existing_tts(self):
        """渲染已有 TTS 整句发音列表"""
        while self.tts_exist_layout.count():
            item = self.tts_exist_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                while item.layout().count():
                    sub = item.layout().takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()
        existing_tts = self._find_existing_tts()
        if not existing_tts:
            self.tts_exist_group.setVisible(False)
            return
        self.tts_exist_group.setVisible(True)
        for tts_id, filename, display_text in existing_tts:
            row = QHBoxLayout()
            exists = check_audio_exists(filename)
            if exists:
                status_label = QLabel("  ✅")
                status_label.setToolTip("音频文件正常")
            else:
                status_label = QLabel("  ❌")
                status_label.setToolTip("音频文件缺失")
            row.addWidget(status_label)
            row.addWidget(QLabel(f"  {display_text[:50]}"))
            # 播放按钮
            play_btn = QPushButton("🔊")
            play_btn.setMinimumWidth(30)
            play_btn.setMaximumWidth(36)
            play_btn.setAutoDefault(False)
            play_btn.setToolTip("试听")
            play_btn.setEnabled(exists)
            play_btn.clicked.connect(lambda _, f=filename: play_audio_file(f))
            row.addWidget(play_btn)
            mod_btn = QPushButton("修改")
            mod_btn.setMinimumWidth(50)
            mod_btn.setMaximumWidth(80)
            mod_btn.setAutoDefault(False)
            mod_btn.clicked.connect(lambda _, t=display_text: self.on_modify_tts(t))
            row.addWidget(mod_btn)
            del_btn = QPushButton("删除")
            del_btn.setMinimumWidth(50)
            del_btn.setMaximumWidth(80)
            del_btn.setAutoDefault(False)
            del_btn.clicked.connect(lambda _, i=tts_id: self.on_delete_tts(i))
            row.addWidget(del_btn)
            self.tts_exist_layout.addLayout(row)

    def _update_tts_status(self):
        """更新 TTS 状态并渲染已有整句发音列表"""
        self._render_existing_tts()
        existing_tts = self._find_existing_tts()
        if existing_tts:
            self.tts_status_label.setText(f"已有 {len(existing_tts)} 个整句发音")
        else:
            self.tts_status_label.setText("")

    def on_generate_tts(self):
        """为手动输入的句子生成整句发音"""
        sentence = self.tts_input.text().strip()
        if not sentence:
            self.tts_status_label.setText("请输入句子")
            return
        config = get_config()
        xiaomi_key = config.get("xiaomi_api_key", "")
        if not xiaomi_key:
            self.tts_status_label.setText("未配置小米 API Key，请在插件设置中配置")
            return
        field = self.field_combo.currentText()
        content = self.note[field]
        media_dir = mw.col.media.dir()
        # 如果是修改模式，先删除旧的 TTS 按钮
        if self.modifying_tts_id:
            old_pattern = re.compile(
                r'<button[^>]*id="' + re.escape(self.modifying_tts_id) + r'"[^>]*>.*?</button>',
                re.DOTALL
            )
            content = old_pattern.sub('', content)
            self.modifying_tts_id = None
        self.tts_status_label.setText("正在生成整句发音...")
        QApplication.processEvents()
        filename, audio_bytes, error = fetch_tts(sentence, xiaomi_key)
        if error:
            self.tts_status_label.setText(f"生成失败：{error}")
            return
        # 写入媒体库
        filepath = os.path.join(media_dir, filename)
        if not os.path.exists(filepath):
            with open(filepath, "wb") as f:
                f.write(audio_bytes)
        # 在内容末尾追加 TTS 播放按钮
        tts_id = "anki-tts-" + filename.replace(".mp3", "").replace("tts_", "")
        tts_btn_style = (
            "background:#e8f5e9;border:1px solid #4caf50;border-radius:3px;"
            "padding:2px 6px;cursor:pointer;font-size:12px;margin-left:4px;"
        )
        tts_btn = (
            f'\n<button id="{tts_id}" style="{tts_btn_style}" '
            f'onclick="var a=new Audio(\'{filename}\');a.volume='
            f"+(localStorage.getItem('anki-sender-vol')||'0.8');"
            f'a.play();">🔊 {sentence[:30]}...</button>'
        )
        if content and not content.endswith("\n"):
            content += "\n"
        content += tts_btn
        content = re.sub(r'\n{3,}', '\n\n', content)
        self.note[field] = content.strip()
        self.tts_input.clear()
        self._update_tts_status()
        self.status_label.setText(f"已添加整句发音：{sentence[:50]}")

    def on_remove_all_tts(self):
        """移除所有整句发音按钮"""
        field = self.field_combo.currentText()
        content = self.note[field]
        # 移除所有 anki-tts-xxx 按钮
        pattern = re.compile(r'<button[^>]*id="anki-tts-[^"]*"[^>]*>.*?</button>', re.DOTALL)
        new_content = pattern.sub('', content)
        removed = content != new_content
        self.note[field] = new_content.strip()
        self._update_tts_status()
        if removed:
            self.status_label.setText("已移除全部整句发音")
        else:
            self.status_label.setText("没有可移除的整句发音")

    def on_delete_tts(self, tts_id):
        """删除单个 TTS 整句发音"""
        field = self.field_combo.currentText()
        content = self.note[field]
        # 删除特定 TTS 按钮
        pattern = re.compile(
            r'<button[^>]*id="' + re.escape(tts_id) + r'"[^>]*>.*?</button>',
            re.DOTALL
        )
        self.note[field] = pattern.sub('', content).strip()
        self._update_tts_status()
        self.status_label.setText(f"已删除 {tts_id}")

    def on_modify_tts(self, display_text):
        """修改 TTS：将句子文本填入输入框，标记为替换模式"""
        self.tts_input.setText(display_text)
        # 找到对应的 TTS ID
        field = self.field_combo.currentText()
        try:
            content = self.note[field]
        except (KeyError, IndexError):
            content = ""
        pattern = re.compile(
            r'<button[^>]*id="(anki-tts-[a-f0-9]+)"[^>]*>(.*?)</button>',
            re.DOTALL
        )
        for m in pattern.finditer(content):
            btn_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            btn_text = re.sub(r'^🔊\s*', '', btn_text)
            if btn_text == display_text or btn_text.startswith(display_text[:30]):
                self.modifying_tts_id = m.group(1)
                break
        self.tts_status_label.setText("修改模式：编辑句子后点击「生成整句发音」")


# ============================================================
# 单张添加发音
# ============================================================

def get_current_card_and_note():
    if mw.reviewer and mw.reviewer.card:
        card = mw.reviewer.card
        # 从数据库重新加载 card 和 note，避免缓存问题
        card = mw.col.get_card(card.id)
        return card, card.note()
    try:
        browser = dialogs._dialogs.get("Browser")
        if browser:
            browser = browser[1]
            if browser:
                selected = browser.selectedCards()
                if selected:
                    card = mw.col.get_card(selected[0])
                    return card, card.note()
    except Exception:
        pass
    return None, None


def add_pronunciation_single():
    card, note = get_current_card_and_note()
    if not note:
        utils.tooltip("未找到当前卡片")
        return
    config = get_config()
    field_name = config.get("target_field", "引用")
    if field_name not in note.keys():
        utils.tooltip(f"字段「{field_name}」不存在，可用字段：{', '.join(note.keys())}")
        return
    content = note[field_name]
    detected_word = extract_word(content)
    existing_prons = find_existing_pronunciations(content)
    parent = mw.app.activeWindow() or mw
    dialog = PronDialog(parent, detected_word, existing_prons, note, field_name)
    dialog.exec()


# ============================================================
# 批量添加发音
# ============================================================

def add_pronunciation_batch():
    try:
        browser = dialogs._dialogs.get("Browser")
        if not browser:
            utils.tooltip("请在浏览器中使用批量模式")
            return
        browser = browser[1]
    except Exception:
        utils.tooltip("请在浏览器中使用批量模式")
        return
    if not browser:
        utils.tooltip("请在浏览器中使用批量模式")
        return
    selected_ids = browser.selectedCards()
    if not selected_ids:
        utils.tooltip("请先选中要添加发音的卡片")
        return
    config = get_config()
    field_name = config.get("target_field", "引用")
    success = 0
    skipped = 0
    failed = 0
    for card_id in selected_ids:
        card = mw.col.get_card(card_id)
        note = card.note()
        if field_name not in note.keys():
            failed += 1
            continue
        content = note[field_name]
        existing_prons = find_existing_pronunciations(content)
        if existing_prons:
            skipped += 1
            continue
        detected_word = extract_word(content)
        parent = mw.app.activeWindow() or mw
        dialog = PronDialog(parent, detected_word, existing_prons, note, field_name)
        dialog.exec()
        if dialog.saved:
            success += 1
        else:
            failed += 1
    utils.tooltip(
        f"批量完成：{success} 张添加成功，"
        f"{skipped} 张跳过（已有发音），"
        f"{failed} 张失败"
    )


# ============================================================
# 设置弹窗
# ============================================================


# ============================================================
# 批量修复发音
# ============================================================

def repair_pronunciation_batch():
    """批量修复选中卡片中缺失的音频文件"""
    try:
        browser = dialogs._dialogs.get("Browser")
        if not browser:
            utils.tooltip("请在浏览器中使用批量修复")
            return
        browser = browser[1]
    except Exception:
        utils.tooltip("请在浏览器中使用批量修复")
        return
    if not browser:
        utils.tooltip("请在浏览器中使用批量修复")
        return
    selected_ids = browser.selectedCards()
    if not selected_ids:
        utils.tooltip("请先选中要修复的卡片")
        return
    config = get_config()
    api_key = config.get("api_key", "")
    if not api_key:
        utils.tooltip("未配置 API Key，无法修复")
        return
    field_name = config.get("target_field", "引用")
    fixed = 0
    skipped = 0
    failed = 0
    for card_id in selected_ids:
        card = mw.col.get_card(card_id)
        note = card.note()
        if field_name not in note.keys():
            failed += 1
            continue
        content = note[field_name]
        existing_prons = find_existing_pronunciations(content)
        if not existing_prons:
            skipped += 1
            continue
        # 检查每个发音文件是否存在，缺失的尝试修复
        card_fixed = False
        for pr in existing_prons:
            if check_audio_exists(pr):
                continue
            word = pr.replace(".mp3", "")
            filename, audio_bytes = fetch_pronunciation(word, api_key)
            if filename and audio_bytes:
                media_dir = mw.col.media.dir()
                filepath = os.path.join(media_dir, pr)
                with open(filepath, "wb") as f:
                    f.write(audio_bytes)
                card_fixed = True
        if card_fixed:
            fixed += 1
        else:
            skipped += 1
    utils.tooltip(
        f"批量修复完成：{fixed} 张修复成功，"
        f"{skipped} 张无需修复，"
        f"{failed} 张失败"
    )


def repair_script_batch():
    """批量修复选中卡片：为旧版单词按钮添加 inline onclick，移除残留 script"""
    try:
        browser = dialogs._dialogs.get("Browser")
        if not browser:
            utils.tooltip("请在浏览器中使用批量修复")
            return
        browser = browser[1]
    except Exception:
        utils.tooltip("请在浏览器中使用批量修复")
        return
    if not browser:
        utils.tooltip("请在浏览器中使用批量修复")
        return
    selected_ids = browser.selectedCards()
    if not selected_ids:
        utils.tooltip("请先选中要修复的卡片")
        return
    field_name = get_config().get("target_field", "引用")
    fixed = 0
    skipped = 0
    failed = 0
    for card_id in selected_ids:
        card = mw.col.get_card(card_id)
        note = card.note()
        if field_name not in note.keys():
            failed += 1
            continue
        content = note[field_name]
        has_word_btns = 'anki-play-' in content
        if not has_word_btns:
            skipped += 1
            continue
        # 检查是否已有 inline onclick
        word_btn_has_onclick = bool(re.search(
            r'id="anki-play-[^"]*"\s+onclick="', content))
        has_old_script = '<script>' in content
        if word_btn_has_onclick and not has_old_script:
            skipped += 1
            continue
        # 1. 移除所有旧 script
        pattern_script = re.compile(r'<script>.*?</script>', re.DOTALL)
        content = pattern_script.sub('', content)
        # 2. 为没有 onclick 的旧版按钮添加 inline onclick
        def _add_onclick(m):
            btn = m.group(0)
            if 'onclick=' in btn:
                return btn
            id_match = re.search(r'id="anki-play-([^"]+)"', btn)
            if not id_match:
                return btn
            word = id_match.group(1)
            onclick = (
                f"var v=+(localStorage.getItem('anki-sender-vol')||'0.8');"
                f"var a=new Audio('{word}.mp3');a.volume=v;a.play();"
            )
            return btn.replace('>', f' onclick="{onclick}">', 1)
        content = re.sub(
            r'<button\s+id="anki-play-[^"]*"[^>]*>.*?</button>',
            _add_onclick, content, flags=re.DOTALL)
        # 3. 为旧版音量滑块添加 oninput
        if 'anki-vol' in content and 'oninput=' not in content:
            content = re.sub(
                r'(<input\s+id="anki-vol"\s+type="range"[^>]*?)(>)',
                r'\1 oninput="var v=this.value/100;'
                r"document.getElementById('anki-vol-val').textContent=v.toFixed(1);"
                r"localStorage.setItem('anki-sender-vol',v);\"\2",
                content)
        note[field_name] = content
        mw.col.update_note(note)
        fixed += 1
    utils.tooltip(
        f"批量修复完成：{fixed} 张修复成功，"
        f"{skipped} 张无需修复，"
        f"{failed} 张失败"
    )

class SettingsDialog(QDialog):
    def __init__(self):
        super().__init__(mw)
        self.setWindowTitle("发音补丁 — 设置")
        self.setMinimumWidth(500)
        config = get_config()
        layout = QVBoxLayout()

        api_group = QGroupBox("Merriam-Webster API Key")
        api_layout = QHBoxLayout()
        self.api_input = QLineEdit(config.get("api_key", ""))
        self.api_input.setPlaceholderText("免费注册获取：dictionaryapi.com")
        api_layout.addWidget(QLabel("API Key："))
        api_layout.addWidget(self.api_input)
        api_group.setLayout(api_layout)
        layout.addWidget(api_group)

        import_btn = QPushButton("从 anki-sender 插件导入 API Key")
        import_btn.clicked.connect(self.import_from_obsidian)
        layout.addWidget(import_btn)

        xiaomi_group = QGroupBox("小米 MiMo API Key（整句发音）")
        xiaomi_layout = QHBoxLayout()
        self.xiaomi_input = QLineEdit(config.get("xiaomi_api_key", ""))
        self.xiaomi_input.setPlaceholderText("platform.xiaomimimo.com 获取")
        xiaomi_layout.addWidget(QLabel("API Key："))
        xiaomi_layout.addWidget(self.xiaomi_input)
        test_xiaomi_btn = QPushButton("测试")
        test_xiaomi_btn.setAutoDefault(False)
        test_xiaomi_btn.clicked.connect(self.test_xiaomi_key)
        xiaomi_layout.addWidget(test_xiaomi_btn)
        self.xiaomi_test_label = QLabel("")
        xiaomi_layout.addWidget(self.xiaomi_test_label)
        xiaomi_group.setLayout(xiaomi_layout)
        layout.addWidget(xiaomi_group)

        field_group = QGroupBox("默认设置")
        field_layout = QFormLayout()
        self.field_input = QLineEdit(config.get("target_field", "引用"))
        field_layout.addRow("默认写入字段：", self.field_input)
        self.shortcut_input = QLineEdit(config.get("shortcut", "Ctrl+Shift+F"))
        field_layout.addRow("快捷键：", self.shortcut_input)
        field_group.setLayout(field_layout)
        layout.addWidget(field_group)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        save_btn = QPushButton("保存")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.on_save)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def test_xiaomi_key(self):
        """测试小米 MiMo API Key 是否可用"""
        key = self.xiaomi_input.text().strip()
        if not key:
            self.xiaomi_test_label.setText("❌ 请先输入 API Key")
            return
        self.xiaomi_test_label.setText("测试中...")
        QApplication.processEvents()
        filename, audio_bytes, error = fetch_tts("Hello, this is a test.", key)
        if error:
            self.xiaomi_test_label.setText(f"❌ {error[:60]}")
        else:
            self.xiaomi_test_label.setText("✅ 可用")

    def import_from_obsidian(self):
        possible_paths = [
            "D:/software/个人笔记/.obsidian/plugins/anki-sender/data.json",
            os.path.expanduser("~/Documents/个人笔记/.obsidian/plugins/anki-sender/data.json"),
            os.path.expanduser("~/Desktop/个人笔记/.obsidian/plugins/anki-sender/data.json"),
        ]
        for path in possible_paths:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    key = data.get("dictionaryApiKey", "")
                    if key:
                        self.api_input.setText(key)
                        utils.tooltip("已导入 API Key")
                        return
                except Exception:
                    pass
        utils.tooltip("未找到 anki-sender 配置文件，请手动输入 API Key")

    def on_save(self):
        config = get_config()
        config["api_key"] = self.api_input.text().strip()
        config["xiaomi_api_key"] = self.xiaomi_input.text().strip()
        config["target_field"] = self.field_input.text().strip() or "引用"
        config["shortcut"] = self.shortcut_input.text().strip() or "Ctrl+Shift+F"
        write_config(config)
        utils.tooltip("设置已保存")
        self.accept()


# ============================================================
# 菜单和快捷键注册
# ============================================================

def setup_menu():
    try:
        menu = mw.form.menuTools.addMenu("发音补丁")
        menu.addAction("添加发音（当前卡片）", add_pronunciation_single)
        menu.addAction("批量添加发音", add_pronunciation_batch)
        menu.addAction("批量修复发音", repair_pronunciation_batch)
        menu.addAction("批量修复脚本", repair_script_batch)
        menu.addSeparator()
        menu.addAction("设置", lambda: SettingsDialog().exec())
    except Exception:
        pass  # mw 未就绪时静默跳过，等 hook 触发时再注册


def on_shortcuts(shortcuts):
    config = get_config()
    shortcut = config.get("shortcut", "Ctrl+Alt+F")
    shortcuts.append((shortcut, add_pronunciation_single))


def setup_shortcut():
    """在主窗口注册全局快捷键"""
    config = get_config()
    shortcut = config.get("shortcut", "Ctrl+Alt+F")
    sc = QShortcut(QKeySequence(shortcut), mw)
    sc.activated.connect(add_pronunciation_single)


def on_browser_menu(browser):
    """Browser 顶部 Edit 菜单"""
    config = get_config()
    shortcut = config.get("shortcut", "Ctrl+Alt+F")
    menu = browser.form.menuEdit.addMenu("发音补丁")
    action = menu.addAction("添加发音")
    action.setShortcut(QKeySequence(shortcut))
    action.triggered.connect(add_pronunciation_single)
    menu.addAction("批量添加发音", add_pronunciation_batch)
    menu.addAction("批量修复发音", repair_pronunciation_batch)
    menu.addAction("批量修复脚本", repair_script_batch)


def on_browser_context_menu(browser, menu):
    """Browser 右键菜单"""
    menu.addSeparator()
    menu.addAction("添加发音", add_pronunciation_single)
    menu.addAction("批量添加发音", add_pronunciation_batch)
    menu.addAction("批量修复发音", repair_pronunciation_batch)
    menu.addAction("批量修复脚本", repair_script_batch)


# ============================================================
# 插件入口（模块级别注册 hooks，兼容不同 Anki 版本）
# ============================================================

# 尝试注册各种 hooks，跳过不存在的
try:
    gui_hooks.main_window_did_init.append(setup_menu)
    gui_hooks.main_window_did_init.append(setup_shortcut)
except AttributeError:
    # 旧版 Anki 没有 main_window_did_init，直接调用
    setup_menu()
    setup_shortcut()

try:
    gui_hooks.reviewer_did_init_shortcuts.append(on_shortcuts)
except AttributeError:
    pass

try:
    gui_hooks.browser_menus_did_init.append(on_browser_menu)
except AttributeError:
    pass

try:
    gui_hooks.browser_will_show_context_menu.append(on_browser_context_menu)
except AttributeError:
    pass
