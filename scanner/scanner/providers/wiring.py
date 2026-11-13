import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from logging import getLogger

from langcodes import Language

from ..models.movie import Movie, SearchMovie
from ..models.serie import SearchSerie, Serie
from .composite import CompositeProvider
from .fanza import AdultAwareProvider
from .jav321 import Jav321
from .names import ProviderName
from .one123av import OneTwoThreeAV
from .provider import Provider
from .themoviedatabase import TheMovieDatabase
from .thetvdb import TVDB

logger = getLogger(__name__)

ADULT_PROVIDERS = {
	ProviderName.AV123: OneTwoThreeAV,
	ProviderName.JAV321: Jav321,
}


class NullProvider(Provider):
	"""Provider used in place of a disabled one; finds nothing."""

	@property
	def name(self) -> str:
		return "null"

	async def search_movies(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchMovie]:
		return []

	async def get_movie(self, external_id: dict[str, str]) -> Movie | None:
		return None

	async def search_series(
		self, title: str, year: int | None, *, language: list[Language]
	) -> list[SearchSerie]:
		return []

	async def get_serie(
		self, external_id: dict[str, str], *, skip_entries=False
	) -> Serie | None:
		return None


def enabled_providers() -> set[str]:
	raw = os.environ.get("KYOO_PROVIDERS", "tmdb,tvdb")
	return {x.strip().lower() for x in raw.split(",") if x.strip()}


@asynccontextmanager
async def build_provider() -> "AsyncIterator[Provider]":
	"""
	Build the metadata provider from `KYOO_PROVIDERS` (default `tmdb,tvdb`).

	- `tmdb`, `tvdb`: the default Kyoo providers.
	- `123av`, `jav321`: adult providers; enabling any of them switches to
	  `AdultAwareProvider` (JAV codes go to the adult providers, everything
	  else to tmdb/tvdb).
	Disabled providers are replaced by a null provider that finds nothing,
	 so `KYOO_PROVIDERS=123av,jav321` gives a JAV-only library.
	"""
	enabled = enabled_providers()
	logger.info("Enabled metadata providers: %s", ", ".join(sorted(enabled)))

	opened: list[Provider] = []
	try:
		tvdb: Provider = NullProvider()
		tmdb: Provider = NullProvider()
		if "tvdb" in enabled:
			tvdb = await TVDB().__aenter__()
			opened.append(tvdb)
		if "tmdb" in enabled:
			tmdb = await TheMovieDatabase().__aenter__()
			opened.append(tmdb)

		adult: list[Provider] = []
		for name, cls in ADULT_PROVIDERS.items():
			if name in enabled:
				provider = await cls().__aenter__()
				opened.append(provider)
				adult.append(provider)

		if adult:
			yield AdultAwareProvider(tvdb, tmdb, *adult)
		else:
			yield CompositeProvider(tvdb, tmdb)
	finally:
		for provider in reversed(opened):
			await provider.__aexit__(None, None, None)
