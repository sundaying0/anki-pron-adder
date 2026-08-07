# Anki 发音补丁插件

> 为 Anki 卡片添加美式发音按钮，点击播放，与 anki-sender 风格完全一致。

---

## 一、需求概述

### 核心功能

| 功能 | 说明 |
|------|------|
| 添加发音 | 输入单词（支持空格分隔多个），从 Merriam-Webster API 下载美式发音 |
| 点击播放 | HTML `<button>` + `<script>` 实现，不使用 Anki 原生 `[sound:]` 自动播放 |
| 试听预览 | 添加前可逐个试听已查询的发音 |
| 编辑管理 | 对已有发音支持删除、修改（替换单词）、修复缺失音频 |
| 批量模式 | 在 Browser 中选中多张卡片，逐张确认添加（跳过已有发音） |
| 脚本检测 | 自动检测播放脚本是否正常，支持一键修复 |
| 并行查询 | 多单词同时查询，本地缓存加速重复查询 |

### 使用场景

1. **单张卡片**：复习时按快捷键，弹窗输入单词，添加发音
2. **批量处理**：在 Browser 中选中多张卡片，批量添加发音（跳过已有发音的卡片）
3. **编辑已有**：删除发音、修改发音对应的单词、修复缺失的音频文件
4. **脚本修复**：卡片发音按钮不响应时，自动检测并修复播放脚本

### 快捷键

- 默认：`Ctrl+Alt+F`（可在插件设置中自定义）
- Reviewer 和 Browser 中均可用

---

## 二、实现方式

### 2.1 技术栈

- **语言**：Python 3（Anki 内置）
- **GUI**：PyQt5（Anki 自带）
- **API**：Merriam-Webster Collegiate Dictionary API
- **音频格式**：API 返回 WAV，保存为 `.mp3` 文件名（Anki 媒体库兼容）
- **并发**：`concurrent.futures.ThreadPoolExecutor` 并行查询多个单词
- **缓存**：本地文件缓存（`~/.anki-pron-cache/`），避免重复 API 调用

### 2.2 播放机制

**不使用 Anki 原生 `[sound:]` 语法**（会自动播放），而是使用 HTML 按钮：

```html
<div style="margin-top:8px;">
  <button id="anki-play-word1" style="...">🔊 word1</button>
  <button id="anki-play-word2" style="...">🔊 word2</button>
  <span>🔈</span>
  <input id="anki-vol" type="range" min="0" max="100" value="80">
  <span id="anki-vol-val">0.8</span>
  <script>
    (function(){
      function init(){
        var v = +(localStorage.getItem('anki-sender-vol') || '0.8');
        // ... 音量控制 + 点击播放逻辑
      }
      if(document.readyState==='loading'){
        document.addEventListener('DOMContentLoaded',init);
      }else{
        init();
      }
    })();
  </script>
</div>
```

**关键点**：
- 所有按钮共享一个音量滑块
- 音量通过 `localStorage` 持久化，与 anki-sender 共享
- 脚本中 `new Audio('word.mp3')` 播放媒体库中的文件
- DOM 加载检测：防止脚本在 DOM 未就绪时执行

### 2.3 对话框工作流

| 按钮 | 功能 | 是否关闭窗口 |
|------|------|-------------|
| 查询发音 | 从 MW API 查询并缓存发音 | 否 |
| 试听 | 播放已查询的发音 | 否 |
| 添加发音 | 将发音写入字段（可多次添加） | 否 |
| 保存 | 保存到数据库 | 否 |
| 修复脚本 | 移除旧脚本，插入新版本 | 否 |
| 刷新 | 重新检测已有发音和脚本状态 | 否 |
| X 关闭 | 未保存时恢复原始内容，关闭 | 是 |

### 2.4 文件结构

```
anki-pron-adder/
├── __init__.py      # 主插件文件（所有逻辑）
├── config.json      # 默认配置
├── manifest.json    # Anki 插件清单
└── README.md        # 本文档
```

### 2.5 Hook 注册

```python
# 主窗口初始化
gui_hooks.main_window_did_init.append(setup_menu)
gui_hooks.main_window_did_init.append(setup_shortcut)

# Reviewer 快捷键（备用）
gui_hooks.reviewer_did_init_shortcuts.append(on_shortcuts)

# Browser 菜单
gui_hooks.browser_menus_did_init.append(on_browser_menu)

# Browser 右键菜单
gui_hooks.browser_will_show_context_menu.append(on_browser_context_menu)
```

### 2.6 核心函数

| 函数 | 作用 |
|------|------|
| `extract_word(text)` | 从卡片内容提取单词（支持 `word /phonetic/` 和纯单词格式） |
| `fetch_pronunciation(word, api_key)` | 调用 MW API 下载发音音频（带本地缓存） |
| `build_pronunciation_html(entries)` | 生成按钮 + 音量滑块 + 脚本的 HTML |
| `find_existing_pronunciations(text)` | 查找字段中已有的发音标记 |
| `remove_pronunciation(text, filename)` | 移除特定发音的标记（保留其他） |
| `play_audio_file(filename)` | 播放媒体库中的音频文件 |
| `check_audio_exists(filename)` | 检查音频文件是否存在于 Anki 媒体库 |

---

## 三、配置说明

### config.json

```json
{
    "api_key": "your-mw-api-key",
    "target_field": "引用",
    "shortcut": "Ctrl+Alt+F"
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `api_key` | Merriam-Webster API Key | 空（需手动配置） |
| `target_field` | 发音写入的字段名 | `引用` |
| `shortcut` | 快捷键 | `Ctrl+Alt+F` |

### 获取 API Key

1. 访问 https://dictionaryapi.com/
2. 注册免费账户
3. 创建一个 Reference API Key
4. 复制 Key 到插件设置中

---

## 四、安装方式

### 方法一：直接复制

1. 将 `anki-pron-adder/` 整个文件夹复制到：
   - Windows: `%APPDATA%\Anki2\addons21\2243789015\`
   - macOS: `~/Library/Application Support/Anki2/addons21/2243789015/`
   - Linux: `~/.local/share/Anki2/addons21/2243789015/`
2. 重启 Anki

### 方法二：开发模式

```bash
# 同步到 Anki 插件目录
cp __init__.py config.json manifest.json "$APPDATA/Anki2/addons21/2243789015/"

# 清除缓存
rm -rf "$APPDATA/Anki2/addons21/2243789015/__pycache__"
```

---

## 五、常见问题与易错点

### 5.1 插件加载失败

**现象**：Anki 启动时提示"插件加载失败"

**原因**：
- `manifest.json` 中 `min_point_version` 版本过高
- 使用了相对导入（`from .module import ...`）
- 语法错误或缺少依赖

**解决**：
- `manifest.json` 只保留 `package` 和 `name` 字段
- 所有代码合并到单个 `__init__.py`
- 用 `try/except` 包裹可能不存在的 hook

### 5.2 发音自动播放

**现象**：打开卡片就自动播放发音，而不是点击播放

**原因**：使用了 `[sound:word.mp3]` 语法

**解决**：必须使用 HTML `<button>` + `<script>` 方式

### 5.3 音量滑块重复

**现象**：每个按钮旁边都有一个音量滑块

**原因**：`build_pronunciation_html` 为每个单词生成了独立的滑块

**解决**：函数接收 `entries: [(word, filename), ...]` 列表，只渲染一个共享滑块

### 5.4 删除发音时残留元素

**现象**：删除单词后，音量滑块或空容器还留在卡片中

**原因**：`remove_pronunciation` 只删除了按钮，没有删除外层 `<div>` 容器

**解决**：
- 删除特定按钮后，检查是否还有剩余按钮
- 如果没有剩余，移除整个容器和脚本

```python
if 'anki-play-' not in text:
    # 移除整个容器和脚本
    pattern_container = re.compile(r'<div\s+style="margin-top:\s*8px;">.*?</div>', re.DOTALL)
    text = pattern_container.sub('', text)
```

### 5.5 删除一个发音导致全部删除

**现象**：有 2 个发音，删除 1 个后保存，2 个都没了

**原因**：`remove_pronunciation` 直接删除了整个容器 div

**解决**：只删除特定按钮，保留容器和其他按钮

### 5.6 快捷键不生效

**现象**：按快捷键无反应

**原因**：
- `Ctrl+Shift+F` 与 Anki 内置搜索冲突
- `reviewer_did_init_shortcuts` hook 在某些版本不触发

**解决**：
- 使用不冲突的快捷键（如 `Ctrl+Alt+F`）
- 同时用 `QShortcut` 在主窗口注册（双重保险）

```python
# 方式一：hook（备用）
gui_hooks.reviewer_did_init_shortcuts.append(on_shortcuts)

# 方式二：QShortcut（主用）
sc = QShortcut(QKeySequence(shortcut), mw)
sc.activated.connect(add_pronunciation_single)
```

### 5.7 Browser 中无法添加发音

**现象**：在 Browser 中右键没有"添加发音"选项

**原因**：只注册了 `browser_menus_did_init`（Edit 菜单），没有注册右键菜单

**解决**：同时注册两个 hook

```python
gui_hooks.browser_menus_did_init.append(on_browser_menu)
gui_hooks.browser_will_show_context_menu.append(on_browser_context_menu)
```

### 5.8 保存按钮不显示

**现象**：对话框底部只有"取消"和"添加发音"，没有"保存"

**原因**：对话框宽度不够，按钮被挤出可视区域

**解决**：增大 `setMinimumWidth`（建议 520+）

### 5.9 配置不生效

**现象**：修改了 config.json 但插件还是用旧配置

**原因**：Anki 缓存了旧配置，或 `__pycache__` 未清除

**解决**：
- 修改代码后删除 `__pycache__` 目录
- 完全重启 Anki（不是仅重新加载插件）

### 5.10 脚本修复不持久

**现象**：点击"修复脚本" → "保存" → 关闭 → 重新打开，仍显示"脚本缺失"

**原因**：
- `card.note()` 返回的是缓存对象，`update_note` 后缓存未更新
- 重新打开对话框时读取的是缓存中的旧内容

**解决**：
- `on_save_only` 中调用 `mw.col.get_note(self.note.id)` 重新从数据库加载
- `get_current_card_and_note` 中调用 `mw.col.get_card(card.id)` 重新加载 card

### 5.11 Enter 键意外关闭对话框

**现象**：在输入框中按 Enter 键，对话框关闭

**原因**：某个按钮设置了 `setDefault(True)`，Enter 触发了默认按钮

**解决**：
- 所有按钮不设置 `setDefault(True)`
- 所有辅助按钮设置 `setAutoDefault(False)`

### 5.12 按钮焦点抢占

**现象**：点击"修改"按钮后，输入框焦点丢失，再输入时触发按钮动作

**原因**：Qt 按钮默认 `autoDefault=True`，获得焦点后成为默认按钮，Enter 触发它

**解决**：所有 modify/delete/repair 按钮设置 `setAutoDefault(False)`

### 5.13 重复发音容器

**现象**：多次添加发音后，出现多个播放区域，每个都有独立的音量滑块

**原因**：`on_add` 每次追加新的 HTML 块，而不是合并到现有块

**解决**：
- `on_add` 中先移除整个现有容器
- 收集所有保留的发音（原有 + 新增）
- 用 `build_pronunciation_html(all_entries)` 重新生成单一容器

### 5.14 脚本执行时机问题

**现象**：按钮显示正常，但点击无反应

**原因**：`<script>` 在 DOM 未完全加载时执行，`document.getElementById` 返回 null

**解决**：脚本中加入 DOM 加载检测

```javascript
if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded', init);
}else{
    init();
}
```

---

## 六、与 anki-sender 的关系

本插件与 anki-sender 独立，但播放样式完全一致：

| 特性 | anki-sender | 发音补丁 |
|------|-------------|----------|
| 触发方式 | Obsidian → AnkiConnect | Anki 内直接操作 |
| 播放按钮 | HTML button | HTML button（相同样式） |
| 音量滑块 | localStorage 持久化 | 共享同一 localStorage key |
| 音频来源 | MW API | MW API（相同） |

两者可以共存，音量设置互相共享。

---

## 七、开发备忘

### Anki 插件开发注意事项

1. **单文件优先**：Anki 的插件系统对多文件支持不佳，尽量合并到 `__init__.py`
2. **Hook 兼容**：不同 Anki 版本的 hook 可能不同，用 `try/except AttributeError` 包裹
3. **配置读写**：用 `mw.addonManager.getConfig(__name__)` 和 `writeConfig()`
4. **媒体文件**：用 `mw.col.media.write_file()` 写入，不要直接操作文件系统
5. **笔记更新**：用 `mw.col.update_note(note)` 保存，不要直接修改数据库
6. **避免缓存**：保存后用 `mw.col.get_note(note.id)` 重新加载，避免后续操作使用旧数据

### 调试方法

1. 查看 Anki 控制台：工具 → 插件 → 查看调试日志
2. 在代码中添加 `print()` 输出，查看控制台
3. 检查 `__pycache__` 是否已清除

### 测试清单

- [ ] 插件正常加载
- [ ] 快捷键在 Reviewer 中生效
- [ ] 快捷键在 Browser 中生效
- [ ] 右键菜单在 Browser 中显示
- [ ] 单词查询成功
- [ ] 多单词查询成功
- [ ] 试听播放正常
- [ ] 添加发音后按钮显示正常
- [ ] 点击按钮播放正常
- [ ] 音量滑块调节正常
- [ ] 删除发音正常（不残留）
- [ ] 删除一个不影响其他
- [ ] 修改发音正常
- [ ] 保存按钮正常工作
- [ ] 批量模式正常
- [ ] 脚本检测和修复正常
- [ ] 脚本修复后保存持久化
- [ ] 音频文件缺失修复正常
- [ ] 关闭窗口未保存时恢复原始内容
