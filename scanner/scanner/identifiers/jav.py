import re

# A JAV product code: 2-6 letters + optional dash + 2-5 digits (ABP-123, MIDE882),
# optionally followed by 1-2 letters (SNOS-189ch), or an FC2 PPV id
# (FC2-PPV-1234567). Underscores may follow the code (`MIDE-390_restored`).
JAV_CODE_RE = re.compile(
	r"\b([a-z]{2,6})-?(\d{2,5})(?:[a-z]{1,2})?(?![a-z0-9])", re.IGNORECASE
)
FC2_CODE_RE = re.compile(
	r"\bFC2[-_ ]?(?:PPV[-_ ]?)?(\d{4,8})(?![a-z0-9])", re.IGNORECASE
)
# Leading junk that may appear before a code (release groups, brackets,
# source-site prefixes like `489155.com@`...).
LEADING_JUNK_RE = re.compile(
	r"^(?:\s|\[[^\]]*\]|\([^)]*\)|【[^】]*】|〔[^〕]*〕|[\w.+-]+@)*"
)


def extract_code(title: str) -> str | None:
	"""Extract a normalized JAV product code (e.g. `ABP-123`) from a
	filename/title. Digits are zero-padded to 3 (`MIDV-041`, `CAWB-002`)."""
	m = FC2_CODE_RE.search(title)
	if m:
		return f"FC2-{int(m.group(1)):0>7}"
	m = JAV_CODE_RE.search(title)
	if m:
		return f"{m.group(1).upper()}-{int(m.group(2)):03d}"
	return None
