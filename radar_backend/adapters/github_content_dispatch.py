"""GitHub repository_dispatch transport and explicit environment composition."""
from __future__ import annotations

import os
from typing import Callable, Mapping

import requests

from radar_backend.application.content_dispatch import ContentDispatchServices
from radar_backend.domain.content_dispatch import DispatchConfiguration, DispatchDelivery


def resolve_configuration(environ: Mapping[str, str]) -> DispatchConfiguration:
    return DispatchConfiguration(
        target_repository=(environ.get("CONTENT_BACKEND_REPOSITORY") or
                           environ.get("GITHUB_REPOSITORY") or "").strip(),
        token=(environ.get("CONTENT_BACKEND_TOKEN") or environ.get("GITHUB_TOKEN") or "").strip(),
        source_repository=environ.get("GITHUB_REPOSITORY"),
    )


def post_dispatch(configuration: DispatchConfiguration, payload: dict, *,
                  post: Callable | None = None) -> DispatchDelivery:
    post = post if post is not None else requests.post
    try:
        response = post(
            f"https://api.github.com/repos/{configuration.target_repository}/dispatches",
            headers={
                "Authorization": f"Bearer {configuration.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "GameTrendRadarFollowersDispatch/1.0",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        return DispatchDelivery(error_type=type(exc).__name__)
    return DispatchDelivery(http=response.status_code)


def compose_services(clock: Callable, environ: Mapping[str, str] | None = None,
                     post: Callable | None = None, emit: Callable = print) -> ContentDispatchServices:
    """Resolve ports at call time so compatibility wrappers retain monkeypatches."""
    return ContentDispatchServices(
        clock=clock,
        configuration=lambda: resolve_configuration(os.environ if environ is None else environ),
        send=lambda configuration, payload: post_dispatch(configuration, payload, post=post),
        emit=emit,
    )
