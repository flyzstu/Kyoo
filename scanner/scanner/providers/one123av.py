import os
import re
from datetime import datetime
from logging import getLogger
from types import TracebackType
from urllib.parse import quote_plus

from aiohttp import ClientSession, ClientTimeout
from langcodes import Language

from ..identifiers.jav import extract_code
from ..models.genre import Genre
from ..models.metadataid import MetadataId
from ..models.movie import Movie, MovieStatus, MovieTranslation, SearchMovie
from ..models.serie import SearchSerie, Serie
from ..models.staff import Person, Role, Staff
from ..models.studio import Studio, StudioTranslation
from ..utils import clean, to_slug
from .names import ProviderName
from .provider import Provider, ProviderError

logger = getLogger(__name__)

TITLE_RE = re.compile(r'<h1 class="watch__title">(.*?)</h1>', re.DOTALL)
COVER_RE = re.compile(r"background-image:url\('([^']+)'")
INFO_ROW_RE = re.compile(r"<dt>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.DOTALL)
CHIP_RE = re.compile(r'<a class="chip"[^>]*>(.*?)</a>')
CARD_RE = re.compile(
	r'<a class="card__cover" href="/\w+/v/(?P<slug>[^"]+)">.*?'
	r'<img class="card__img" src="(?P<img>[^"]+)".*?'
	r'<a class="card__link" href="/\w+/v/[^"]+">(?P<title>.*?)</a>',
	re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")

# site lang code -> (url prefix, kyoo language)
LANGS = {
	"en": "en",
	"cn": "zh-CN",
	"tw": "zh-TW",
	"ja": "ja",
}

# localized detail labels (en / zh-Hans / zh-Hant / ja)
LABELS = {
	"code": ("Code", "代码", "代碼", "コード"),
	"type": ("Type", "类型", "類型", "タイプ"),
	"release": ("Release date", "发布日期", "發布日期", "発売日"),
	"duration": ("Duration", "时长", "時長", "再生時間"),
	"cast": ("Cast", "演员", "演員", "出演者"),
	"maker": ("Maker", "制作商", "製作商", "メーカー"),
	"series": ("Series", "系列", "系列", "シリーズ"),
	"genres": ("Genres", "类别", "類別", "ジャンル"),
	"tags": ("Tags", "标签", "標籤", "タグ"),
}

# genre names (en/zh) -> Kyoo genres. Adjust to taste.
GENRE_MAP = {
	"Solowork": Genre.DRAMA,
	"Big Boobs": Genre.ROMANCE,
	"Big Tits": Genre.ROMANCE,
	"大胸部": Genre.ROMANCE,
	"Mature Woman": Genre.ROMANCE,
	"熟女": Genre.ROMANCE,
	"Married Woman": Genre.ROMANCE,
	"人妻": Genre.ROMANCE,
	"Documentary": Genre.DOCUMENTARY,
	"纪录片": Genre.DOCUMENTARY,
	"Drama": Genre.DRAMA,
	"剧情": Genre.DRAMA,
	"Romance": Genre.ROMANCE,
	"恋爱": Genre.ROMANCE,
	"Comedy": Genre.COMEDY,
	"喜剧": Genre.COMEDY,
	"Horror": Genre.HORROR,
	"恐怖": Genre.HORROR,
	"Mystery": Genre.MYSTERY,
	"悬疑": Genre.MYSTERY,
	"School Girl": Genre.DRAMA,
	"女子校生": Genre.DRAMA,
}


def _strip(html: str) -> str:
	return TAG_RE.sub("", html).strip()


def _slug_matches(slug: str, code: str) -> bool:
	"""Check a site slug refers to the given code (FC2 slugs come in
	`fc2-ppv-1234567`/`fc2_1234567` flavors)."""
	if slug == code.lower() or slug.startswith(f"{code.lower()}-"):
		return True
	if code.startswith("FC2-"):
		return re.fullmatch(
			f"fc2[-_ ]?(?:ppv[-_ ]?)?{code[4:]}", slug
		) is not None
	return False


def _parse_duration(raw: str) -> int | None:
	parts = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+)", raw.strip())
	if not parts:
		return None
	return int(parts.group(1) or 0) * 60 + int(parts.group(2))


def _parse_info(html: str) -> dict[str, list[str]]:
	"""Parse the details `<dl>`; labels are localized so match all langs."""
	block = html.split('class="watch__info"', 1)
	info: dict[str, list[str]] = {}
	if len(block) < 2:
		return info
	for dt, dd in INFO_ROW_RE.findall(block[1]):
		label = _strip(dt)
		for key, names in LABELS.items():
			if label in names:
				chips = CHIP_RE.findall(dd)
				info[key] = [_strip(x) for x in chips] if chips else [_strip(dd)]
				break
	return info


class OneTwoThreeAV(Provider):
	"""
	Provider scraping 123av.com for adult movies. No API key needed.

	`AV123_LANGS` (default `cn,ja`) lists the site languages to fetch, first
	one being the primary translation. Supported: en, cn (简体), tw (繁體),
	ja. Detail pages are parsed from HTML; if the site changes its markup,
	only the regexes at the top of this file need updating.
	"""

	def __init__(self) -> None:
		super().__init__()
		self._langs = [
			x.strip()
			for x in os.environ.get("AV123_LANGS", "cn,ja").split(",")
			if x.strip() in LANGS
		] or ["cn"]
		self._client = ClientSession(
			base_url=os.environ.get("AV123_BASE_URL", "https://123av.com/"),
			headers={
				"User-Agent": "kyoo scanner v5",
				"Accept-Language": ", ".join(self._langs),
			},
			timeout=ClientTimeout(total=30),
		)

	async def __aenter__(self):
		return self

	async def __aexit__(
		self,
		exc_type: type[BaseException] | None,
		exc_value: BaseException | None,
		traceback: TracebackType | None,
	):
		await self._client.close()

	@property
	def name(self) -> str:
		return ProviderName.AV123

	async def _get(self, path: str) -> str:
		async with self._client.get(path) as r:
			if r.status == 404:
				raise ProviderError(f"123av: not found: {path}")
			r.raise_for_status()
			return await r.text()

	def _map_details(self, pages: dict[str, str], slug: str) -> Movie:
		primary = self._langs[0]
		html = pages.get(primary) or next(iter(pages.values()))
		title = _strip(TITLE_RE.search(html).group(1)) if TITLE_RE.search(html) else slug
		cover = COVER_RE.search(html)
		info = _parse_info(html)

		code = next(iter(info.get("code", [])), slug)
		air_date = None
		if info.get("release"):
			try:
				air_date = datetime.strptime(info["release"][0], "%Y-%m-%d").date()
			except ValueError:
				pass
		runtime = _parse_duration(info.get("duration", [""])[0] or "")
		genres = [
			GENRE_MAP[g] for g in info.get("genres", []) if g in GENRE_MAP
		] or [Genre.DRAMA]
		maker = next(iter(info.get("maker", [])), None)
		# the ja page has original names, prefer it for cast when available
		cast = info.get("cast", [])
		if "ja" in pages and primary != "ja":
			ja_cast = _parse_info(pages["ja"]).get("cast")
			if ja_cast:
				cast = ja_cast
		tags = info.get("tags", []) + info.get("genres", [])

		# english title (when requested) doubles as latin name
		latin = None
		if "en" in pages and pages["en"] is not html:
			m = TITLE_RE.search(pages["en"])
			if m:
				latin = _strip(m.group(1))

		translations = {}
		for lang in self._langs:
			if lang not in pages:
				continue
			m = TITLE_RE.search(pages[lang])
			name = _strip(m.group(1)) if m else title
			translations[Language.get(LANGS[lang])] = MovieTranslation(
				name=clean(name) or code,
				latin_name=latin if lang != "en" else None,
				description=None,
				tagline=None,
				aliases=[code],
				tags=tags,
				poster=cover.group(1) if cover else None,
				thumbnail=cover.group(1) if cover else None,
				banner=None,
				logo=None,
				trailer=None,
			)

		return Movie(
			slug=to_slug(code),
			original_language=Language.get("ja"),
			genres=genres,
			rating={},
			status=MovieStatus.FINISHED,
			runtime=runtime,
			air_date=air_date,
			external_id={
				self.name: [
					MetadataId(
						data_id=slug,
						link=f"https://123av.com/en/v/{quote_plus(slug)}",
					)
				]
			},
			translations=translations,
			studios=(
				[
					Studio(
						slug=to_slug(maker),
						external_id={self.name: [MetadataId(data_id=to_slug(maker))]},
						translations={
							LANGS[primary]: StudioTranslation(name=maker, logo=None)
						},
					)
				]
				if maker
				else []
			),
			staff=[
				Staff(
					kind=Role.ACTOR,
					character=None,
					staff=Person(
						slug=to_slug(a),
						name=a,
						latin_name=None,
						image=None,
						external_id={self.name: [MetadataId(data_id=to_slug(a))]},
					),
				)
				for a in cast
			],
		)

	async def search_movies(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchMovie]:
		code = extract_code(title)
		if code is None:
			logger.info("123av: no JAV code in %r, skipping", title)
			return []
		lang = self._langs[0]
		html = await self._get(
			f"/{lang}/search?keyword={quote_plus(code)}"
		)
		results = []
		for m in CARD_RE.finditer(html):
			slug = m.group("slug")
			# prefer exact matches first (`abp-123`, not `abp-123-uncensored-leaked`)
			if not _slug_matches(slug, code):
				continue
			results.append(
				SearchMovie(
					id=slug,
					slug=to_slug(slug),
					name=_strip(m.group("title")),
					description=None,
					air_date=None,
					poster=m.group("img"),
					original_language=Language.get("ja"),
					external_id={self.name: [MetadataId(data_id=slug)]},
				)
			)
			if len(results) >= 5:
				break
		if not results:
			logger.info("123av: no result for %s", code)
		return results

	async def get_movie(self, external_id: dict[str, str]) -> Movie | None:
		if self.name not in external_id:
			return None
		slug = external_id[self.name]
		pages: dict[str, str] = {}
		for lang in self._langs:
			try:
				pages[lang] = await self._get(f"/{lang}/v/{slug}")
			except ProviderError as e:
				logger.warning("123av: %s page unavailable for %s: %s", lang, slug, e)
		if not pages:
			return None
		return self._map_details(pages, slug)

	# Adult content is treated as movies-only; series endpoints are no-ops.

	async def search_series(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchSerie]:
		return []

	async def get_serie(
		self, external_id: dict[str, str], *, skip_entries=False
	) -> Serie | None:
		return None
