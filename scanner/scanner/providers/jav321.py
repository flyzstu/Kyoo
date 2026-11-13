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
from ..models.movie import (
	Movie,
	MovieStatus,
	MovieTranslation,
	SearchMovie,
)
from ..models.serie import SearchSerie, Serie
from ..models.staff import Person, Role, Staff
from ..models.studio import Studio, StudioTranslation
from ..utils import clean, to_slug
from .names import ProviderName
from .provider import Provider, ProviderError

logger = getLogger(__name__)

TITLE_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.DOTALL)
POSTER_RE = re.compile(r'<img class="img-responsive"[^>]*src="([^"]+)"')
INFO_RE = re.compile(r"<b>([^<]+)</b>:\s*(.*?)<br>", re.DOTALL)
DESC_RE = re.compile(r'<div class="col-md-12">(.*?)</div>', re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")

# localized info labels (www = ja labels, en. = en labels)
LABELS = {
	"cast": ("出演者", "Stars"),
	"maker": ("メーカー", "Studio"),
	"genres": ("ジャンル", "Genre"),
	"code": ("品番", "SN"),
	"release": ("配信開始日", "Release Date"),
	"duration": ("収録時間", "Play time"),
	"rating": ("平均評価", "Rating"),
	"series": ("シリーズ", "Series"),
}

GENRE_MAP = {
	"単体作品": Genre.DRAMA,
	"巨乳": Genre.ROMANCE,
	"熟女": Genre.ROMANCE,
	"人妻": Genre.ROMANCE,
	"女子校生": Genre.DRAMA,
	"ドキュメンタリー": Genre.DOCUMENTARY,
	"ドラマ": Genre.DRAMA,
	"コメディ": Genre.COMEDY,
	"ホラー": Genre.HORROR,
	"ミステリー": Genre.MYSTERY,
}


def _strip(html: str) -> str:
	return TAG_RE.sub("", html).strip()


def _parse_info(html: str) -> dict[str, list[str]]:
	info: dict[str, list[str]] = {}
	for label, raw in INFO_RE.findall(html):
		for key, names in LABELS.items():
			if _strip(label) in names:
				values = [
					_strip(x)
					for x in re.findall(r">([^<>]+)</a>", raw)
				] or ([_strip(raw)] if _strip(raw) else [])
				info[key] = values
				break
	return info


def _parse_duration(raw: str) -> int | None:
	m = re.search(r"(\d+)", raw)
	return int(m.group(1)) if m else None


def _parse_description(html: str) -> str | None:
	for block in DESC_RE.findall(html):
		text = _strip(block)
		if len(text) > 40 and "<video" not in block:
			return text
	return None


class Jav321(Provider):
	"""
	Provider scraping jav321.com for adult movies. No API key needed.

	The site is server-rendered and its data mirrors FANZA/DMM (including the
	product description and rating). Content is Japanese; labels depend on
	the subdomain (`www` = ja labels, `en.` = en labels).
	"""

	def __init__(self) -> None:
		super().__init__()
		self._client = ClientSession(
			base_url=os.environ.get("JAV321_BASE_URL", "https://www.jav321.com/"),
			headers={
				"User-Agent": "kyoo scanner v5",
				"Accept-Language": "ja",
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
		return ProviderName.JAV321

	async def _get_detail(self, html: str, cid: str) -> Movie | None:
		title_m = TITLE_RE.search(html)
		if not title_m:
			return None
		title = _strip(title_m.group(1))
		poster = POSTER_RE.search(html)
		info = _parse_info(html)
		description = _parse_description(html)

		code = next(iter(info.get("code", [])), cid)
		air_date = None
		if info.get("release"):
			try:
				air_date = datetime.strptime(info["release"][0], "%Y-%m-%d").date()
			except ValueError:
				pass
		runtime = _parse_duration(info.get("duration", [""])[0] or "")
		rating = next(iter(info.get("rating", [])), None)
		genres = [
			GENRE_MAP[g] for g in info.get("genres", []) if g in GENRE_MAP
		] or [Genre.DRAMA]
		maker = next(iter(info.get("maker", [])), None)
		cast = info.get("cast", [])
		tags = info.get("genres", []) + info.get("series", [])

		return Movie(
			slug=to_slug(code),
			original_language=Language.get("ja"),
			genres=genres,
			rating={self.name: round(float(rating) * 20)} if rating else {},
			status=MovieStatus.FINISHED,
			runtime=runtime,
			air_date=air_date,
			external_id={
				self.name: [
					MetadataId(
						data_id=cid,
						link=f"https://www.jav321.com/video/{quote_plus(cid)}",
					)
				]
			},
			translations={
				Language.get("ja"): MovieTranslation(
					name=clean(title) or code,
					latin_name=None,
					description=clean(description),
					tagline=None,
					aliases=[code],
					tags=tags,
					poster=poster.group(1) if poster else None,
					thumbnail=poster.group(1) if poster else None,
					banner=None,
					logo=None,
					trailer=None,
				)
			},
			studios=(
				[
					Studio(
						slug=to_slug(maker),
						external_id={self.name: [MetadataId(data_id=to_slug(maker))]},
						translations={"ja": StudioTranslation(name=maker, logo=None)},
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
			logger.info("jav321: no JAV code in %r, skipping", title)
			return []
		try:
			async with self._client.post(
				"/search", data={"sn": code}, allow_redirects=True
			) as r:
				r.raise_for_status()
				html = await r.text()
				path = str(r.url.path)
		except Exception as e:
			logger.warning("jav321: search failed for %s: %s", code, e)
			return []
		if "/video/" not in path:
			# no exact match, the site stayed on the search page
			logger.info("jav321: no result for %s", code)
			return []
		cid = path.rsplit("/video/", 1)[-1]
		movie = await self._get_detail(html, cid)
		if movie is None:
			return []
		translation = movie.translations[Language.get("ja")]
		return [
			SearchMovie(
				id=cid,
				slug=movie.slug,
				name=translation.name,
				description=translation.description,
				air_date=movie.air_date,
				poster=translation.poster,
				original_language=Language.get("ja"),
				external_id={
					self.name: [MetadataId(data_id=cid)]
				},
			)
		]

	async def get_movie(self, external_id: dict[str, str]) -> Movie | None:
		if self.name not in external_id:
			return None
		cid = external_id[self.name]
		async with self._client.get(f"/video/{cid}") as r:
			if r.status == 404:
				raise ProviderError(f"jav321: not found: {cid}")
			r.raise_for_status()
			html = await r.text()
		return await self._get_detail(html, cid)

	# Adult content is treated as movies-only; series endpoints are no-ops.

	async def search_series(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchSerie]:
		return []

	async def get_serie(
		self, external_id: dict[str, str], *, skip_entries=False
	) -> Serie | None:
		return None
