#!/usr/bin/env python3
"""
Multi-format File Converter for LLM Wiki.

Converts non-markdown files in 00-INBOX/ to markdown for compilation.
Uses Microsoft MarkItDown as the unified conversion engine.

Supported formats: PDF, DOCX, PPTX, XLSX, EPUB, HTML, CSV, images (OCR),
                   audio/video (metadata only, transcription requires [audio-transcription] extra).

Usage:
    python scripts/convert.py <input_file>              # convert single file, output to stdout
    python scripts/convert.py <input_file> -o <output>   # convert and write to file
    python scripts/convert.py --inbox                    # convert all non-md files in 00-INBOX/
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

import requests

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import REPO_ROOT, INBOX_DIR, read_file, write_file

logger = logging.getLogger(__name__)

# ── Proxy config ───────────────────────────────────────────────────────
# Used by Google Translate API requests. Adjust if needed.
_PROXY = "http://127.0.0.1:7897"

# ── LLM config (for terminology verification) ─────────────────────────
# Uses OpenAI-compatible API. Configure via environment variables:
#   LLM_API_BASE  — API base URL (default: https://api.openai.com/v1)
#   LLM_API_KEY   — API key
#   LLM_MODEL     — Model name (default: gpt-4o-mini)
#
# 自动回退：环境变量未设置时，从 Hermes config.yaml / auth.json + .env 读取
# 当前 provider 配置（统一逻辑见 llm_api.py，换 provider 无需改本脚本）
import os as _os
from llm_api import load_llm_config as _load_hermes_config
from llm_api import resolve_api_model as _resolve_api_model
from llm_api import call_llm as _call_llm

_hermes_base, _hermes_key, _hermes_model = _load_hermes_config()

_LLM_API_BASE = _os.environ.get("LLM_API_BASE", "") or _hermes_base or "https://api.openai.com/v1"
_LLM_API_KEY = _os.environ.get("LLM_API_KEY", "") or _hermes_key
_LLM_MODEL = _os.environ.get("LLM_MODEL", "") or _hermes_model or "gpt-4o-mini"


# 解析后的真实 API 模型名（供 /chat/completions 调用使用）
_LLM_API_MODEL = _resolve_api_model(_LLM_MODEL, _LLM_API_BASE)


# ── English detection & translation ────────────────────────────────────

def is_likely_english(text: str, threshold: float = 0.7, min_chars: int = 50) -> bool:
    """Check if text is predominantly English.

    Args:
        text: The text to check.
        threshold: Minimum ratio of ASCII alpha chars to be considered English.
        min_chars: Minimum non-whitespace chars to make a reliable judgment.
                   Short texts are unreliable and default to False.
    """
    stripped = text.strip()
    non_ws = len(stripped.replace(" ", ""))
    if non_ws < min_chars:
        return False
    ascii_alpha = sum(1 for c in stripped if c.isascii() and c.isalpha())
    return ascii_alpha / max(non_ws, 1) >= threshold


def translate_to_chinese(text: str, chunk_size: int = 4500) -> str | None:
    """Translate English text to Chinese using Google Translate free API.

    Splits long text into chunks to stay within URL length limits.
    Returns translated text, or None on failure.
    """
    if not text.strip():
        return None

    chunks = _split_text(text, chunk_size)
    translated_parts = []

    for chunk in chunks:
        try:
            url = "https://translate.googleapis.com/translate_a/single"
            params = {
                "client": "gtx",
                "sl": "en",
                "tl": "zh-CN",
                "dt": "t",
                "q": chunk,
            }
            resp = requests.get(
                url,
                params=params,
                proxies={"http": _PROXY, "https": _PROXY},
                timeout=30,
            )
            if resp.status_code == 200:
                result = resp.json()
                translated = "".join(item[0] for item in result[0] if item[0])
                translated_parts.append(translated)
            else:
                logger.warning("Google Translate returned status %d", resp.status_code)
                return None
        except Exception as e:
            logger.warning("Translation failed: %s", e)
            return None

    return "\n\n".join(translated_parts)


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split text into chunks at paragraph boundaries, respecting max_chars."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) + 2 > max_chars and current:
            chunks.append(current.strip())
            current = para
        else:
            current = current + "\n\n" + para if current else para

    if current.strip():
        chunks.append(current.strip())

    # If a single paragraph exceeds max_chars, split by sentences
    final = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final.append(chunk)
        else:
            sentences = chunk.replace(". ", ".\n").split("\n")
            sub = ""
            for s in sentences:
                if len(sub) + len(s) + 1 > max_chars and sub:
                    final.append(sub.strip())
                    sub = s
                else:
                    sub = sub + " " + s if sub else s
            if sub.strip():
                final.append(sub.strip())

    return final


# ── Terminology extraction & verification (LLM-assisted) ──────────────

def extract_terms(text: str, max_terms: int = 40) -> dict[str, str] | None:
    """Extract technical terms from English text and suggest Chinese translations.

    Returns {english_term: suggested_chinese} or None on failure.
    Uses a small LLM call — input is the original text, output is a compact term list.
    """
    if not _LLM_API_KEY:
        logger.info("No LLM_API_KEY set, skipping terminology extraction")
        return None

    # Trim text to keep the LLM call small — first 3000 chars is enough for term extraction
    sample = text[:3000]

    prompt = (
        "Extract up to {max_terms} technical/specialized terms from the following English text. "
        "For each term, provide the recommended Chinese translation used in professional contexts. "
        "Output ONLY a JSON object, no explanation. Example: {{\"attention mechanism\": \"注意力机制\", \"gradient descent\": \"梯度下降\"}}\n\n"
        "{sample}"
    ).format(max_terms=max_terms, sample=sample)

    try:
        content = _call_llm(prompt, max_tokens=1024, timeout=30)
        if not content:
            logger.warning("LLM term extraction failed: empty response")
            return None

        # Extract JSON from response (may have markdown code block wrapper)
        json_match = re.search(r"\{[\s\S]*\}", content)
        if not json_match:
            logger.warning("LLM term extraction: no JSON found in response")
            return None

        import json
        terms = json.loads(json_match.group())
        logger.info("Extracted %d technical terms", len(terms))
        return terms

    except Exception as e:
        logger.warning("LLM term extraction error: %s", e)
        return None


def verify_terms(translated_text: str, terms: dict[str, str]) -> dict[str, str]:
    """Check translated text against the terminology table, fix mistranslations.

    Returns {wrong_chinese: correct_chinese} for terms that were mistranslated.
    """
    fixes = {}
    for en_term, zh_correct in terms.items():
        # Check if the correct term appears in the translation
        if zh_correct in translated_text:
            continue  # Correctly translated, no fix needed

        # The term might be translated incorrectly — try to find what Google used
        # We ask the LLM to identify the wrong translation in context
        # But to keep it lightweight, we do a simple search:
        # If the correct term is NOT in the text, Google likely mistranslated it
        fixes[en_term] = zh_correct

    if not fixes:
        return {}

    # Ask LLM to locate and fix only the wrong terms (small context)
    if not _LLM_API_KEY:
        return {}

    # Build a compact prompt with only the wrong terms
    terms_str = "\n".join(f"- {en} → {zh}" for en, zh in fixes.items())
    # Find surrounding context for each wrong term in the translated text
    # We only send a small snippet around each potentially wrong translation
    prompt = (
        "In the Chinese translation below, the following English terms were NOT translated correctly. "
        "Find where each term was wrongly translated and tell me the wrong Chinese phrase used. "
        "Output ONLY a JSON object mapping each wrong Chinese phrase to the correct one. "
        "If a term doesn't appear in the text at all, skip it.\n\n"
        "Terms to fix:\n{terms_str}\n\n"
        "Translated text (excerpt):\n{excerpt}"
    ).format(
        terms_str=terms_str,
        excerpt=translated_text[:2000],  # Keep excerpt small
    )

    try:
        content = _call_llm(prompt, max_tokens=512, timeout=30)
        if not content:
            return fixes  # Return raw fixes without precise mapping

        json_match = re.search(r"\{[\s\S]*\}", content)
        if not json_match:
            return fixes

        import json
        return json.loads(json_match.group())

    except Exception as e:
        logger.warning("LLM term verification error: %s", e)
        return fixes


def apply_term_fixes(text: str, fixes: dict[str, str]) -> str:
    """Replace wrong Chinese translations with correct terminology."""
    for wrong, correct in fixes.items():
        text = text.replace(wrong, correct)
    return text

# Supported extensions (MarkItDown handles all of these)
MARKITDOWN_EXTENSIONS = {
    ".pdf", ".docx", ".pptx", ".xlsx",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".epub", ".html", ".htm", ".csv",
    ".wav", ".mp3", ".m4a", ".mp4",
}

# Extensions that MarkItDown handles but only extracts metadata (no text content)
METADATA_ONLY_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4"}

# Fallback: Tesseract OCR for images when MarkItDown OCR fails
def _find_tesseract() -> str | None:
    """定位 tesseract：config.yaml > 系统 PATH > 常见安装位置（跨平台）。"""
    import shutil
    # 1. 显式配置 config.paths.tesseract
    try:
        from config import get_path as _cfg_path
        cfg = _cfg_path("paths.tesseract")
        if cfg and Path(str(cfg)).exists():
            return str(cfg)
    except Exception:
        pass
    # 2. 系统 PATH
    if shutil.which("tesseract"):
        return "tesseract"
    # 3. 常见安装位置
    for p in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/opt/homebrew/bin/tesseract",
    ]:
        if Path(p).exists():
            return p
    return None

_TESSERACT_CMD = _find_tesseract()


def _tesseract_ocr(path: Path) -> str | None:
    """Fallback OCR using Tesseract. Returns extracted text or None."""
    if not _TESSERACT_CMD:
        return None
    try:
        import pytesseract
        from PIL import Image

        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_CMD
        img = Image.open(str(path))
        return pytesseract.image_to_string(img, lang="chi_sim+eng").strip()
    except Exception:
        return None


def convert_file(path: Path) -> str:
    """Convert a file to markdown using MarkItDown.

    Falls back to Tesseract OCR for images, or plain text read for text files.
    """
    ext = path.suffix.lower()

    # Text formats — no conversion needed, just read
    if ext in (".md", ".txt"):
        return read_file(path)

    # MarkItDown supported formats
    if ext in MARKITDOWN_EXTENSIONS:
        try:
            from markitdown import MarkItDown
            md_converter = MarkItDown()
            result = md_converter.convert(str(path))
            content = result.text_content.strip() if result.text_content else ""

            if not content:
                # MarkItDown returned empty — try fallbacks
                if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                    return _convert_image_fallback(path)
                if ext in METADATA_ONLY_EXTENSIONS:
                    return _convert_media_placeholder(path)
                return f"[ERROR: MarkItDown produced empty output for {path.name}]\n\nSource: {path.name}"

            # For images: if MarkItDown only got metadata (no OCR text), try Tesseract
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                # Check if content is just metadata (no actual text extracted)
                if len(content) < 100 and "Transcript" not in content:
                    tesseract_text = _tesseract_ocr(path)
                    if tesseract_text:
                        content = f"## OCR 识别文本\n\n{tesseract_text}\n\n## 原始图片\n\n![{path.stem}]({path.name})"

            # Add source header
            header = f"# {path.stem}\n\n> Source: {path.name}\n\n"
            return header + content

        except ImportError:
            # MarkItDown not installed — try individual fallbacks
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                return _convert_image_fallback(path)
            return f"[ERROR: MarkItDown not installed. Run: pip install \"markitdown[pdf,docx,pptx,xlsx]\"]\n\nSource: {path.name}"
        except Exception as e:
            # MarkItDown failed — try fallbacks
            if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                fallback = _convert_image_fallback(path)
                if not fallback.startswith("[ERROR"):
                    return fallback
            return f"[ERROR: Conversion failed for {path.name}: {e}]\n\nSource: {path.name}"

    # Unknown format — try reading as text
    try:
        return read_file(path)
    except Exception:
        return f"[ERROR: Unsupported format: {ext}]\n\nSource: {path.name}"


def _convert_image_fallback(path: Path) -> str:
    """Fallback image conversion using Tesseract OCR."""
    ocr_text = _tesseract_ocr(path)

    sections = []
    if ocr_text:
        sections.append(f"## OCR 识别文本\n\n{ocr_text}")
    else:
        sections.append("## OCR 识别结果\n\n（图片中未识别到文字内容）")

    sections.append(f"## 原始图片\n\n![{path.stem}]({path.name})")
    header = f"# {path.stem}\n\n> Source: {path.name} (image)\n\n"
    return header + "\n\n".join(sections)


def _convert_media_placeholder(path: Path) -> str:
    """Placeholder for audio/video files (metadata only, no transcription)."""
    ext = path.suffix.lower()
    media_type = "audio" if ext in {".wav", ".mp3", ".m4a"} else "video"
    return (
        f"# {path.stem}\n\n"
        f"> Source: {path.name} ({media_type})\n\n"
        f"⚠️ {media_type.capitalize()} file — transcription not yet supported.\n"
        f"Use `#research` with YouTube Full skill for video subtitles, "
        f"or a Whisper/SenseVoice API for local audio transcription.\n"
    )


def convert_inbox() -> list[dict]:
    """Convert all non-markdown files in 00-INBOX/ to markdown.

    After conversion, detects English .md files and translates them to Chinese.
    Returns list of {input, output, status} dicts.
    """
    if not INBOX_DIR.exists():
        return []

    results = []
    for item in sorted(INBOX_DIR.iterdir()):
        if item.is_dir():
            continue
        ext = item.suffix.lower()
        if ext in (".md", ".txt"):
            continue  # Already text, no conversion needed

        # Convert and save as .md alongside original
        output_path = item.with_suffix(".md")
        if output_path.exists():
            results.append({"input": item.name, "output": output_path.name, "status": "skipped_exists"})
            continue

        try:
            md_content = convert_file(item)
            write_file(output_path, md_content)
            results.append({"input": item.name, "output": output_path.name, "status": "converted"})
        except Exception as e:
            results.append({"input": item.name, "output": "", "status": f"error: {e}"})

    # ── Translate English .md files ────────────────────────────────────
    for item in sorted(INBOX_DIR.iterdir()):
        if item.is_dir():
            continue
        if item.suffix.lower() not in (".md", ".txt"):
            continue

        content = read_file(item)
        if not is_likely_english(content):
            continue

        # Skip if already translated (has a .zh-cn.md counterpart)
        zh_path = item.with_suffix(".zh-cn.md")
        if zh_path.exists():
            results.append({"input": item.name, "output": zh_path.name, "status": "skipped_translated"})
            continue

        logger.info("Translating English file: %s", item.name)

        # Step 1: Extract technical terms from original (lightweight LLM call)
        terms = extract_terms(content)

        # Step 2: Google Translate full text
        translated = translate_to_chinese(content)
        if not translated:
            results.append({"input": item.name, "output": "", "status": "translation_failed"})
            logger.warning("Translation failed for: %s", item.name)
            continue

        # Step 3: Verify terminology and fix mistranslations (lightweight LLM call)
        if terms:
            fixes = verify_terms(translated, terms)
            if fixes:
                translated = apply_term_fixes(translated, fixes)
                logger.info("Applied %d terminology fixes for %s", len(fixes), item.name)

        # Step 4: Preserve YAML frontmatter, translate title
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                frontmatter = content[:end + 3]
                # Translate the title field in frontmatter
                title_match = re.search(
                    r'^(title:\s*["\']?)(.+?)(["\']?)\s*$', frontmatter, re.MULTILINE
                )
                if title_match:
                    orig_title = title_match.group(2)
                    zh_title = translate_to_chinese(orig_title)
                    if zh_title:
                        frontmatter = frontmatter.replace(
                            title_match.group(0),
                            f"{title_match.group(1)}{zh_title}{title_match.group(3)}",
                        )
                translated = frontmatter + "\n\n" + translated

        write_file(zh_path, translated)
        results.append({"input": item.name, "output": zh_path.name, "status": "translated"})
        logger.info("Translated: %s → %s", item.name, zh_path.name)

    return results


def cleanup_translations() -> int:
    """Remove .zh-cn.md files from INBOX that have been compiled.

    A .zh-cn.md is safe to delete if its corresponding summary exists
    in wiki/summaries/ (meaning it was already compiled).
    Returns the number of files cleaned up.
    """
    if not INBOX_DIR.exists():
        return 0

    cleaned = 0
    for zh_file in INBOX_DIR.glob("*.zh-cn.md"):
        # The original English file should have been archived to raw/ already
        # after compilation. If the .zh-cn.md still exists in INBOX,
        # it means compilation is done (summary is in wiki/summaries/).
        # We check if the original file is no longer in INBOX as a signal.
        original = INBOX_DIR / zh_file.name.replace(".zh-cn.md", ".md")
        if not original.exists():
            # Original already moved to raw/ → compilation done → safe to delete
            zh_file.unlink()
            logger.info("Cleaned up translation: %s", zh_file.name)
            cleaned += 1
        else:
            # Original still in INBOX → not yet compiled → keep .zh-cn.md
            logger.debug("Keeping translation (original still in INBOX): %s", zh_file.name)

    return cleaned

def main():
    parser = argparse.ArgumentParser(description="Multi-format File Converter for LLM Wiki")
    parser.add_argument("input", nargs="?", help="Input file to convert")
    parser.add_argument("-o", "--output", help="Output file path (default: stdout)")
    parser.add_argument("--inbox", action="store_true",
                        help="Convert all non-md files in 00-INBOX/")
    parser.add_argument("--cleanup-translations", action="store_true",
                        help="Remove .zh-cn.md files from INBOX after compilation")
    args = parser.parse_args()

    if args.cleanup_translations:
        cleaned = cleanup_translations()
        if cleaned:
            print(f"🧹 Cleaned up {cleaned} translation file(s) from INBOX")
        else:
            print("✅ No translation files to clean up")
        return

    if args.inbox:
        results = convert_inbox()
        if not results:
            print("✅ No non-markdown files found in 00-INBOX/")
        else:
            converted = sum(1 for r in results if r["status"] == "converted")
            translated = sum(1 for r in results if r["status"] == "translated")
            failed = sum(1 for r in results if "error" in r["status"] or "failed" in r["status"])
            print(f"🔄 Converted {converted} file(s), translated {translated} English file(s)")
            if failed:
                print(f"⚠️  {failed} file(s) had issues")
            for r in results:
                print(f"  {r['input']} → {r['output']} ({r['status']})")
        return

    if not args.input:
        parser.print_help()
        return

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    md = convert_file(input_path)
    if args.output:
        write_file(Path(args.output), md)
        print(f"✅ Converted: {input_path} → {args.output}")
    else:
        print(md)


if __name__ == "__main__":
    main()
