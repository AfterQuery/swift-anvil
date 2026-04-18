import re
from pathlib import Path

PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)

DEFAULT_MODEL = "openrouter/anthropic/claude-sonnet-4.6"

GITHUB_PRS_DIR = Path(__file__).resolve().parent / "github_prs"
