"""工作区/代码扫描共享的文件过滤常量与文本读取。

原先在 code_analysis / code_search / project_indexing / chat 路由各自维护，
已经出现漂移（例如 chat 的工作区白名单缺 java/go/rust 等扩展）。收敛到一处。
"""

from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".next",
    ".nuxt",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

TEXT_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".java",
    ".go",
    ".rs",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".php",
    ".rb",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".sql",
    ".sh",
    ".ps1",
    ".bat",
    ".cmd",
    ".html",
    ".css",
    ".scss",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".md",
    ".mdx",
    ".txt",
    ".dockerfile",
}

TEXT_FILENAMES = {
    "Dockerfile",
    "Makefile",
    "README",
    "LICENSE",
    "package.json",
    "requirements.txt",
    "pyproject.toml",
}


def is_text_candidate(path: Path) -> bool:
    if path.name in TEXT_FILENAMES:
        return True
    suffix = path.suffix.lower()
    return suffix in TEXT_EXTENSIONS or path.name.lower().endswith("dockerfile")


def read_text_file(path: Path, max_bytes: int = 300_000) -> str | None:
    """读取文本文件；超限/二进制/解码失败返回 None（调用方自行决定回退）。"""
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return None
    if not data or len(data) > max_bytes or b"\x00" in data[:4096]:
        return None
    for encoding in ["utf-8-sig", "utf-8", "gb18030", "latin-1"]:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None
