"""The user's tags: list, create, rename, delete (vts-qwcj).

Setting a recording's tags lives with the recording (`PUT
/api/recordings/{id}/tags`, and the task adapter); this router manages the tags
themselves. Every route is owner-scoped: another user's tag id is a 404, the
same as an id that does not exist.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from vts.api.deps import get_current_user, get_session_dep
from vts.api.schemas import TagDeleteOut, TagNameIn, TagOut, TagWithCountOut
from vts.db.repo import Repo
from vts.services.auth import AuthenticatedUser
from vts.services.tags import TagExists, TagNameError

router = APIRouter()

_EXISTS = "Tag already exists"


@router.get("/api/tags", response_model=list[TagWithCountOut])
async def list_tags(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> list[TagWithCountOut]:
    """The user's tags sorted by name, each with how many recordings carry
    it. Unused tags are listed with count 0."""
    rows = await Repo(session).list_tags(uuid.UUID(user.id))
    return [TagWithCountOut(id=t.id, name=t.name, count=n) for t, n in rows]


@router.post("/api/tags", response_model=TagOut, status_code=201)
async def create_tag(
    payload: TagNameIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> TagOut:
    """Create a tag. Names are trimmed and compared case-insensitively;
    409 if the user already has one with the same name."""
    try:
        tag = await Repo(session).create_tag(uuid.UUID(user.id), payload.name)
    except TagNameError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TagExists as exc:
        raise HTTPException(status_code=409, detail=_EXISTS) from exc
    await session.commit()
    return TagOut(id=tag.id, name=tag.name)


@router.patch("/api/tags/{tag_id}", response_model=TagOut)
async def rename_tag(
    tag_id: uuid.UUID,
    payload: TagNameIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> TagOut:
    """Rename a tag; every recording carrying it follows. 409 if another of
    the user's tags has that name; a change of case alone is allowed."""
    try:
        tag = await Repo(session).rename_tag(uuid.UUID(user.id), tag_id, payload.name)
    except TagNameError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TagExists as exc:
        raise HTTPException(status_code=409, detail=_EXISTS) from exc
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    await session.commit()
    return TagOut(id=tag.id, name=tag.name)


@router.delete("/api/tags/{tag_id}", response_model=TagDeleteOut)
async def delete_tag(
    tag_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> TagDeleteOut:
    """Delete a tag and remove it from every recording; returns how many
    recordings it was removed from."""
    unlinked = await Repo(session).delete_tag(uuid.UUID(user.id), tag_id)
    if unlinked is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    await session.commit()
    return TagDeleteOut(unlinked=unlinked)
