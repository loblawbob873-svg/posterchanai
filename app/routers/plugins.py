"""Plugin management API routes"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import List, Optional
from pydantic import BaseModel
import json

from app.database import get_db
from app.models import User, Plugin
from app.auth import get_current_user

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


class PluginAction(BaseModel):
    name: str
    description: str
    method: str = "GET"
    path: str
    params: Optional[List[str]] = None
    body: Optional[dict] = None


class PluginCreate(BaseModel):
    name: str
    description: str
    base_url: str
    auth_type: str = "none"
    auth_header: str = "X-API-Key"
    auth_value: Optional[str] = None
    actions: List[PluginAction]
    allowed_users: Optional[List[int]] = None  # List of user IDs, None = all users


class PluginUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    base_url: Optional[str] = None
    auth_type: Optional[str] = None
    auth_header: Optional[str] = None
    auth_value: Optional[str] = None
    actions: Optional[List[PluginAction]] = None
    enabled: Optional[bool] = None
    allowed_users: Optional[List[int]] = None


class PluginResponse(BaseModel):
    id: int
    user_id: Optional[int]
    name: str
    description: str
    base_url: str
    auth_type: str
    auth_header: str
    actions: List[dict]
    enabled: bool
    is_global: bool
    allowed_users: Optional[List[int]] = None

    class Config:
        from_attributes = True


@router.get("", response_model=List[PluginResponse])
def list_plugins(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all plugins available to the user (their own + global)"""
    plugins = db.query(Plugin).filter(
        or_(Plugin.user_id == current_user.id, Plugin.user_id == None)
    ).all()

    result = []
    for p in plugins:
        try:
            actions = json.loads(p.actions)
        except (json.JSONDecodeError, TypeError, ValueError):
            actions = []

        # Parse allowed_users
        allowed_users = None
        if p.allowed_users:
            allowed_users = [int(x.strip()) for x in p.allowed_users.split(',') if x.strip()]

        result.append(PluginResponse(
            id=p.id,
            user_id=p.user_id,
            name=p.name,
            description=p.description,
            base_url=p.base_url,
            auth_type=p.auth_type,
            auth_header=p.auth_header,
            actions=actions,
            enabled=p.enabled,
            is_global=p.user_id is None,
            allowed_users=allowed_users
        ))

    return result


@router.post("", response_model=PluginResponse, status_code=status.HTTP_201_CREATED)
def create_plugin(
    data: PluginCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new plugin"""
    # Check if name already exists for this user
    existing = db.query(Plugin).filter(
        Plugin.name == data.name,
        or_(Plugin.user_id == current_user.id, Plugin.user_id == None)
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Plugin with name '{data.name}' already exists"
        )

    actions_json = json.dumps([a.model_dump() for a in data.actions])

    # Convert allowed_users list to comma-separated string
    allowed_users_str = None
    if data.allowed_users is not None:
        allowed_users_str = ','.join(str(x) for x in data.allowed_users) if data.allowed_users else ''

    # Only admins can create global plugins (with user restrictions)
    # Non-admins always create personal plugins
    user_id = current_user.id
    if current_user.is_admin and data.allowed_users is not None:
        user_id = None  # Global plugin

    plugin = Plugin(
        user_id=user_id,
        name=data.name,
        description=data.description,
        base_url=data.base_url,
        auth_type=data.auth_type,
        auth_header=data.auth_header,
        auth_value=data.auth_value,
        actions=actions_json,
        enabled=True,
        allowed_users=allowed_users_str if user_id is None else None
    )

    db.add(plugin)
    db.commit()
    db.refresh(plugin)

    return PluginResponse(
        id=plugin.id,
        user_id=plugin.user_id,
        name=plugin.name,
        description=plugin.description,
        base_url=plugin.base_url,
        auth_type=plugin.auth_type,
        auth_header=plugin.auth_header,
        actions=[a.model_dump() for a in data.actions],
        enabled=plugin.enabled,
        is_global=plugin.user_id is None,
        allowed_users=data.allowed_users
    )


@router.get("/{plugin_id}", response_model=PluginResponse)
def get_plugin(
    plugin_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific plugin"""
    plugin = db.query(Plugin).filter(
        Plugin.id == plugin_id,
        or_(Plugin.user_id == current_user.id, Plugin.user_id == None)
    ).first()

    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    try:
        actions = json.loads(plugin.actions)
    except (json.JSONDecodeError, TypeError, ValueError):
        actions = []

    allowed_users = None
    if plugin.allowed_users:
        allowed_users = [int(x.strip()) for x in plugin.allowed_users.split(',') if x.strip()]

    return PluginResponse(
        id=plugin.id,
        user_id=plugin.user_id,
        name=plugin.name,
        description=plugin.description,
        base_url=plugin.base_url,
        auth_type=plugin.auth_type,
        auth_header=plugin.auth_header,
        actions=actions,
        enabled=plugin.enabled,
        is_global=plugin.user_id is None,
        allowed_users=allowed_users
    )


@router.put("/{plugin_id}", response_model=PluginResponse)
def update_plugin(
    plugin_id: int,
    data: PluginUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a plugin (own plugins or global if admin)"""
    # Admins can edit global plugins, users can only edit their own
    if current_user.is_admin:
        plugin = db.query(Plugin).filter(
            Plugin.id == plugin_id,
            or_(Plugin.user_id == current_user.id, Plugin.user_id == None)
        ).first()
    else:
        plugin = db.query(Plugin).filter(
            Plugin.id == plugin_id,
            Plugin.user_id == current_user.id
        ).first()

    if not plugin:
        raise HTTPException(
            status_code=404,
            detail="Plugin not found or you don't have permission to edit it"
        )

    if data.name is not None:
        plugin.name = data.name
    if data.description is not None:
        plugin.description = data.description
    if data.base_url is not None:
        plugin.base_url = data.base_url
    if data.auth_type is not None:
        plugin.auth_type = data.auth_type
    if data.auth_header is not None:
        plugin.auth_header = data.auth_header
    if data.auth_value is not None:
        plugin.auth_value = data.auth_value
    if data.actions is not None:
        plugin.actions = json.dumps([a.model_dump() for a in data.actions])
    if data.enabled is not None:
        plugin.enabled = data.enabled
    if data.allowed_users is not None and current_user.is_admin and plugin.user_id is None:
        plugin.allowed_users = ','.join(str(x) for x in data.allowed_users) if data.allowed_users else None

    db.commit()
    db.refresh(plugin)

    try:
        actions = json.loads(plugin.actions)
    except (json.JSONDecodeError, TypeError, ValueError):
        actions = []

    return PluginResponse(
        id=plugin.id,
        user_id=plugin.user_id,
        name=plugin.name,
        description=plugin.description,
        base_url=plugin.base_url,
        auth_type=plugin.auth_type,
        auth_header=plugin.auth_header,
        actions=actions,
        enabled=plugin.enabled,
        is_global=False
    )


@router.delete("/{plugin_id}")
def delete_plugin(
    plugin_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a plugin (only own plugins, not global)"""
    plugin = db.query(Plugin).filter(
        Plugin.id == plugin_id,
        Plugin.user_id == current_user.id  # Can only delete own plugins
    ).first()

    if not plugin:
        raise HTTPException(
            status_code=404,
            detail="Plugin not found or you don't have permission to delete it"
        )

    db.delete(plugin)
    db.commit()

    return {"message": "Plugin deleted"}


@router.post("/{plugin_id}/toggle")
def toggle_plugin(
    plugin_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Toggle a plugin's enabled status"""
    plugin = db.query(Plugin).filter(
        Plugin.id == plugin_id,
        Plugin.user_id == current_user.id
    ).first()

    if not plugin:
        raise HTTPException(
            status_code=404,
            detail="Plugin not found or you don't have permission to modify it"
        )

    plugin.enabled = not plugin.enabled
    db.commit()

    return {"enabled": plugin.enabled}
