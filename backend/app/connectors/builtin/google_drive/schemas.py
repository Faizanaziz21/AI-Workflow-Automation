from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl


class DriveConfig(BaseModel):
    client_id: str = Field(description="OAuth client ID from Google Cloud console")
    api_base_url: HttpUrl = Field(default=HttpUrl("https://www.googleapis.com"))
    token_url: HttpUrl = Field(default=HttpUrl("https://oauth2.googleapis.com/token"))
    default_folder_id: str | None = None


class DriveCredentials(BaseModel):
    client_secret: str
    refresh_token: str | None = None
    access_token: str | None = None
    expires_at: float | None = None


class DriveFile(BaseModel):
    id: str
    name: str
    mime_type: str
    size: int | None = None
    created_time: str | None = None
    modified_time: str | None = None
    web_view_link: str | None = None
    parents: list[str] = []


class ListFilesInput(BaseModel):
    folder_id: str | None = None
    name_contains: str | None = None
    mime_type: str | None = None
    page_size: int = Field(default=50, ge=1, le=1000)


class ListFilesOutput(BaseModel):
    files: list[DriveFile]


class UploadFileInput(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    folder_id: str | None = None
    platform_file_id: str | None = Field(default=None, description="ID of a file stored in FlowForge")
    text_content: str | None = Field(default=None, description="Inline text content (used when no platform file)")
    mime_type: str = "text/plain"


class DownloadFileInput(BaseModel):
    file_id: str
    export_mime_type: str | None = Field(
        default=None, description="Export format for Google Docs, e.g. application/pdf or text/plain"
    )


class DownloadFileOutput(BaseModel):
    file: DriveFile
    platform_file: dict
    text_preview: str | None = None


class CreateFolderInput(BaseModel):
    name: str
    parent_id: str | None = None


class NewFilesConfig(BaseModel):
    folder_id: str = Field(description="Folder to watch")
