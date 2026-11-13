import os
from datetime import datetime
from logging import getLogger
from types import TracebackType
from typing import Any
from urllib.parse import quote_plus

from aiohttp import ClientSession
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

# FANZA genres don't map 1:1 to Kyoo's list; adjust to taste.
FANZA_GENRE_MAP = {
	"巨乳": Genre.ROMANCE,
	"熟女": Genre.ROMANCE,
	"女子校生": Genre.DRAMA,
	"人妻": Genre.ROMANCE,
	"単体作品": Genre.DRAMA,
}


class FANZA(Provider):
	"""
	Provider for DMM/FANZA adult movies, using FANZA's (undocumented) GraphQL
	endpoint that powers the DMM TV / FANZA TV apps.

	Note: the schema below is not officially documented. If the API rejects a
	query, introspect the live schema and adjust the field names in the
	documents/`_map_movie` accordingly.
	"""

	def __init__(self) -> None:
		super().__init__()
		endpoint = os.environ.get(
			"FANZA_GRAPHQL_URL", "https://api.dmm-play.com/graphql"
		)
		base, _, path = endpoint.rpartition("/")
		self._gql_path = path or "graphql"
		token = os.environ.get("FANZA_API_TOKEN", "")
		self._client = ClientSession(
			base_url=f"{base}/",
			headers={
				"User-Agent": "kyoo scanner v5",
				"Content-Type": "application/json",
				# The app API expects a bearer/device token obtained at login.
				"Authorization": f"Bearer {token}",
			},
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
		return ProviderName.FANZA

	async def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
		async with self._client.post(
			self._gql_path, json={"query": query, "variables": variables}
		) as r:
			r.raise_for_status()
			data = await r.json()
		if "errors" in data:
			raise ProviderError(f"FANZA graphql error: {data['errors']}")
		return data["data"]

	# GraphQL documents. Adjust field names against the live schema if needed.
	_SEARCH_QUERY = """
	query Search($keyword: String!) {
		searchVideoList(keyword: $keyword) {
			items {
				id
				title
				description
				jacketImage
				releaseDate
				runtime
				genres { name }
				maker { id name }
				actresses { id name }
				product { id legacyId }
			}
		}
	}
	"""

	_GET_QUERY = """
	query GetVideo($id: ID!) {
		getVideoDetail(id: $id) {
			id
			title
			asciiTitle
			description
			jacketImage
			thumbnailImage
			releaseDate
			runtime
			rating { score }
			genres { name }
			maker { id name }
			actresses { id name }
			product { id legacyId }
		}
	}
	"""

	def _map_movie(self, x: dict[str, Any]) -> Movie:
		code = x.get("product", {}).get("legacyId") or to_slug(x["title"])
		product_id = x["product"]["id"]
		air_date = (
			datetime.strptime(x["releaseDate"], "%Y-%m-%d").date()
			if x.get("releaseDate")
			else None
		)
		rating = x.get("rating", {}).get("score")
		genres = [
			FANZA_GENRE_MAP[g["name"]]
			for g in x.get("genres", [])
			if g["name"] in FANZA_GENRE_MAP
		] or [Genre.DRAMA]
		studio = x.get("maker")
		return Movie(
			slug=to_slug(code),
			original_language=Language.get("ja"),
			genres=genres,
			rating={self.name: round(float(rating) * 20)} if rating else {},
			status=MovieStatus.FINISHED,
			runtime=int(x["runtime"]) if x.get("runtime") else None,
			air_date=air_date,
			external_id={
				self.name: [
					MetadataId(
						data_id=str(product_id),
						link=f"https://www.dmm.co.jp/digital/videoa/-/detail/=/cid={quote_plus(code)}/",
					)
				]
			},
			translations={
				Language.get("ja"): MovieTranslation(
					name=clean(x["title"]) or code,
					latin_name=clean(x.get("asciiTitle")) or None,
					description=clean(x.get("description")),
					tagline=None,
					aliases=[code],
					tags=[g["name"] for g in x.get("genres", [])],
					poster=x.get("jacketImage"),
					thumbnail=x.get("thumbnailImage"),
					banner=None,
					logo=None,
					trailer=None,
				)
			},
			studios=(
				[
					Studio(
						slug=to_slug(studio["name"]),
						external_id={
							self.name: [MetadataId(data_id=str(studio["id"]))]
						},
						translations={
							"ja": StudioTranslation(
								name=studio["name"], logo=None
							)
						},
					)
				]
				if studio
				else []
			),
			staff=[
				Staff(
					kind=Role.ACTOR,
					character=None,
					staff=Person(
						slug=to_slug(a["name"]),
						name=a["name"],
						latin_name=None,
						image=None,
						external_id={
							self.name: [MetadataId(data_id=str(a["id"]))]
						},
					),
				)
				for a in x.get("actresses", [])
			],
		)

	@staticmethod
	def _map_search(x: dict[str, Any], provider: str) -> SearchMovie:
		code = x.get("product", {}).get("legacyId") or to_slug(x["title"])
		return SearchMovie(
			id=str(x["id"]),
			slug=to_slug(code),
			name=x["title"],
			description=x.get("description"),
			air_date=datetime.strptime(x["releaseDate"], "%Y-%m-%d").date()
			if x.get("releaseDate")
			else None,
			poster=x.get("jacketImage"),
			original_language=Language.get("ja"),
			external_id={
				provider: [MetadataId(data_id=str(x["product"]["id"]))]
			},
		)

	async def search_movies(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchMovie]:
		keyword = self.extract_code(title) or title
		data = await self._gql(self._SEARCH_QUERY, {"keyword": keyword})
		items = data.get("searchVideoList", {}).get("items", [])
		if year:
			items = [
				x
				for x in items
				if not x.get("releaseDate") or x["releaseDate"].startswith(str(year))
			]
		ret = [self._map_search(x, self.name) for x in items]
		if not ret:
			logger.info("FANZA found no result for %s", keyword)
		return ret

	async def get_movie(self, external_id: dict[str, str]) -> Movie | None:
		if self.name not in external_id:
			return None
		data = await self._gql(
			self._GET_QUERY, {"id": external_id[self.name]}
		)
		detail = data.get("getVideoDetail")
		if not detail:
			return None
		return self._map_movie(detail)

	# Adult content is treated as movies-only; series endpoints are no-ops.

	async def search_series(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchSerie]:
		return []

	async def get_serie(
		self, external_id: dict[str, str], *, skip_entries=False
	) -> Serie | None:
		return None


class AdultAwareProvider(Provider):
	"""
	Composite that routes JAV-looking guesses to adult providers and merges
	their results: the first provider (e.g. 123av for Chinese titles/posters)
	is the base, the others fill the gaps (e.g. jav321 for descriptions and
	ratings). Everything else goes to the default providers.

	Wire it in `scanner/__init__.py`:

	    from scanner.providers.jav321 import Jav321
	    from scanner.providers.one123av import OneTwoThreeAV
	    async with (..., OneTwoThreeAV() as av123, Jav321() as jav321):
	        app.state.provider = AdultAwareProvider(tvdb, tmdb, av123, jav321)
	"""

	def __init__(
		self, tvdb: Provider, tmdb: Provider, *adult: Provider
	) -> None:
		self._tvdb = tvdb
		self._tmdb = tmdb
		self._adult = adult

	@property
	def name(self) -> str:
		return "adult-aware"

	def _is_adult(self, title: str) -> bool:
		return extract_code(title) is not None

	# fields filled in the base translation only when missing
	FILL_FIELDS = ("description", "latin_name", "tagline")
	# fields where later providers win (jav321's full-size DMM covers are
	# preferred over 123av's scaled ones)
	OVERRIDE_FIELDS = ("poster", "thumbnail", "banner", "logo")

	@staticmethod
	def _merge_movies(base: Movie, *others: Movie) -> Movie:
		for other in others:
			base.rating |= other.rating
			base.external_id = MetadataId.merge(
				base.external_id, other.external_id
			)
			if not base.studios:
				base.studios = other.studios
			if not base.staff:
				base.staff = other.staff
			for lang, translation in other.translations.items():
				if lang in base.translations:
					target = base.translations[lang]
					for field in AdultAwareProvider.FILL_FIELDS:
						if getattr(target, field) is None:
							setattr(target, field, getattr(translation, field))
				else:
					base.translations[lang] = translation
		# images are not localized: if any provider has one, every language
		# gets it (later providers win, e.g. jav321's full-size DMM covers)
		for field in AdultAwareProvider.OVERRIDE_FIELDS:
			value = next(
				(
					getattr(translation, field)
					for other in others
					for translation in other.translations.values()
					if getattr(translation, field) is not None
				),
				None,
			)
			if value is not None:
				for translation in base.translations.values():
					setattr(translation, field, value)
		return base

	async def search_movies(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchMovie]:
		if not self._is_adult(title):
			return await self._tmdb.search_movies(title, year, language=language)
		base: list[SearchMovie] | None = None
		for provider in self._adult:
			try:
				results = await provider.search_movies(
					title, year, language=language
				)
			except Exception as e:
				logger.warning(
					"%s: search failed for %r: %s", provider.name, title, e
				)
				continue
			if not results:
				continue
			if base is None:
				base = results
				continue
			# attach the other providers' ids to the first result so
			# get_movie can merge all sources afterwards
			base[0].external_id = MetadataId.merge(
				base[0].external_id, results[0].external_id
			)
		return base or []

	async def get_movie(self, external_id: dict[str, str]) -> Movie | None:
		movies: list[Movie] = []
		for provider in self._adult:
			if provider.name not in external_id:
				continue
			try:
				movie = await provider.get_movie(external_id)
			except Exception as e:
				logger.warning(
					"%s: get_movie failed for %s: %s",
					provider.name,
				external_id,
					e,
				)
				continue
			if movie is not None:
				movies.append(movie)
		if movies:
			return self._merge_movies(movies[0], *movies[1:])
		return await self._tmdb.get_movie(external_id)

	async def search_series(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchSerie]:
		return await self._tmdb.search_series(title, year, language=language)

	async def get_serie(
		self, external_id: dict[str, str], *, skip_entries=False
	) -> Serie | None:
		return await self._tmdb.get_serie(external_id, skip_entries=skip_entries)
