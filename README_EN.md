<h1 align="center">MusicDownload</h1>

<p align="center">
  🇨🇳 <a href="./README.md">简体中文</a> | 🇺🇸 <a href="./README_EN.md">English</a>
</p>

<p align="center">
  <img alt="Python Version" src="https://img.shields.io/badge/Python-3.10%20~%203.14-blue.svg">
  <img alt="GUI Framework" src="https://img.shields.io/badge/GUI-PyQt5-green.svg">
  <img alt="Release Build" src="https://img.shields.io/badge/Build-PyInstaller%20%2B%20UPX-orange.svg">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows-lightgrey.svg">
  <img alt="Downloads" src="https://img.shields.io/github/downloads/waves-dylan/MusicDown/total?label=Downloads&color=brightgreen">
</p>

**MusicDownload** is a high-performance, full-featured music downloading and metadata enhancement tool built with Python and PyQt5. Based on the open-source engine [musicdl](https://github.com/CharlesPikachu/musicdl), this project features deep architectural refactoring, feature expansions, and modern UI upgrades—resolving common issues with audio matching, metadata tagging, lyrics synchronization, and executable bloat found in earlier versions.

---

### ✨ Key Features & Major Enhancements

#### 1. 🎵 17 Aggregated Music Platforms
* **Domestic Sources**: NetEase Cloud Music, QQ Music, Kugou, Kuwo, Migu Music, Soda Music, Qianqian Music, 5sing;
* **International Sources**: Apple Music, Spotify, Deezer, TIDAL, Qobuz, SoundCloud, Jamendo, Joox, StreetVoice;
* **Smart Matching for International Sources**: For metadata-only sources like Spotify, built-in duration-tolerance and title fuzzy-matching algorithms seamlessly find high-quality third-party audio streams with high precision.

#### 2. 🏷️ Atomic Audio Metadata Tagging & Cover Embedding (`audio_metadata.py`)
* **True Audio Container Detection**: Detects file formats directly from binary headers (preventing parser crashes when CDNs serve FLAC files mislabeled with `.mp3` extensions and correcting file extensions automatically);
* **Non-destructive Atomic Writes**: Tagging is performed on a temporary copy and atomically replaced only after verification passes;
* **Process Lock Retry**: Built-in exponential backoff retry handles Windows file locks (`PermissionError`) caused by antivirus software or Windows Explorer, preventing silent metadata drops;
* **Comprehensive Tag Support**: Writes Title, Artist, Album, Track Number, Embedded Lyrics (USLT / Vorbis / MP4), and High-Resolution Album Art (APIC / Picture) across MP3, FLAC, M4A, OGG, and WAV.

#### 3. 🌐 Smart Bilingual Lyrics Synchronization (`bilingual_lyric.py`)
* **Millisecond-Accurate Alignment**: Dual-pointer time-window algorithm accurately aligns foreign lyrics with Chinese translations;
* **3 Formatting Modes**:
  * **Dual Line (`dual_line`)**: Both original and translated lines share the same timestamp for clean scrolling on car dashboards and players (Poweramp, Foobar2000, Hiby Music);
  * **Inline (`inline`)**: Merges original and translated lyrics onto the same line;
  * **Translation Only (`translation_only`)**: Keeps only the translated lyrics;
* **Cross-Source Lyrics Enrichment**: Automatically detects song languages and retrieves high-quality bilingual LRC lyrics from NetEase or QQ Music.

#### 4. 🔤 Simplified & Traditional Chinese Conversion (`zh_converter.py`)
* **Flexible Conversion**: Supports "Keep Original", "Convert to Simplified Chinese", and "Convert to Traditional Chinese";
* **Progressive Fallback**: Uses `zhconv` phrase-level conversion when available, seamlessly falling back to a comprehensive built-in character map in offline or portable environments;
* **Format Protection**: Strictly protects LRC timestamps `[mm:ss.xx]`, header tags `[ti:]`, and Japanese Kana / Korean Hangul characters from unintended translation.

#### 5. 💾 User Preferences Memory & Portable Config (`config_manager.py`)
* **Pure Portable INI Storage**: Built upon `config.ini` and `QSettings(IniFormat)`, allowing zero-configuration plug-and-play on removable USB drives with off-screen protection and automatic `%APPDATA%` permission fallback;
* **Comprehensive Habit Persistence**:
  * **Downloads & Paths**: Remembers custom save directories (with a dedicated "Open Folder" button), per-source result caps, batch scopes, and auto-download toggles;
  * **Music Source Selections**: Automatically retains user-selected source combinations, offering one-click presets ("Select All", "Clear", "Mainstream Chinese", "Reset Default");
  * **Window & Table Layouts**: Automatically captures window geometry/position and table column widths on exit;
  * **Search History Autocomplete**: Built-in recent query history with fuzzy dropdown completion and one-click clear;
  * **Quick Navigation & Factory Reset**: Top menu bar enables instant access to `config.ini` in File Explorer and one-click restoration to factory defaults.

#### 6. ⚡ Modern PyQt5 UI & Full-Scenario Telemetry (Project Telemetry)
* **Dual-Mode Progress Display (Terminal Console + Desktop GUI)**:
  * **Immersive Terminal**: Natively integrates `rich`-based multi-task progress bars, multi-source search trees, real-time speed (MB/s), and precise ETA countdowns;
  * **PyQt5 Modern Task Monitor Dashboard**: Built-in `ModernProgressDialog` with per-track mini cards, rainbow progress bars, and task status capsules (Queued / Downloading / Lyric Aligning / Tagging / Done / Failed);
  * **10 FPS Safe Throttling & EWMA Smoothing**: Silky UI under gigabit burst chunks with < 1% CPU, eliminating speed readout jitter.
* **Async Album Art Thread Pool**: Generational cache with multi-threaded image processing ensures smooth scrolling across hundreds of search results;
* **Modern UI Controls**:
  * Responsive FlowLayout for music source selection chips;
  * Tri-state header checkbox for one-click selection and inversion;
  * **Press Enter to search** directly from the search box—no need to click the button;
  * Right-click context menus and double-click inspection;
  * Collapsible drawer-style live log terminal.

#### 7. 📦 Clean Packaging & Binary Optimization
* **One-Click Build Script**: Comes with `make_release.bat`, automatically leveraging the local `.venv` environment;
* **Structural Dependency Pruning**: Customized `musicdownload.spec` excludes unnecessary heavyweight libraries (`numpy`, `matplotlib`, `pandas`, `av`, `lxml`) and eliminates PyQt5's redundant 20MB `opengl32sw.dll` software renderer;
* **UPX Compression with Crash-Protection Whitelist**: Compresses the standalone executable down to ~**44 MB**, while protecting critical Qt and VC runtime libraries from corruption.

---

### 🖥️ Screenshots

<table align="center" border="0" cellpadding="10">
  <tr>
    <td align="center">
      <img src="images/1.png" width="280"><br>
      <b>Search & Main Interface</b>
    </td>
    <td align="center">
      <img src="images/2.png" width="280"><br>
      <b>Sorting Tracks by Field</b>
    </td>
    <td align="center">
      <img src="images/3.png" width="280"><br>
      <b>Progress & Live Log Drawer</b>
    </td>
  </tr>
</table>

---

### 📂 Project Structure

```text
MusicDown/
├── musicdownload.py        # Main entry point: Modern PyQt5 UI, concurrency control & business logic
├── telemetry_hub.py        # Core module: Unified telemetry event hub, EWMA rate smoothing & Rich renderer
├── telemetry_widgets.py    # UI component: PyQt5 modern multi-task dashboard, streaming cards & 10 FPS bridge
├── config_manager.py       # Preferences module: Portable INI-based settings & window state manager
├── audio_metadata.py       # Module 1: Container verification, atomic metadata & album art tagger
├── bilingual_lyric.py      # Module 2: Bilingual LRC alignment, layout restructuring & enrichment
├── zh_converter.py         # Module 3: Simplified/Traditional Chinese text conversion engine
├── musicdownload.spec      # PyInstaller spec for size optimization & isolated bundling
├── make_release.bat        # Windows one-click automated build script with UPX compression
├── requirements.txt        # Project dependencies
├── config.ini              # Portable user preferences file (auto-generated)
├── tools/                  # Build tools (contains UPX compressor for Windows)
│   └── upx/
│       └── upx.exe
└── images/                 # UI preview screenshots
```

---

### 🚀 Getting Started

#### Prerequisites
* **Python 3.10 ~ 3.14** is recommended.

#### Running from Source
```bash
# 1. Clone the repository
git clone https://github.com/waves-dylan/MusicDown.git
cd MusicDown

# 2. Create and activate a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch the application
python musicdownload.py
```

---

### 🛠️ Building Standalone Executable

The project is preconfigured with optimized packaging settings:

```bat
# Run the batch script from the project root:
.\make_release.bat
```
Once completed, the single-file binary will be placed at the project root as `musicdownload.exe`.

---

### 📄 Acknowledgements
* Core music search and download engine: [musicdl](https://github.com/CharlesPikachu/musicdl) by CharlesPikachu
* Audio metadata tagging: [mutagen](https://github.com/quodlibet/mutagen)
* Chinese conversion: [zhconv](https://github.com/zh-code/zhconv)
* GUI framework: [PyQt5](https://www.riverbankcomputing.com/software/pyqt/)

---

### ⚖️ Disclaimer
This project is developed solely for educational, technical research, and personal music organization purposes. All audio and lyrics copyrights belong to their respective platforms and creators. Please do not use this software for commercial purposes.
