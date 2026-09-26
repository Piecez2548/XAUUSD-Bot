"""FastAPI registration for the read-only Research Roadmap API."""

from __future__ import annotations

from fastapi import HTTPException, Query

from services.research_roadmap import (
    RoadmapError,
    get_roadmap_overview,
    get_roadmap_run,
    get_roadmap_timeline,
)


def _raise_api_error(error: RoadmapError) -> None:
    status = 404 if error.code == "ROADMAP_RUN_NOT_FOUND" else 400
    if error.code == "ROADMAP_QUERY_FAILED":
        status = 503
    raise HTTPException(status_code=status, detail=error.code) from None


def register_research_roadmap_routes(app, database) -> None:
    """Register GET-only roadmap routes on the existing authenticated app."""

    @app.get("/api/research/roadmap")
    def research_roadmap(limit: str = Query(default="20")):
        try:
            return get_roadmap_overview(database, limit=limit).model_dump(mode="json")
        except RoadmapError as error:
            _raise_api_error(error)

    @app.get("/api/research/roadmap/runs/{run_id}")
    def research_roadmap_run(run_id: str):
        try:
            return get_roadmap_run(database, run_id).model_dump(mode="json")
        except RoadmapError as error:
            _raise_api_error(error)

    @app.get("/api/research/roadmap/runs/{run_id}/timeline")
    def research_roadmap_timeline(
        run_id: str,
        limit: str = Query(default="100"),
        cursor: str | None = Query(default=None),
    ):
        try:
            return get_roadmap_timeline(
                database, run_id, limit=limit, cursor=cursor
            ).model_dump(mode="json")
        except RoadmapError as error:
            _raise_api_error(error)
