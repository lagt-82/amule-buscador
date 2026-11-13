"""Cliente mínimo para la API REST oficial de aMule (amuleapi, aMule >= 3.1)."""
import asyncio

import httpx


class AmuleError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class AmuleClient:
    def __init__(self, base_url: str, password: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/") + "/api/v1"
        self.password = password
        self.token: str | None = None
        self._lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self.http.aclose()

    async def _login(self):
        try:
            r = await self.http.post(
                f"{self.base}/auth/login",
                params={"include_token": "true"},
                json={"password": self.password},
            )
        except httpx.HTTPError as e:
            raise AmuleError(502, "amule_unreachable", f"No se puede conectar con aMule: {e}")
        if r.status_code != 200:
            raise AmuleError(502, "login_failed", "aMule rechazó la contraseña (revisa AMULE_WEBUI_PWD)")
        self.token = r.json()["token"]

    async def request(self, method: str, path: str, **kw):
        for attempt in range(2):
            if not self.token:
                async with self._lock:
                    if not self.token:
                        await self._login()
            try:
                r = await self.http.request(
                    method, self.base + path,
                    headers={"Authorization": f"Bearer {self.token}"}, **kw,
                )
            except httpx.HTTPError as e:
                raise AmuleError(502, "amule_unreachable", f"No se puede conectar con aMule: {e}")
            if r.status_code == 401 and attempt == 0:
                self.token = None  # token caducado: volvemos a entrar
                continue
            if r.status_code >= 400:
                try:
                    err = r.json()["error"]
                    raise AmuleError(r.status_code, err.get("code", "error"), err.get("message", ""))
                except (ValueError, KeyError, TypeError):
                    raise AmuleError(r.status_code, "error", r.text[:300])
            if r.status_code == 204 or not r.content:
                return None
            return r.json()
        raise AmuleError(401, "unauthorized", "No autorizado")

    # --- atajos ---
    async def status(self):
        return await self.request("GET", "/status")

    async def start_search(self, body: dict):
        return await self.request("POST", "/search", json=body)

    async def list_searches(self):
        return await self.request("GET", "/search", params={"sort": "search_id", "order": "desc", "limit": 50})

    async def results(self, search_id: int, limit: int = 500):
        return await self.request(
            "GET", f"/search/{search_id}/results",
            params={"limit": limit, "sort": "sources.total", "order": "desc"},
        )

    async def stop_search(self, search_id: int):
        return await self.request("POST", f"/search/{search_id}/stop")

    async def delete_search(self, search_id: int):
        return await self.request("DELETE", f"/search/{search_id}")

    async def more(self, search_id: int):
        return await self.request("POST", f"/search/{search_id}/more")

    async def download_result(self, file_hash: str, ecid: int | None = None):
        body = {"ecid": ecid} if ecid is not None else {}
        return await self.request("POST", f"/search/results/{file_hash}/download", json=body)

    async def downloads(self, status: str = "all"):
        return await self.request("GET", "/downloads", params={"status": status, "limit": 1000})

    async def add_links(self, links: list[str]):
        return await self.request("POST", "/downloads", json={"links": links})

    async def patch_download(self, file_hash: str, action: str):
        return await self.request("PATCH", f"/downloads/{file_hash}", json={"action": action})

    async def cancel_download(self, file_hash: str):
        return await self.request("DELETE", f"/downloads/{file_hash}")

    async def clear_completed(self):
        return await self.request("POST", "/downloads_clear_completed")
