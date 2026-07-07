"""Minimal Subsonic API client for Navidrome — only what the quiz needs."""
import hashlib
import secrets

import httpx

from . import config


class SubsonicError(RuntimeError):
    pass


def auth_params(user: str | None = None, password: str | None = None) -> dict:
    """Salted token auth per the Subsonic API spec (no plaintext password on the wire)."""
    user = user or config.NAVIDROME_USER
    password = password if password is not None else config.NAVIDROME_PASSWORD
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return {"u": user, "t": token, "s": salt, "v": "1.16.1",
            "c": config.CLIENT_NAME, "f": "json"}


class Client:
    def __init__(self, base_url: str | None = None, transport: httpx.BaseTransport | None = None):
        self.base = (base_url or config.NAVIDROME_URL) + "/rest"
        self._http = httpx.Client(timeout=30, transport=transport)

    def _get(self, endpoint: str, **params) -> dict:
        r = self._http.get(f"{self.base}/{endpoint}", params={**auth_params(), **params})
        r.raise_for_status()
        body = r.json()["subsonic-response"]
        if body.get("status") != "ok":
            err = body.get("error", {})
            raise SubsonicError(f"{endpoint}: {err.get('message', 'unknown error')} (code {err.get('code')})")
        return body

    def ping(self) -> bool:
        self._get("ping.view")
        return True

    def album_list(self, offset: int, size: int = 500) -> list[dict]:
        body = self._get("getAlbumList2.view", type="alphabeticalByName", size=size, offset=offset)
        return body.get("albumList2", {}).get("album", [])

    def album_songs(self, album_id: str) -> list[dict]:
        body = self._get("getAlbum.view", id=album_id)
        return body.get("album", {}).get("song", [])

    def stream_url_params(self, song_id: str) -> tuple[str, dict]:
        """URL + params to download original audio for clip cutting."""
        return f"{self.base}/download.view", {**auth_params(), "id": song_id}

    def download(self, song_id: str, dest_path: str) -> None:
        url, params = self.stream_url_params(song_id)
        with self._http.stream("GET", url, params=params) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)

    def download_transcoded(self, song_id: str, dest_path: str) -> None:
        """Server-side transcode to mp3 — rescues formats ffmpeg can't decode (old WMA rips)."""
        params = {**auth_params(), "id": song_id, "format": "mp3", "maxBitRate": 320}
        with self._http.stream("GET", f"{self.base}/stream.view", params=params) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
